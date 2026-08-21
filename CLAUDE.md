# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DeckaTV is a [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for SteamOS machines (a Steam Deck, a Steam Machine, or a custom build). It pairs with network TVs, switches their HDMI input from the Quick Access menu, and auto-switches a TV to a chosen input when the machine is docked to that specific physical screen. A Python backend (`main.py` + `backend/`) talks to TVs and the OS; a React frontend (`src/`) is the Quick Access panel. The two communicate over Decky's `callable` RPC bridge (see `src/api.ts` ↔ the `async def` methods on `Plugin` in `main.py`).

### Layout

Decky loads the plugin from its root, so the files it requires there stay at the root and can't be moved: `main.py` (backend entry), `plugin.json`, `package.json`/`pnpm-lock.yaml`, and the build config `rollup.config.js` (defaults to `src/index.tsx`) / `tsconfig.json`. Everything else is grouped: `backend/` and `src/` are *your source* (Python + frontend); `scripts/` is build/deploy tooling; the gitignored `node_modules/`, `py_modules/`, `dist/` are *dependencies and build output*. The build/deploy scripts stage the root files + `backend/` + `py_modules/` + `dist/` into a flat plugin folder, so the deployed structure differs from the repo.

## Commands

```bash
make venv-dev    # one-time: create .venv and install pytest (from requirements-dev.txt)
make test        # unit tests: PYTHONPATH=backend pytest backend/tv_core/tests backend/tv_driver_lg/tests tests .github/scripts/tests -q
make build       # frontend only -> dist/index.js (rollup)
make deploy      # build + rsync into ~/homebrew/plugins/DeckaTV on this machine
make release     # build the distributable deckatv.zip
pnpm run watch   # rebuild the frontend on change

# run a single test
PYTHONPATH=backend python3 -m pytest backend/tv_core/tests/test_store.py -q
PYTHONPATH=backend python3 -m pytest backend/tv_core/tests/test_driver.py::<name> -q

# deploy to a machine over SSH (defaults: deck@steamdeck.lan)
DECK_HOST=192.168.1.50 ./scripts/deploy_remote.sh
```

There is no Python linter config and no JS test suite; `make test` covers the core library, the release-version tooling, and `main.py`'s auto-switch scheduling (root `tests/`, whose `conftest.py` stubs the Decky-injected `decky` module so `main.py` imports off-device). `scripts/pair_test.py` (pairs against a real TV) and `scripts/audio_probe.py` (prints the live output level, for picking the keep-alive's silence floor) are manual, hardware-dependent smoke scripts, not part of `make test`. The build/deploy scripts (`scripts/*.sh`) `cd` to the repo root themselves, so they can be run from anywhere.

## Architecture

Dependencies point one way: `tv_driver_lg → tv_core`, and `main.py → both`. **The core never imports a concrete driver.** `main.py` is the composition root — it builds the driver registry (`REGISTRY = build_registry([LgDriver()])`) and is the only place that names a brand.

- `backend/tv_core/` — brand-agnostic core.
  - `driver.py` — the `TvDriver` contract (`pair`, `list_inputs`, `set_input`, `reachable`, plus optional `discover`) plus the registry (`build_registry`/`list_brands`/`select_driver`).
  - `store.py` — JSON-persisted state: paired TVs (`{host, name, brand, creds, mac?, inputs?}`), per-screen `rules`, and the last-selected TV. `creds` is whatever opaque JSON the driver's `pair` returned — the core never inspects it. `set_inputs` also repoints any rule whose cached input no longer exists.
  - `edid.py` — `connected_displays()` reads `/sys/class/drm`; each display's `id` is its EDID monitor name (falling back to a vendor+product code). Rules key off this EDID identity, so a rule follows the **physical TV**, not an HDMI port.
  - `audio.py` — whether this machine is emitting sound, measured without a microphone: it captures the output sink's *monitor* (a loopback of what's being played) via `pw-record` (or `parec` on `@DEFAULT_MONITOR@`) and tracks the peak level in dBFS. Deliberately amplitude-based rather than device-state-based — see the keep-alive section below.
  - `wol.py` — Wake-on-LAN. MAC is learned from `/proc/net/arp` while the TV is awake (at pairing or any reachable moment) and backfilled into the store, so the TV can later be woken from standby.
- `backend/tv_driver_lg/` — the LG driver. `__init__.py` is the thin `TvDriver` subclass; `webos.py` is the LG SSAP-over-WebSocket client (it imports the vendored `websockets` lazily, inside `_open`, so the package stays importable under tests without it); `discover.py` is SSDP (UPnP M-SEARCH) LAN discovery — chosen over mDNS because every webOS TV claims the same `lgwebostv.local` hostname, while SSDP has each TV reply from its own IP. All LG-specific code stays here.
- `src/` — React UI. Generic: brand is just a dropdown populated from `list_brands`. `index.tsx` is the panel root; `api.ts` declares every backend RPC.
- `py_modules/websockets/` — the pure-Python `websockets` dependency, **vendored** (gitignored; produced by `scripts/vendor_python.sh`, pinned version inside that script). `main.py` adds `py_modules/` and `backend/` to `sys.path` at import time.

### The auto-switch loop (`Plugin._watch` in `main.py`)

A 5s poll diffs `connected_displays()` against the last seen set. Application is **level-driven, not edge-driven**: a newly-appeared display *queues* its rule (`_enqueue` → `self.pending`), and every subsequent poll re-attempts queued rules (`_drain` → `_attempt`) until the switch actually lands or a budget expires. This is deliberate — firing once on the appearance lost the switch whenever the network/TV wasn't ready in that one window, which is exactly the case on cold boot (Wi-Fi not up yet) and often on resume. A successful switch stamps `last_success[display_id]`; one attempt per display runs at a time (`self.inflight`). The tuned constants at the top of `main.py` exist for real hardware hazards — read their comments before changing them:

- `SETTLE_SECONDS` — wait out gamescope's own dock-time display reconfig before perturbing the HDMI link (switching too early can crash the Steam client). Applied as the `after` delay before the first attempt.
- `APPLY_BUDGET_SECONDS` — how long a queued rule keeps getting retried (each poll) before giving up. Covers slow Wi-Fi association on boot and slow TV wake.
- `COOLDOWN_SECONDS` — an input switch can make the link flap and look like the display reappearing; a re-appearance within this window of the last *successful* switch is ignored (debounce).
- Suspend/resume: the process is frozen during sleep, so the docked display never appears to "leave and return." `_suspended_seconds()` compares `CLOCK_BOOTTIME` vs `CLOCK_MONOTONIC`; a jump past `RESUME_THRESHOLD` means we resumed, so `seen`/`pending`/`last_success` are cleared to re-queue and re-apply rules on wake.
- Remote Play: auto-switch is paused while a session streams *from* the machine (the player is elsewhere in the house, so switching would grab the TV from whoever is watching it). Only the frontend can see `SteamClient.RemotePlay`, and Decky's RPC is frontend→backend only, so the backend can't ask at trigger time — `src/streaming.ts` pushes the state over `set_streaming` **on change only**, no polling or heartbeat. A missed "session stopped" would strand the pause, so three things undo it: `resyncStreaming()` on every panel open, `_watch` clearing the flag on resume (a session can't survive suspend), and the panel's `StreamingIndicator` making a live pause visible rather than silent. The flag is never persisted, so a plugin restart also fails open. Suppression is not deferral: rules skipped during a session are dropped, not replayed when it ends. The pause is checked in **two** places, because clearing the queue can't cancel an attempt already in flight — `_wake` alone can hold one for `WAKE_TIMEOUT` — so `_attempt` re-checks immediately before `set_input`.
- `_wake` Wake-on-LANs an unreachable TV (burst of magic packets) and waits up to `WAKE_TIMEOUT` for the control API; it opportunistically re-resolves the MAC from ARP if one was never learned. A TV with no resolvable MAC, or wake-over-LAN disabled, simply won't wake.

### The audio keep-alive (`Plugin._keep_audio_awake` in `main.py`)

An ARC soundbar drops into standby once the line has been silent for a while, and the first sound
after that is clipped while the audio path wakes back up. Riding the same 5s poll, this nudges the
TV with a `volume_up`/`volume_down` pair after a configurable stretch of silence: net-zero on the
volume, but the TV relays both to the soundbar over CEC, which wakes it. Off by default — each
nudge flashes the TV's volume OSD.

- **Amplitude, not device state.** `/proc/asound`'s PCM state and a PipeWire node's RUNNING/IDLE
  only say a stream is *connected*. PipeWire keeps pushing digital silence while a game sits at a
  quiet menu, so both read "active" at exactly the moment the soundbar has decided the line is
  silent. Hence the monitor capture and a dBFS floor.
- **dBFS, not dB.** There is no absolute loudness on this side of the HDMI cable — a soundbar's own
  scale depends on its volume knob — so the floor (default −50 dBFS) is calibrated against real
  gear, not converted. `scripts/audio_probe.py` prints the live level for that; the panel shows the
  same reading next to the slider. True digital silence sits below −90, quiet ambience near −60.
- **The capture is scoped.** Holding a capture stream stops PipeWire from suspending the sink, so
  `_keep_audio_awake` starts it only while the setting is on and a TV is attached, and stops it
  while a Remote Play session runs. A default install spawns no subprocess.
- **`silent_seconds()` returns `None` for "don't know"** (no capture, no PipeWire, no tool) — never
  read as silence, so a broken capture can't nudge.
- **Never `_wake`.** Unlike an input rule, this fires on its own schedule; the nudge is skipped
  entirely unless the TV is already reachable. Powering on a TV the user deliberately turned off
  would be a far worse misfire than a clipped sound.
- `NUDGE_COOLDOWN` paces repeats while the silence continues, and also paces retries against an
  unreachable TV — the attempt is stamped, not just the success. `NUDGE_GAP` separates the two
  volume commands so the TV doesn't coalesce them into one event.
- Resume clears the silence clock alongside `seen`/`pending`/`last_success`: the capture was frozen
  too, so the elapsed "silence" is an artifact of sleeping rather than an observation.

## Adding a new brand

1. Create `backend/tv_driver_<brand>/` with a `TvDriver` subclass implementing `pair`, `list_inputs`, `set_input`, `reachable`, plus a unique `name` (stable id stored on each TV) and `label` (UI text). Optional: `discover` (LAN auto-discovery) and the `power_off`/`volume_up`/`volume_down` commands. The optional *queries* default to an honest answer (`reachable` → False, `discover` → `[]`); the optional *commands* default to raising `NotImplementedError`, since returning quietly would make the UI report a power-off or volume change that never happened.
2. Inject it in `main.py`: `build_registry([LgDriver(), <Brand>Driver()])`.

Nothing in `tv_core` or `src/` changes — the brand appears in the UI dropdown automatically.

## Releases

Pushing to `main` triggers `.github/workflows/release.yml`, which runs `.github/scripts/determine_version.py` to compute the next semver from **conventional commits** since the latest tag, then tags and publishes a GitHub release with the built zip. Commit-type → bump: `ci`/`doc`/`docs` skipped; `chore`/`fix` → patch; anything else → minor; `!` or `BREAKING CHANGE:` → major. A non-conventional subject is ignored for versioning. If no commit warrants a bump, nothing is released. The release commit/tag is made detached (off `main`) so the branch isn't advanced.
