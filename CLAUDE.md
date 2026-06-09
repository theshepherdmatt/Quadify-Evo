# CLAUDE.md — Quadify (Audiophonics EVO Sabre build)

Orientation for an AI assistant working on this project. Read this first.

## What this is
Quadify is a **Volumio plugin** (Node `index.js` controller + a Python display/UI app under `quadifyapp/`)
that drives a front-panel OLED, buttons/LEDs, IR, and a rotary encoder on a Raspberry Pi audio streamer.

This particular install is a **hacked Audiophonics EVO Sabre** (balanced dual-ES9038Q2M DAC + Pi),
shoehorned into a **Quad FM4 tuner** case. It is a one-off; breaking it is acceptable, but see the rules below.

## ⚠️ Critical workflow rules (do not violate)
1. **Know the two locations — they are NOT the same:**
   - **LIVE running plugin** = `/data/plugins/system_hardware/quadify/` on the Pi. This is what actually runs.
     **Make all real changes here, over SSH.**
   - **Backup clone** = `/home/volumio/Quadify-Evo/` = **THIS directory** (the SMB share `\\volumio.local\Quadify\`).
     It is the git repo used for backups. It mirrors the live plugin **only as of the last backup — it can LAG**.
     Editing files here does NOT change the running plugin; it only updates the backup repo.
   - So: don't trust this dir's copy of a file as "what's running" — confirm against `/data/...` over SSH.
2. **The Pi is the source of truth. GitHub is BACKUP ONLY, one-way: Pi → GitHub.**
   Repo: `git@github.com:theshepherdmatt/Quadify-Evo.git`.
   **NEVER copy GitHub → Pi / pull / reset onto the Pi** — it would wipe newer local work. Only push the Pi's
   state up. (There is a separate `Quadify-Plugin` repo = the standard-hardware branch; do not confuse them.)
3. **Always back up after a change** (copy the changed file from `/data/...` into this clone, commit, push — see "Backing up").

## Access
- **SSH:** `ssh volumio` (passwordless key login is set up). User `volumio`, host `volumio.local`.
  The Pi is on DHCP and its **IP drifts** — always use the hostname, never a hard-coded IP. If the host key
  changes after a reinstall: `ssh-keygen -R volumio.local` then reconnect with `StrictHostKeyChecking=accept-new`.
- **Files:** this SMB dir = the backup clone `~/Quadify-Evo` (remote is SSH; the Pi has its own GitHub key).
  To change the *running* plugin you must edit `/data/plugins/system_hardware/quadify/...` over SSH, not here.
  SMB can be slow; large `ls`/greps may need backgrounding.

## How to make a change safely
1. Edit the **live** file over SSH: `/data/plugins/system_hardware/quadify/<path>`.
   **Line endings are CRLF** — preserve them (don't let an editor reflow to LF). Leave a `.bak`.
   (Tip: for multi-point edits, `scp` the file local, edit + `py_compile` locally, `scp` back.)
2. Validate before restarting:
   - Python: `python3 -m py_compile <file>`
   - Node:   `node --check index.js`
3. Restart the affected service and watch logs:
   - `sudo systemctl restart quadify.service`
   - `sudo journalctl -u quadify.service -b --no-pager | tail -50`
4. For `index.js` (the Volumio plugin controller) to reload, restart the Volumio backend:
   `sudo systemctl restart volumio` (blips the UI/playback ~30–60s; the Python display app keeps running).
5. Leave a `.bak` of anything you edit in place as a restore point.

## Backing up (Pi → GitHub)
```
ssh volumio
cd ~/Quadify-Evo
cp /data/plugins/system_hardware/quadify/<path> <path>       # copy the changed file out of the live plugin
git add <path>
git -c user.name='theshepherdmatt' -c user.email='matt.theshepherd@gmail.com' commit -m "..."
git push origin main
```

## Hardware / architecture (EVO Sabre specifics)
- **Display** = the EVO's *secondary* OLED (SSD1322, 256×64, SPI): `gpio10 DATA, gpio11 CLK, gpio24 RST, gpio27 DC, CS`.
  Matches `quadifyapp/config.yaml` (`rst_pin 24`, `dc_pin 27`, `rotation 0`). The EVO's small OLED1 was removed.
- **Volume** = handled by the DAC hardware (IR remote VOL± → ES9038 directly). Quadify does NOT do volume.
- **IR remote** (GPIO4, via LIRC) drives the Pi *and* the DAC simultaneously.
- **Rotary encoder** (GPIO clk13 / dt5 / sw6) = Quadify MENU navigation (not volume).
- **Buttons/LEDs** = an MCP23017 expander at I²C `0x20` (config `mcp23017_address: 32` decimal = 0x20).
  `index.js` normalises the address via `coerceHexAddr` (a YAML number `32` → hex `"20"`); that conversion is
  intentional, not a bug.
- **Power button + its LED** ("button 8" / "LED 8") are wired to the EVO hardware power, isolated from the matrix.

## Services
`quadify.service` (main Python app) · `quadify-buttonsleds` · `ir_listener` · `cava` · `quadify-lirc-post`
· `quadify-leds-off` · `volumio-clean-poweroff`. The Python app's command server listens on a UNIX socket
`/tmp/quadify.sock` (send e.g. `select`, `scroll_up`, `menu`, `exit_screensaver`).

## Key files
- `index.js` — Volumio plugin controller (config UI / `getUIConfig` / install glue).
- `quadifyapp/src/main.py` — boot orchestration, early splash, command server, Volumio listener.
- `quadifyapp/src/managers/mode_manager.py` — the display FSM (clock / screensaver / sleep / menus / playback screens).
- `quadifyapp/src/display/display_manager.py` — luma SSD1322 driver wrapper (`sleep()`/`wake()` = hide/show).
- `quadifyapp/config.yaml` + `quadifyapp/src/preference.json` — hardware config + runtime prefs (ModeManager reads prefs).

## Recent work (newest first; all backed up to Quadify-Evo `main`)
- **Clock-at-boot fix**: Pi has no RTC, so the boot clock is stale until ntpd syncs. `main.py` now waits before
  the clock-screen handoff until the time is correct — gated on `ntpq -c "rv 0 stratum"` (stratum 1–15 = ntpd
  locked/stepped), which releases far sooner than the laggy `NTPSynchronized` flag. Verified on cold boot.
- **OLED deep-sleep tier**: after the screensaver, the panel powers fully off (`ssd1322 hide()`) once
  `oled_sleep_timeout` (default 600s) elapses; any input wakes it (`dispatch_select`/`dispatch_scroll` handle the
  `sleep` state). Lives in `mode_manager.py` + `display_manager.py`.
- **Plugin settings blank page**: `index.js` had ~50 curly/smart quotes (`‘ ’`) used as string delimiters →
  `SyntaxError` → the whole plugin failed to load → empty settings page. Replaced with ASCII quotes.
  **Watch for this recurring** on the EVO — re-check with `node --check index.js`.
- **Early splash**: `main.py` inits the OLED first and paints "Starting up..." before the Volumio/NTP waits
  (in-app, not a separate splash service).

## Known issues / gotchas
- **MCP23017 LEDs/buttons currently dead = a loose wire (hardware).** `i2cdetect -y 1` shows nothing at 0x20.
  This is NOT a software bug — do not chase it in code.
- **Slow reboots** (~90s): four services (`quadify-buttonsleds`, `ir_listener`, `quadify-leds-off`,
  `volumio-clean-poweroff`) default to a 90s `TimeoutStopSec` and don't exit fast on SIGTERM. Known, intentionally
  left alone for now.
- **Smart-quote bug**: the EVO's `index.js` has historically picked up curly quotes — validate JS edits with `node --check`.
- **Future idea (not started):** with OLED1 removed, show the DAC's input selection + volume on the big OLED.
  `0x48` on I²C bus 1 is likely an ES9038Q2M control interface — verify before building.

## Validate-before-you-trust
Don't assume a fix works — restart the service and read the journal. For display behaviour, drive the UNIX
socket (`/tmp/quadify.sock`) or watch `journalctl -u quadify.service -f`.
