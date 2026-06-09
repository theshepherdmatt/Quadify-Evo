#!/usr/bin/env python3
# src/main.py

import RPi.GPIO as GPIO
GPIO.setwarnings(False)

import time
import threading
import logging
import yaml
import socket
import subprocess
import lirc
import os
import sys
import signal
from PIL import Image, ImageDraw

# UI / Hardware Imports
from display.screens.clock import Clock
#from hardware.buttonsleds import ButtonsLEDController
from hardware.shutdown_system import shutdown_system
from display.screens.original_screen import OriginalScreen
from display.screens.modern_screen import ModernScreen
from display.screens.minimal_screen import MinimalScreen
from display.screens.vu_screen import VUScreen
from display.screens.digitalvu_screen import DigitalVUScreen
from display.screensavers.snake_screensaver import SnakeScreensaver
from display.screensavers.geo_screensaver import GeoScreensaver
from display.screensavers.bouncing_text_screensaver import BouncingTextScreensaver
from display.display_manager import DisplayManager
from managers.menu_manager import MenuManager
from managers.mode_manager import ModeManager
from managers.manager_factory import ManagerFactory
from controls.rotary_control import RotaryControl
from network.volumio_listener import VolumioListener
from assets.images.convert2 import main as convert_icons_main


# --------------------------- config / util ---------------------------

def load_config(config_path='/config.yaml'):
    abs_path = os.path.abspath(config_path)
    print(f"Attempting to load config from: {abs_path}")
    print(f"Does the file exist? {os.path.isfile(config_path)}")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
            logging.debug(f"Configuration loaded from {config_path}.")
        except yaml.YAMLError as e:
            logging.error(f"Error loading config file {config_path}: {e}")
    else:
        logging.warning(f"Config file {config_path} not found. Using default configuration.")
    return config




# --------------------------- main ---------------------------

def main():
    # --- Logging ---
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger = logging.getLogger("QuadifyMain")

    # --- Config ---
    config_path = "/data/plugins/system_hardware/quadify/quadifyapp/config.yaml"
    config = load_config(config_path)
    display_config = config.get('display', {})


    # --- DisplayManager ---
    display_manager = DisplayManager(display_config)

    # --- LEDs controller ---
    #buttons_leds = ButtonsLEDController()
    #buttons_leds.start()

    # Convert / ensure menu icons exist
    convert_icons_main()

    # Turn off LED8 (if present on MCP23017)
    # Read address from config so it matches the user's preference (#10).
    try:
        import smbus2
        _mcp_raw = config.get('mcp23017_address', 0x20)
        try:
            if isinstance(_mcp_raw, int):
                MCP23017_ADDRESS = _mcp_raw
            else:
                _s = str(_mcp_raw).strip().lower()
                MCP23017_ADDRESS = int(_s, 16) if _s.startswith('0x') else int(_s, 16)
        except (ValueError, TypeError):
            MCP23017_ADDRESS = 0x20
        MCP23017_GPIOA = 0x12
        bus = smbus2.SMBus(1)
        current = bus.read_byte_data(MCP23017_ADDRESS, MCP23017_GPIOA)
        bus.write_byte_data(MCP23017_ADDRESS, MCP23017_GPIOA, current & 0b11111110)
        bus.close()
    except Exception as e:
        print(f"Error turning off LED8: {e}")

    # --- Boot orchestration ---
    volumio_ready_event = threading.Event()
    ready_stop_event = threading.Event()

    # Show a simple "Starting up..." message until Volumio is ready
    try:
        from PIL import ImageDraw as _ID, ImageFont as _IF
        _w, _h = display_manager.oled.size
        _img = Image.new("RGB", (_w, _h), "black")
        _d = ImageDraw.Draw(_img)
        _font = display_manager.fonts.get("song_font", _IF.load_default())
        _msg = "Starting up..."
        _bb = _font.getbbox(_msg)
        _tw, _th = _bb[2] - _bb[0], _bb[3] - _bb[1]
        _d.text(((_w - _tw) // 2, (_h - _th) // 2), _msg, font=_font, fill="white")
        display_manager.display_pil(_img)
    except Exception as e:
        logger.warning("Could not show startup message: %s", e)
        display_manager.clear_screen()

    # --- VolumioListener (early) ---
    volumio_cfg = config.get('volumio', {})
    volumio_host = volumio_cfg.get('host', 'localhost')
    volumio_port = volumio_cfg.get('port', 3000)

    class DummyModeManager:
        def __init__(self):
            self.last_state = None

        def get_mode(self):
            return None

        def trigger(self, event):
            pass

        def process_state_change(self, sender, state, **kwargs):
            self.last_state = state

    dummy_mode_manager = DummyModeManager()
    volumio_listener = VolumioListener(host=volumio_host, port=volumio_port)
    volumio_listener.mode_manager = dummy_mode_manager

    # --- Rotary (early) to exit ready loop ---
    def on_button_press_inner():
        if not ready_stop_event.is_set():
            ready_stop_event.set()

    rotary_control = RotaryControl(
        rotation_callback=lambda d: None,
        button_callback=on_button_press_inner,
        long_press_callback=lambda: None,
        long_press_threshold=2.5
    )
    threading.Thread(target=rotary_control.start, daemon=True).start()

    # handle_scroll and handle_select have been moved to ModeManager.dispatch_scroll()
    # and ModeManager.dispatch_select() to avoid duplicate if/elif chains (#19).
    # The command server and rotary handlers call those methods directly.

    # --- DAC input tracker ---
    _DAC_INPUTS = ["USB", "RPI", "COAX", "OPT", "BT"]
    _PREF_PATH = "/data/plugins/system_hardware/quadify/quadifyapp/src/preference.json"

    def _load_dac_input_index():
        try:
            import json
            with open(_PREF_PATH, "r") as f:
                return int(json.load(f).get("dac_input_index", 0))
        except Exception:
            return 0

    def _save_dac_input_index(idx):
        try:
            import json
            with open(_PREF_PATH, "r") as f:
                data = json.load(f)
            data["dac_input_index"] = idx
            tmp = _PREF_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, _PREF_PATH)
        except Exception as e:
            logger.warning("Failed to save dac_input_index: %s", e)

    _dac_input_index = [_load_dac_input_index()]  # mutable cell for closure
    _dac_overlay_timer = [None]
    _dac_vol_overlay_timer = [None]

    def handle_dac_input():
        _dac_input_index[0] = (_dac_input_index[0] + 1) % len(_DAC_INPUTS)
        label = _DAC_INPUTS[_dac_input_index[0]]
        _save_dac_input_index(_dac_input_index[0])
        logger.info("DAC input → %s (index %d)", label, _dac_input_index[0])
        _show_dac_input_overlay(label)

    def _show_dac_input_overlay(label):
        """Flash input name on OLED for 2 s then let the current screen resume."""
        from PIL import ImageDraw, ImageFont as _IF
        if _dac_overlay_timer[0]:
            _dac_overlay_timer[0].cancel()

        def _draw():
            try:
                w, h = display_manager.oled.size
                img = Image.new("RGB", (w, h), "black")
                d = ImageDraw.Draw(img)
                font = display_manager.fonts.get("minimal_service",
                       display_manager.fonts.get("song_font", _IF.load_default()))
                bb = font.getbbox(label)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                d.text(((w - tw) // 2, (h - th) // 2), label, font=font, fill="white")
                sub = display_manager.fonts.get("data_font", _IF.load_default())
                tag = "DAC INPUT"
                tb = sub.getbbox(tag)
                d.text(((w - (tb[2]-tb[0])) // 2, (h - th) // 2 - 16),
                       tag, font=sub, fill="white")
                display_manager.display_pil(img)
            except Exception as e:
                logger.warning("dac_input overlay error: %s", e)

        _draw()
        # After 2 s, poke the active screen to redraw itself
        def _restore():
            _dac_overlay_timer[0] = None
            try:
                volumio_listener.socketIO.emit("getState", {})
            except Exception:
                pass
        _dac_overlay_timer[0] = threading.Timer(3.5, _restore)
        _dac_overlay_timer[0].start()

    def _show_dac_volume_overlay(direction):
        """Show VOL + or VOL - on the OLED for 2 s.

        When ModernScreen is active it owns the draw loop, so we delegate to
        it rather than writing directly to the display (which would cause the
        two threads to fight and make the spectrum appear to stop).
        For all other modes we fall back to writing directly.
        """
        from PIL import ImageFont as _IF
        if _dac_vol_overlay_timer[0]:
            _dac_vol_overlay_timer[0].cancel()
            _dac_vol_overlay_timer[0] = None

        # Delegate to ModernScreen if it is the active screen
        try:
            _mm = mode_manager  # noqa: F821 — assigned later in main(), valid at call-time
            if _mm.get_mode() == "modern" and _mm.modern_screen:
                _mm.modern_screen.show_volume_overlay(direction, duration=2.0)
                return
        except (NameError, AttributeError):
            pass  # mode_manager not yet created (early server) — fall through

        # Fallback: draw directly (original / minimal / other modes)
        try:
            w, h = display_manager.oled.size
            img = Image.new("RGB", (w, h), "black")
            d = ImageDraw.Draw(img)
            tag_font = display_manager.fonts.get("playback_medium", _IF.load_default())
            arrow_font = display_manager.fonts.get("clock_bold", _IF.load_default())
            sign = "+" if direction > 0 else "-"
            tag = "VOLUME"
            tb = tag_font.getbbox(tag)
            d.text(((w - (tb[2]-tb[0])) // 2, h // 2 - 28), tag, font=tag_font, fill="white")
            ab = arrow_font.getbbox(sign)
            aw, ah = ab[2] - ab[0], ab[3] - ab[1]
            d.text(((w - aw) // 2, h // 2 - ah // 2 + 4), sign, font=arrow_font, fill="white")
            display_manager.display_pil(img)
        except Exception as e:
            logger.warning("dac_volume overlay error: %s", e)

        def _restore():
            _dac_vol_overlay_timer[0] = None
            try:
                volumio_listener.socketIO.emit("getState", {})
            except Exception:
                pass
        _dac_vol_overlay_timer[0] = threading.Timer(2.0, _restore)
        _dac_vol_overlay_timer[0].start()

    # --------------------- IR command socket server ---------------------

    def make_command_server(mode_manager: ModeManager, early: bool = False):
        """
        Start a UNIX socket server at /tmp/quadify.sock.
        In 'early' mode, the first real press (menu/select/ok/toggle) will stop
        the ready loop AND exit this server so the main UI server can rebind.
        """
        sock_path = "/tmp/quadify.sock"

        def server():
            # Clean up any stale socket file
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            try:
                os.chmod(sock_path, 0o666)  # let other processes (IR listener) connect
            except Exception:
                pass
            srv.listen(5)
            print(f"Quadify command server listening on {sock_path} (early={early})")

            try:
                while True:
                    conn, _ = srv.accept()
                    with conn:
                        data = conn.recv(1024)
                        if not data:
                            continue

                        command = data.decode("utf-8").strip()
                        current_mode = mode_manager.get_mode()
                        # Log at INFO so normal remote use isn't silent, but avoid
                        # per-scroll debug floods (#21).
                        print(f"Command received: {command!r} (mode={current_mode})")

                        # Early server: first real press kills this server so UI one can bind
                        if early and not ready_stop_event.is_set() and command in ("menu", "select", "ok", "toggle"):
                            print("Exiting ready GIF due to remote control command (early server).")
                            ready_stop_event.set()
                            return  # <-- IMPORTANT: exit early server, frees the socket

                        if command == "home":
                            mode_manager.trigger("to_clock")
                        elif command == "shutdown":
                            try:
                                from PIL import ImageFont as _IF
                                w, h = display_manager.oled.size
                                img = Image.new("RGB", (w, h), "black")
                                d = ImageDraw.Draw(img)
                                font = display_manager.fonts.get("song_font", _IF.load_default())
                                msg = "Shutting down..."
                                bb = font.getbbox(msg)
                                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                                d.text(((w - tw) // 2, (h - th) // 2), msg, font=font, fill="white")
                                display_manager.display_pil(img)
                                time.sleep(2)
                            except Exception as e:
                                logger.warning("shutdown overlay error: %s", e)
                            subprocess.run(["sudo", "/bin/systemctl", "poweroff", "--no-wall"], check=False)

                        elif command == "menu":
                            if current_mode == "clock":
                                mode_manager.trigger("to_menu")
                        elif command == "toggle":
                            mode_manager.toggle_play_pause()
                        elif command == "repeat":
                            pass

                        elif command == "select":
                            mode_manager.dispatch_select()  # (#19)

                        elif command in ("scroll_up", "scroll_left"):
                            mode_manager.dispatch_scroll(-1)   # (#19)
                        elif command in ("scroll_down", "scroll_right"):
                            mode_manager.dispatch_scroll(+1)   # (#19)

                        elif command == "seek_plus":
                            subprocess.run(["volumio", "seek", "plus"], check=False)
                        elif command == "seek_minus":
                            subprocess.run(["volumio", "seek", "minus"], check=False)
                        elif command == "skip_next":
                            subprocess.run(["volumio", "next"], check=False)
                        elif command == "skip_previous":
                            subprocess.run(["volumio", "previous"], check=False)
                        elif command == "volume_plus":
                            _show_dac_volume_overlay(1)
                        elif command == "volume_minus":
                            _show_dac_volume_overlay(-1)
                        elif command == "back":
                            mode_manager.trigger("back")
                        elif command == "dac_input":
                            handle_dac_input()
            except Exception as e:
                print(f"Error in command server: {e}")
            finally:
                try:
                    srv.close()
                except Exception:
                    pass
                # Ensure the path is freed for the next bind
                try:
                    os.unlink(sock_path)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        return t


    # Start early command server with dummy manager (for ready exit + basic commands)
    make_command_server(dummy_mode_manager, early=True)
    print("Quadify command server thread (early) started.")


    def on_state_changed(sender, state):
        logger.info(f"[on_state_changed] State: {state!r}")
        status = str(state.get('status', '???')).lower()
        # Do NOT call mode_manager.process_state_change here — ModeManager subscribes
        # directly to volumio_listener.state_changed in its __init__ (#2).
        ready_stop_event.set()
        if not volumio_ready_event.is_set():
            volumio_ready_event.set()

    volumio_listener.state_changed.connect(on_state_changed)
    # Also treat a successful socket connection as sufficient to proceed,
    # in case Volumio connects but doesn't push state immediately.
    def _on_connected(sender, **kwargs):
        if not volumio_ready_event.is_set():
            logger.info("Socket connected — proceeding to UI startup.")
            volumio_ready_event.set()
    volumio_listener.connected.connect(_on_connected)

    logger.info("Waiting for Volumio (max 90s)...")
    volumio_ready_event.wait(timeout=90)
    if not volumio_ready_event.is_set():
        logger.warning("Volumio not ready after 90s — starting UI anyway.")
    ready_stop_event.set()
    logger.info("Continuing to UI startup.")

    # --- Build full UI stack ---
    clock_config = config.get('clock', {})
    clock = Clock(display_manager, clock_config, volumio_listener)
    clock.logger = logging.getLogger("Clock")
    clock.logger.setLevel(logging.INFO)

    mode_manager = ModeManager(
        display_manager=display_manager,
        clock=clock,
        volumio_listener=volumio_listener,
        preference_file_path="../preference.json",
        config=config
    )

    manager_factory = ManagerFactory(
        display_manager=display_manager,
        volumio_listener=volumio_listener,
        mode_manager=mode_manager,
        config=config
    )
    manager_factory.setup_mode_manager()
    volumio_listener.mode_manager = mode_manager

    # Wire the sources_changed signal emitted by VolumioListener to the menu
    # refresh.  This replaces the direct menu_manager attribute reference that
    # was previously kept on VolumioListener (#13).
    def _on_sources_changed(sender, sources=None, **kwargs):
        mm = mode_manager.menu_manager
        if mm:
            mm.refresh_main_menu()
            mm.display_menu()
    volumio_listener.sources_changed.connect(_on_sources_changed)

    # Handoff last early state if any — determine initial screen
    last_state = getattr(dummy_mode_manager, 'last_state', None)
    if last_state:
        logger.info("Handing off last Volumio state from DummyModeManager to real ModeManager")
        mode_manager.process_state_change(volumio_listener, last_state)
        status = (last_state.get("status") or "").lower()
        service = (last_state.get("service") or "").lower()
        display_mode = mode_manager.config.get("display_mode", "original")

        if status == "play":
            if service == "webradio":
                mode_manager.trigger("to_webradio")
            elif display_mode == "vuscreen":
                mode_manager.trigger("to_vuscreen")
            elif display_mode == "digitalvuscreen":
                mode_manager.trigger("to_digitalvuscreen")
            elif display_mode == "modern":
                mode_manager.trigger("to_modern")
            elif display_mode == "minimal":
                mode_manager.trigger("to_minimal")
            else:
                mode_manager.trigger("to_original")
        else:
            # stopped, paused, or unknown — start on clock
            mode_manager.trigger("to_clock")
    else:
        # No state received yet — start on clock, ModeManager will react to pushState
        mode_manager.trigger("to_clock")

    # Start the real command server bound to the real mode_manager
    make_command_server(mode_manager, early=False)
    print("Quadify command server thread (UI) started.")

    # --- Rotary handlers ---
    def on_rotate_ui(direction):
        # Rotary only scrolls menus — DAC controls hardware volume via IR remote.
        mode_manager.dispatch_scroll(1 if direction > 0 else -1)

    def on_button_press_ui():
        mode_manager.dispatch_select()  # (#19)

    def on_long_press_ui():
        current_mode = mode_manager.get_mode()
        if current_mode == "menu":
            mode_manager.trigger("to_clock")
        else:
            mode_manager.trigger("back")

    rotary_control.rotation_callback = on_rotate_ui
    rotary_control.button_callback = on_button_press_ui
    rotary_control.long_press_callback = on_long_press_ui

    # --- Graceful SIGTERM handler (systemd sends SIGTERM on 'systemctl stop') ---
    def _handle_sigterm(signum, frame):
        logger.info("Received SIGTERM; initiating graceful shutdown.")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # --- Main loop ---
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down Quadify via KeyboardInterrupt.")
    finally:
        if 'buttons_leds' in locals() and buttons_leds:
            try:
                buttons_leds.stop()
            except Exception as e:
                logger.warning(f"Error stopping buttons_leds: {e}")

        if 'rotary_control' in locals() and rotary_control:
            try:
                rotary_control.stop()
            except Exception as e:
                logger.warning(f"Error stopping rotary_control: {e}")

        try:
            volumio_listener.stop_listener()
        except Exception as e:
            logger.warning(f"Error stopping volumio_listener: {e}")

        if 'clock' in locals() and clock:
            try:
                clock.stop()
            except Exception as e:
                logger.warning(f"Error stopping clock: {e}")

        if 'display_manager' in locals() and display_manager:
            try:
                display_manager.cleanup()  # clears screen and releases SPI/serial
            except Exception as e:
                logger.warning(f"Error cleaning up display_manager: {e}")

        logger.info("Quadify shut down gracefully.")


if __name__ == "__main__":
    main()

