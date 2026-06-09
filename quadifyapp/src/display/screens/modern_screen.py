# src/display/screens/modern_screen.py

import logging
import os
import subprocess
import threading
import time
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from managers.base_manager import BaseManager

# IconProvider is optional: prefer a provided instance on the mode_manager,
# otherwise try to construct one. If import fails, we'll fall back gracefully.
try:
    from handlers.icon_provider import IconProvider
except Exception:  # noqa: BLE001
    IconProvider = None  # type: ignore

FIFO_PATH = "/tmp/display.fifo"  # Path to the FIFO for CAVA data



class ModernScreen(BaseManager):
    """
    A 'Modern' / 'Detailed' playback screen:
      - Artist & Title (scrolling when needed)
      - Optional spectrum visualization (CAVA via FIFO)
      - Progress bar + current/total time
      - Volume + track info
      - Small service icon (Tidal, Qobuz, Spotify, Radio Paradise, etc.)
    """

    # --------------------------- Init & wiring ---------------------------

    def __init__(self, display_manager, volumio_listener, mode_manager):
        super().__init__(display_manager, volumio_listener, mode_manager)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        self.mode_manager = mode_manager
        self.volumio_listener = volumio_listener

        # Prefer IconProvider over display_manager icons
        self.icon_provider = None
        try:
            self.icon_provider = IconProvider()
            self.logger.info("ModeManager: IconProvider dir=%s manifest=%s",
                            getattr(self.icon_provider, "assets_dir", None),
                            getattr(self.icon_provider, "manifest_path", None))
        except Exception as e:
            self.logger.warning("ModeManager: IconProvider not available: %s", e)

        # Spectrum / CAVA
        self.running_spectrum = False
        self.spectrum_thread = None
        self.spectrum_bars = []
        _disp_cfg = display_manager.config.get("display", display_manager.config)
        self.fifo_path = _disp_cfg.get("fifo_path", FIFO_PATH)
        self._spec_brightness = int(_disp_cfg.get("spectrum_brightness", 60))
        self._spec_peak_brightness = int(_disp_cfg.get("spectrum_peak_brightness", 100))
        self._spec_bar_width = int(_disp_cfg.get("spectrum_bar_width", 2))
        self._spec_gap_width = int(_disp_cfg.get("spectrum_gap_width", 3))
        self.spectrum_mode = self.mode_manager.config.get("modern_spectrum_mode", "bars")  # "bars" | "dots" | "scope"

        # Dot/scope smoothing state
        self._dot_prev_heights = []
        self._dot_peak_heights = []
        self._dot_last_ts = time.time()

        # Fonts
        self.font_title = display_manager.fonts.get("song_font", ImageFont.load_default())
        self.font_artist = display_manager.fonts.get("artist_font", ImageFont.load_default())
        self.font_info = display_manager.fonts.get("data_font", ImageFont.load_default())
        self.font_progress = display_manager.fonts.get("progress_bar", ImageFont.load_default())

        # Marquee scrolling
        self.scroll_offset_title = 0
        self.scroll_offset_artist = 0
        self.scroll_speed = 2
        self._title_pause  = self._SCROLL_PAUSE_FRAMES
        self._artist_pause = self._SCROLL_PAUSE_FRAMES

        # State & threading
        self.latest_state = None
        self.current_state = None
        self.state_lock = threading.Lock()
        self.update_event = threading.Event()
        self.stop_event = threading.Event()
        self.is_active = False

        # Keep last-known service to show its icon while paused/stopped
        self.previous_service: Optional[str] = None

        # Volume overlay state (set externally via show_volume_overlay())
        self._vol_overlay_dir = None   # +1 or -1
        self._vol_overlay_until = 0.0  # epoch timestamp when it expires

        # Display update thread
        self.update_thread = threading.Thread(target=self.update_display_loop, daemon=True)
        self.update_thread.start()
        self.logger.info("ModernScreen: Started background update thread.")

        # Connect to Volumio listener
        if self.volumio_listener:
            self.volumio_listener.state_changed.connect(self.on_volumio_state_change)
        self.logger.info("ModernScreen initialized.")

    # --------------------------- Volumio state ---------------------------

    def on_volumio_state_change(self, sender, state):
        """React to Volumio state changes only when active in 'modern' mode."""
        if not self.is_active or self.mode_manager.get_mode() != "modern":
            self.logger.debug("ModernScreen: ignoring state change; not active or mode != 'modern'.")
            return

        self.logger.debug("ModernScreen: state changed => %s", state)
        with self.state_lock:
            self.latest_state = state
        self.update_event.set()

    # --------------------------- Update loop -----------------------------

    def update_display_loop(self):
        last_update_time = time.time()
        while not self.stop_event.is_set():
            triggered = self.update_event.wait(timeout=0.1)
            with self.state_lock:
                if triggered and self.latest_state:
                    self.current_state = self.latest_state.copy()
                    self.latest_state = None
                    self.update_event.clear()
                    last_update_time = time.time()
                elif self.current_state:
                    status = (self.current_state.get("status") or "").lower()
                    duration_val = self.current_state.get("duration")
                    try:
                        duration_ok = int(duration_val) > 0
                    except Exception:  # noqa: BLE001
                        duration_ok = False

                    if status == "play" and duration_ok:
                        elapsed = time.time() - last_update_time
                        self.current_state["seek"] = int(self.current_state.get("seek") or 0) + int(elapsed * 1000)
                    last_update_time = time.time()

            if self.is_active and self.mode_manager.get_mode() == "modern" and self.current_state:
                self.draw_display(self.current_state)

    # --------------------------- Start/Stop ------------------------------

    def start_mode(self):
        if self.mode_manager.get_mode() != "modern":
            self.logger.warning("ModernScreen: Attempted start, but mode != 'modern'.")
            return

        self.is_active = True
        self.reset_scrolling()
        self.spectrum_mode = self.mode_manager.config.get("modern_spectrum_mode", "bars")

        # Clear any stale pixels
        self.display_manager.clear_screen()

        # Force immediate state refresh
        try:
            if self.volumio_listener and self.volumio_listener.socketIO:
                self.volumio_listener.socketIO.emit("getState", {})
        except Exception as e:
            self.logger.warning("ModernScreen: Failed to emit 'getState'. Error => %s", e)

        # Ensure CAVA is running before starting the spectrum thread
        if self.mode_manager.config.get("cava_enabled", False):
            if not self._is_cava_running():
                self.logger.info("ModernScreen: CAVA not running — attempting restart.")
                self._start_cava_service()
            else:
                self.logger.info("ModernScreen: CAVA already running.")

        # Start spectrum thread
        if not self.spectrum_thread or not self.spectrum_thread.is_alive():
            self.running_spectrum = True
            self.spectrum_thread = threading.Thread(target=self._read_fifo, daemon=True)
            self.spectrum_thread.start()
            self.logger.info("ModernScreen: Spectrum reading thread started.")

        # Ensure update thread alive
        if not self.update_thread.is_alive():
            self.stop_event.clear()
            self.update_thread = threading.Thread(target=self.update_display_loop, daemon=True)
            self.update_thread.start()


    def stop_mode(self):
        if not self.is_active:
            return

        self.is_active = False
        self.stop_event.set()
        self.update_event.set()

        # Stop spectrum thread
        self.running_spectrum = False
        if self.spectrum_thread and self.spectrum_thread.is_alive():
            self.spectrum_thread.join(timeout=1)
            self.logger.info("ModernScreen: Spectrum thread stopped.")

        # Stop update thread
        if self.update_thread.is_alive():
            self.update_thread.join(timeout=1)

        self.display_manager.clear_screen()
        self.logger.info("ModernScreen: Stopped mode and cleared screen.")

    # --------------------------- CAVA helpers ----------------------------

    def _is_cava_running(self):
        try:
            subprocess.check_call(
                ['systemctl', 'is-active', '--quiet', 'cava'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def _start_cava_service(self):
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'restart', 'cava'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.logger.info("ModernScreen: CAVA service restarted.")
        except Exception as e:
            self.logger.error("ModernScreen: Failed to restart CAVA: %s", e)

    # --------------------------- Spectrum FIFO ---------------------------

    def _read_fifo(self):
        """
        Continuously read spectrum data from FIFO.
        Auto-reconnects if FIFO disappears (e.g. cava restarted).
        """
        fifo_path = self.fifo_path
        retry_delay = 1.0
        self.logger.info("ModernScreen: Spectrum thread started, reading %s", fifo_path)

        while self.running_spectrum:
            if not os.path.exists(fifo_path):
                self.logger.warning("ModernScreen: FIFO %s not found. Retrying in %.1fs", fifo_path, retry_delay)
                time.sleep(retry_delay)
                continue
            try:
                with open(fifo_path, "r") as fifo:
                    for line in fifo:
                        if not self.running_spectrum:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            bars = [int(x) for x in line.split(";") if x.isdigit()]
                            if bars:
                                self.spectrum_bars = bars
                        except Exception as e:
                            self.logger.error("ModernScreen: Failed to parse FIFO line '%s' -> %s", line, e)
            except Exception as e:
                self.logger.error("ModernScreen: FIFO read error: %s. Retrying in %.1fs", e, retry_delay)
                time.sleep(retry_delay)

        self.logger.info("ModernScreen: Spectrum thread exiting.")



    # --------------------------- Utilities -------------------------------

    # Frames to hold still before scrolling starts / after one full loop
    _SCROLL_PAUSE_FRAMES = 25

    def reset_scrolling(self):
        self.scroll_offset_title = 0
        self.scroll_offset_artist = 0
        self._title_pause  = self._SCROLL_PAUSE_FRAMES
        self._artist_pause = self._SCROLL_PAUSE_FRAMES

    def _advance_scroll(self, text, font, max_width, offset, pause_attr):
        """Return (new_offset, scrolling) and decrement the pause counter."""
        bb = font.getbbox(text)
        text_width = bb[2] - bb[0]
        if text_width <= max_width:
            return 0, False

        pause = getattr(self, pause_attr, 0)
        if pause > 0:
            setattr(self, pause_attr, pause - 1)
            return offset, True          # scrolling=True but offset unchanged

        gap = 32
        total = text_width + gap
        new_offset = (offset + self.scroll_speed) % total
        if new_offset < offset:          # wrapped — pause again
            setattr(self, pause_attr, self._SCROLL_PAUSE_FRAMES)
        return new_offset, True

    def _blit_scrolling_text(self, base_image, text, font, x, y, max_width, offset):
        """
        Render a seamlessly-looping marquee into base_image at (x, y),
        clipped to max_width pixels wide.
        """
        bb = font.getbbox(text)
        text_width  = bb[2] - bb[0]
        text_height = bb[3] - bb[1]
        y_pad = -bb[1]          # shift down so ascenders aren't clipped
        gap = 32
        total = text_width + gap

        strip_h = text_height + y_pad + 2
        strip = Image.new("RGB", (total * 2, strip_h), "black")
        sd = ImageDraw.Draw(strip)
        sd.text((0,     y_pad), text, font=font, fill="white")
        sd.text((total, y_pad), text, font=font, fill="white")

        crop_x = int(offset) % total
        crop = strip.crop((crop_x, 0, crop_x + max_width, strip_h))
        base_image.paste(crop, (x, y))

    def adjust_volume(self, volume_change):
        if not self.volumio_listener:
            self.logger.error("ModernScreen: no volumio_listener, cannot adjust volume.")
            return

        if self.latest_state is None:
            self.latest_state = {"volume": 100}

        with self.state_lock:
            curr_vol = self.latest_state.get("volume", 100)
            new_vol = max(0, min(int(curr_vol) + volume_change, 100))

        try:
            if volume_change > 0:
                self.volumio_listener.socketIO.emit("volume", "+")
            elif volume_change < 0:
                self.volumio_listener.socketIO.emit("volume", "-")
            else:
                self.volumio_listener.socketIO.emit("volume", new_vol)
        except Exception as e:  # noqa: BLE001
            self.logger.error("ModernScreen: error adjusting volume => %s", e)

    def show_volume_overlay(self, direction, duration=2.0):
        """Request a VOLUME +/- overlay for `duration` seconds (thread-safe)."""
        self._vol_overlay_dir = direction
        self._vol_overlay_until = time.time() + duration
        self.update_event.set()

    # --------------------------- Icons -----------------------------------

    def _service_key_for_provider(self, service: str) -> str:
        s = (service or "").lower()
        mapping = {
            "radio_paradise": "RADIO_PARADISE",
            "radioparadise": "RADIO_PARADISE",
            "mother_earth_radio": "MOTHER_EARTH_RADIO",
            "motherearthradio": "MOTHER_EARTH_RADIO",
            "webradio": "WEB_RADIO",
            "spop": "SPOTIFY",
            "spotify": "SPOTIFY",
            "qobuz": "QOBUZ",
            "tidal": "TIDAL",
            "mpd": "MUSIC_LIBRARY",
        }
        return mapping.get(s, s.upper())

    def _get_service_icon(self, service: str, size: int = 16) -> Optional[Image.Image]:
        """Prefer IconProvider; fall back to display_manager icons."""
        icon = None
        if self.icon_provider:
            key = self._service_key_for_provider(service)
            # If your IconProvider exposes get_service_icon_from_state, we'll use it elsewhere with data
            get_icon = getattr(self.icon_provider, "get_icon", None)
            if callable(get_icon):
                icon = get_icon(key, size=size) or get_icon(service, size=size)
        if icon is None:
            dm_icon = getattr(self.display_manager, "icons", {}).get(service)
            if dm_icon:
                icon = dm_icon.resize((size, size), Image.LANCZOS).convert("RGB")
        return icon

    def _draw_volume_glyph(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        volume=None,           # ignored
        muted: bool = False,   # ignored
        scale: int = 1,        # optional; or pass size=...
        size: int = None
    ):
        """
        Draw a tiny right-pointing triangle (speaker) only.
        Defaults to ~9px wide at scale=1. No waves, no mute X.
        """
        s = int(size if size is not None else max(7, 9 * scale))

        # nudge to even pixels for crisper edges on OLEDs
        if x & 1: x += 1
        if y & 1: y += 1

        # triangle points: left mid, top-right, bottom-right
        pts = [
            (x,           y + s // 2),
            (x + s,       y),
            (x + s,       y + s),
        ]
        draw.polygon(pts, fill="white")


    # --------------------------- Drawing ---------------------------------

    def draw_display(self, data):
        """
        Render modern playback screen.
        """
        base_image = Image.new("RGB", self.display_manager.oled.size, "black")
        draw = ImageDraw.Draw(base_image)

        spectrum_enabled = self.running_spectrum and self.mode_manager.config.get("cava_enabled", False)

        # Service resolution and memory of previous
        service = (data.get("service") or "").lower()
        track_type = (data.get("trackType") or "").lower()
        status = (data.get("status") or "").lower()

        if service == "mpd" and track_type in {"tidal", "qobuz", "spotify", "radio_paradise"}:
            service = track_type

        if status in {"pause", "stop"} and not service:
            service = self.previous_service or "default"
        else:
            if service and service != self.previous_service:
                self.logger.info("ModernScreen: Service changed => %s", service)
            self.previous_service = service or self.previous_service or "default"

        # 1) Spectrum
        self._draw_spectrum(draw)

        # 2) Core state
        song_title = data.get("title", "Unknown Title")
        artist_name = data.get("artist", "Unknown Artist")
        seek_ms = data.get("seek", 0)
        duration_s = max(1, int(data.get("duration", 1)))
        samplerate = data.get("samplerate", "N/A")
        bitdepth = data.get("bitdepth", "N/A")
        volume = data.get("volume", 50)
        muted = bool(data.get("mute", False))

        seek_s = max(0, int(seek_ms) / 1000 if seek_ms is not None else 0)
        progress = max(0.0, min(seek_s / duration_s, 1.0))

        cur_min, cur_sec = divmod(int(seek_s), 60)
        tot_min, tot_sec = divmod(int(duration_s), 60)
        current_time = f"{cur_min}:{cur_sec:02d}"
        total_duration = f"{tot_min}:{tot_sec:02d}"

        # 3) Text layout
        screen_width, screen_height = self.display_manager.oled.size
        margin = 5
        max_text_width = screen_width - 2 * margin
        line_shift = 6 if not spectrum_enabled else 0  # lift text a touch when spectrum off

        # Artist (top)
        self.scroll_offset_artist, artist_scrolling = self._advance_scroll(
            artist_name, self.font_artist, max_text_width,
            self.scroll_offset_artist, "_artist_pause"
        )
        artist_y = margin - 8
        if artist_scrolling:
            self._blit_scrolling_text(base_image, artist_name, self.font_artist,
                                      margin, artist_y, max_text_width,
                                      self.scroll_offset_artist)
        else:
            _bb = self.font_artist.getbbox(artist_name)
            artist_x = (screen_width - (_bb[2] - _bb[0])) // 2
            draw.text((artist_x, artist_y), artist_name, font=self.font_artist, fill="white")

        # Title
        self.scroll_offset_title, title_scrolling = self._advance_scroll(
            song_title, self.font_title, max_text_width,
            self.scroll_offset_title, "_title_pause"
        )
        title_y = (margin + 6) + line_shift
        if title_scrolling:
            self._blit_scrolling_text(base_image, song_title, self.font_title,
                                      margin, title_y, max_text_width,
                                      self.scroll_offset_title)
        else:
            _bb = self.font_title.getbbox(song_title)
            title_x = (screen_width - (_bb[2] - _bb[0])) // 2
            draw.text((title_x, title_y), song_title, font=self.font_title, fill="white")

        # Info: samplerate / bitdepth
        info_text = f"{samplerate} / {bitdepth}"
        _bb = draw.textbbox((0, 0), info_text, font=self.font_info)
        info_w = _bb[2] - _bb[0]
        info_x = (screen_width - info_w) // 2
        info_y = (margin + 25) + line_shift
        draw.text((info_x, info_y), info_text, font=self.font_info, fill="white")

        # 4) Progress bar + times
        progress_width = int(screen_width * 0.7)
        progress_x = (screen_width - progress_width) // 2
        progress_y = margin + 53  # slightly higher than before

        # Current time (left)
        draw.text((progress_x - 30, progress_y - 9), current_time, font=self.font_info, fill="white")

        # Total duration (right)
        dur_x = progress_x + progress_width + 12
        dur_y = progress_y - 9
        draw.text((dur_x, dur_y), total_duration, font=self.font_info, fill="white")

        # Progress line + indicator
        draw.line([progress_x, progress_y, progress_x + progress_width, progress_y], fill="white", width=1)
        indicator_x = progress_x + int(progress_width * progress)
        draw.line([indicator_x, progress_y - 2, indicator_x, progress_y + 2], fill="white", width=1)

        # 5) Service icon near duration (slightly above, right-aligned to text end)
        icon_size = 22
        service_icon = None
        # first try state-aware
        if self.icon_provider and hasattr(self.icon_provider, "get_service_icon_from_state"):
            service_icon = self.icon_provider.get_service_icon_from_state(data, size=icon_size)
        # then try direct mapping (mpd -> MUSIC_LIBRARY, etc.)
        if not service_icon and self.icon_provider:
            service_icon = self.icon_provider.get_icon(self._service_key_for_provider(service), size=icon_size)

        if service_icon:
            if service_icon.mode == "RGBA":
                bg = Image.new("RGB", service_icon.size, (0, 0, 0))
                bg.paste(service_icon, mask=service_icon.split()[3])
                service_icon = bg

            _bb = draw.textbbox((0, 0), total_duration, font=self.font_info)
            dur_text_w, dur_text_h = _bb[2] - _bb[0], _bb[3] - _bb[1]
            right_edge = dur_x + dur_text_w
            SERVICE_ICON_Y_PAD = -3   # how much above the duration baseline
            SERVICE_ICON_X_PAD = -1   # small gap to the right edge

            icon_x = right_edge - icon_size - SERVICE_ICON_X_PAD
            icon_y = dur_y - icon_size - SERVICE_ICON_Y_PAD

            # Clamp within screen
            screen_w, screen_h = self.display_manager.oled.size
            icon_x = max(0, min(icon_x, screen_w - icon_size))
            icon_y = max(0, min(icon_y, screen_h - icon_size))

            base_image.paste(service_icon, (icon_x, icon_y))

        # Volume overlay (drawn last so it sits on top of everything)
        if self._vol_overlay_dir is not None:
            if time.time() < self._vol_overlay_until:
                base_image = Image.new("RGB", self.display_manager.oled.size, "black")
                draw = ImageDraw.Draw(base_image)
                w, h = self.display_manager.oled.size
                tag_font = self.display_manager.fonts.get("playback_medium", self.font_info)
                arrow_font = self.display_manager.fonts.get("clock_bold", self.font_title)
                sign = "+" if self._vol_overlay_dir > 0 else "-"
                tag = "VOLUME"
                tb = tag_font.getbbox(tag)
                draw.text(((w - (tb[2] - tb[0])) // 2, h // 2 - 28), tag, font=tag_font, fill="white")
                ab = arrow_font.getbbox(sign)
                aw, ah = ab[2] - ab[0], ab[3] - ab[1]
                draw.text(((w - aw) // 2, h // 2 - ah // 2 + 4), sign, font=arrow_font, fill="white")
            else:
                self._vol_overlay_dir = None

        # Present
        self.display_manager.display_pil(base_image)

    # --------------------------- Spectrum drawing ------------------------

    def _draw_spectrum(self, draw: ImageDraw.ImageDraw):
        width, height = self.display_manager.oled.size
        bar_region_height = height // 2
        vertical_offset = -8

        # Case 1: user disabled spectrum
        if not self.mode_manager.config.get("cava_enabled", False):
            y_top = max(0, vertical_offset)
            y_bottom = min(height, bar_region_height + vertical_offset)
            draw.rectangle([0, y_top, width, y_bottom], fill="black")
            return

        # Case 2: enabled, but not running
        if not self.running_spectrum:
            self.logger.warning("ModernScreen: Spectrum enabled but thread not running – restarting.")
            self.running_spectrum = True
            self.spectrum_thread = threading.Thread(target=self._read_fifo, daemon=True)
            self.spectrum_thread.start()

        bars = self.spectrum_bars
        n = len(bars)
        if n == 0:
            return

        # Layout — configurable via config.yaml
        bar_width = self._spec_bar_width
        gap_width = self._spec_gap_width
        max_height = bar_region_height
        start_x = (width - (n * (bar_width + gap_width))) // 2
        y_base = height + vertical_offset  # bottom of spectrum area

        # Clear spectrum area to prevent ghosting
        draw.rectangle([0, y_base - max_height, width, y_base], fill="black")

        # Smoothing / peaks
        if len(self._dot_prev_heights) != n:
            self._dot_prev_heights = [0] * n
            self._dot_peak_heights = [0] * n

        now = time.time()
        dt = max(0.0, min(0.2, now - self._dot_last_ts))
        self._dot_last_ts = now

        target_heights = [int(max(0, min(b, 255)) * (max_height / 255.0)) for b in bars]
        alpha = 0.35

        dot_size = 3
        dot_pitch = dot_size + 1
        col_x_pad = max(0, (bar_width - dot_size) // 2)

        peak_decay_px_per_sec = 60.0
        peak_decay = int(peak_decay_px_per_sec * dt)

        bv = self._spec_brightness
        pv = self._spec_peak_brightness
        bar_col = (bv, bv, bv)
        dot_col = (bv, bv, bv)
        peak_col = (pv, pv, pv)
        scope_col = (bv, bv, bv)

        if self.spectrum_mode == "bars":
            for i, h_t in enumerate(target_heights):
                h = int(self._dot_prev_heights[i] + alpha * (h_t - self._dot_prev_heights[i]))
                self._dot_prev_heights[i] = h

                x1 = start_x + i * (bar_width + gap_width)
                x2 = x1 + bar_width
                y1 = y_base - h
                y2 = y_base
                draw.rectangle([x1, y1, x2, y2], fill=bar_col)

        elif self.spectrum_mode == "dots":
            for i, h_t in enumerate(target_heights):
                h = int(self._dot_prev_heights[i] + alpha * (h_t - self._dot_prev_heights[i]))
                self._dot_prev_heights[i] = h

                self._dot_peak_heights[i] = max(self._dot_peak_heights[i] - peak_decay, h)
                peak_h = self._dot_peak_heights[i]

                num_dots = max(0, h // dot_pitch)
                x = start_x + i * (bar_width + gap_width) + col_x_pad

                for d in range(num_dots):
                    y = y_base - (d * dot_pitch) - dot_size
                    draw.ellipse([x, y, x + dot_size, y + dot_size], fill=dot_col)

                if peak_h > 0:
                    peak_row = max(0, (peak_h // dot_pitch) - 1)
                    y_peak = y_base - (peak_row * dot_pitch) - dot_size
                    draw.ellipse([x, y_peak, x + dot_size, y_peak + dot_size], fill=peak_col)

        elif self.spectrum_mode == "scope":
            scope_data = [int(h) for h in target_heights]
            prev_x = start_x
            prev_y = y_base - scope_data[0]
            for i, val in enumerate(scope_data[1:], 1):
                x = start_x + i * (bar_width + gap_width)
                y = y_base - val
                draw.line([prev_x, prev_y, x, y], fill=scope_col, width=1)
                prev_x, prev_y = x, y

    # --------------------------- External actions ------------------------

    def display_playback_info(self):
        state = self.volumio_listener.get_current_state()
        if state:
            self.draw_display(state)
        else:
            self.logger.warning("ModernScreen: No current volumio state available to display.")

    def toggle_play_pause(self):
        self.logger.info("ModernScreen: Toggling play/pause.")
        if not self.volumio_listener or not self.volumio_listener.is_connected():
            self.logger.warning("ModernScreen: Not connected to Volumio => cannot toggle.")
            return
        try:
            self.volumio_listener.socketIO.emit("toggle", {})
        except Exception as e:  # noqa: BLE001
            self.logger.error("ModernScreen: toggle_play_pause failed => %s", e)
