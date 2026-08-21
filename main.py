import asyncio
import logging
import os
import sys
import time

import decky

ROOT = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))

from tv_core.audio import SilenceMonitor
from tv_core.driver import build_registry, list_brands, select_driver
from tv_core.edid import connected_displays
from tv_core.logs import clear_logs, prune_logs, read_log_tail
from tv_core.store import Store
from tv_core.wol import is_valid_mac, resolve_mac, send_magic_packet
from tv_driver_lg import LgDriver

POLL_SECONDS = 5
LOG_TAIL_LINES = 200
# Let gamescope finish its own dock-time display reconfiguration before we perturb
# the HDMI link by switching inputs — doing both at once can crash the Steam client.
# Lower = snappier switch but closer to that collision window; 5s leaves some margin.
SETTLE_SECONDS = 5
# An input switch can make the TV briefly drop and re-add the HDMI link, which looks
# like the display reappearing. Ignore a display we just acted on for this long so a
# flapping link can't machine-gun switches.
COOLDOWN_SECONDS = 60
# A newly-appeared display usually isn't actionable the instant it shows up: on cold boot
# the machine's Wi-Fi isn't connected yet, and a TV woken from standby needs a moment before
# its control API answers. So rather than fire once and give up (which loses the switch
# whenever the network or TV is a beat slow — the cause of flaky dock-on-boot/resume), we
# keep re-attempting the rule on each poll until the switch lands or this budget elapses.
APPLY_BUDGET_SECONDS = 180
# When a TV is asleep, Wake-on-LAN it and wait this long for the control API to boot.
# A TV in standby answers in a few seconds; one that powered itself fully off (LG TVs
# do this after hours with no signal) needs a whole cold boot, so the budget is wide.
WAKE_TIMEOUT = 60
WAKE_POLL = 2
# A single magic packet can be dropped, and a cold NIC may miss the first few, so send
# a small burst each cycle rather than relying on one packet landing.
WAKE_BURST = 3
# Suspend freezes this whole process, so the poll loop never observes the docked
# display "disappear" and re-appear across a sleep — meaning rules would never fire on
# wake. CLOCK_BOOTTIME counts suspended time while CLOCK_MONOTONIC does not, so a jump
# in their difference larger than this (seconds) means we just resumed and should
# forget what we've seen and re-evaluate the connected displays from scratch.
RESUME_THRESHOLD = 10
# An ARC soundbar drops into standby once the line has been silent for a while, and the first
# sound after that is clipped while the audio path wakes back up. A volumeUp/volumeDown pair is
# net-zero on the volume, but the TV relays both to the soundbar over CEC, which wakes it. How
# long silence must last (and how quiet counts as silent) is a per-setup judgement, so both live
# in the store — see tv_core.audio for the defaults and what a dBFS level means.
#
# Don't let the nudge repeat faster than this while the silence goes on: each one flashes the
# TV's volume OSD, and this also paces the retries when the TV can't be reached.
NUDGE_COOLDOWN = 240
# The TV has to see two distinct volume events; sent back to back they can coalesce into one.
NUDGE_GAP = 1.0

# Keep the vendored websockets library from writing chatty connection logs.
logging.getLogger("websockets").setLevel(logging.WARNING)

REGISTRY = build_registry([LgDriver()])

# Auto-switch trigger toggles, persisted per-name in the store (default on). "connect" = a
# screen appears (docking or booting up docked); "wake" = it re-appears after resume; "home"
# = the Steam-button re-assert. Disabling one suppresses just that path.
TRIGGERS = ("connect", "wake", "home")


class Plugin:
    async def _main(self):
        prune_logs(decky.DECKY_PLUGIN_LOG_DIR)
        settings_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "deckatv.json")
        self.store = Store(settings_path)
        self.seen = set()
        self.pending = {}  # display_id -> {after, deadline}: rules awaiting a successful switch
        self.inflight = set()  # display_ids with an attempt running (one at a time per display)
        self.last_success = {}  # display_id -> monotonic time of the last successful switch
        self.tasks = set()  # spawned attempts, kept alive and cancellable on unload
        self._resumed = False  # set when _watch detects a resume; tags the next poll as "wake"
        # Whether a Remote Play session is streaming from this machine. Only the frontend can see
        # SteamClient, so it pushes this on change (see src/streaming.ts). Never persisted, and
        # cleared on resume, so every way of losing track of it fails *open* — auto-switch comes
        # back rather than staying disabled for good.
        self.streaming = False
        # Measures whether this machine is emitting sound, so a long silence can nudge the TV's
        # volume and keep an ARC soundbar awake. Started lazily by _keep_audio_awake, so a
        # default install (the setting is off) never spawns the capture.
        self.audio = SilenceMonitor()
        self.last_nudge = None  # monotonic time of the last nudge *attempt*
        self.nudging = False  # a nudge is in flight (it outlives a poll)
        self.watcher = asyncio.get_event_loop().create_task(self._watch())

    async def _unload(self):
        watcher = getattr(self, "watcher", None)
        if watcher is not None:
            watcher.cancel()
        audio = getattr(self, "audio", None)
        if audio is not None:
            audio.stop()
        for task in list(getattr(self, "tasks", ())):
            task.cancel()

    async def list_brands(self):
        return list_brands(REGISTRY)

    async def list_tvs(self):
        return self.store.tvs

    async def get_selected_tv(self):
        return self.store.selected

    async def set_selected_tv(self, host: str):
        self.store.set_selected(host)

    async def list_rules(self):
        return self.store.rules

    async def get_triggers(self):
        return {name: self.store.trigger_enabled(name) for name in TRIGGERS}

    async def set_trigger(self, name: str, enabled: bool):
        self.store.set_trigger(name, enabled)

    async def set_streaming(self, active: bool):
        """The frontend reporting whether a Remote Play session is streaming from this machine."""
        self.streaming = active

    async def auto_switch_paused(self):
        """What the panel's indicator shows. Needs an enabled rule matching a connected display,
        or the indicator would claim a pause on an install where nothing was going to switch anyway."""
        if not self._paused_by_streaming():
            return False
        present = await asyncio.to_thread(
            lambda: {display["id"] for display in connected_displays()}
        )
        return bool(self._enabled_displays(present))

    async def get_pause_when_streaming(self):
        return self.store.pause_when_streaming

    async def set_pause_when_streaming(self, enabled: bool):
        self.store.set_pause_when_streaming(enabled)

    def _paused_by_streaming(self):
        """Whether a live Remote Play session currently holds the TV off-limits."""
        return self.streaming and self.store.pause_when_streaming

    async def get_audio_keepalive(self):
        return self.store.audio_keepalive

    async def set_audio_keepalive(self, enabled: bool, seconds: int, dbfs: int):
        self.store.set_audio_keepalive(enabled, seconds, dbfs)

    async def get_audio_status(self):
        """What the panel's readout shows, so the silence floor can be calibrated against the
        real hardware: whether a capture is live, why not when it isn't, the most recent peak
        level, and how long the output has been under the floor."""
        return {
            "running": self.audio.running,
            "unavailable": self.audio.unavailable,
            "silent_seconds": self.audio.silent_seconds(),
            "peak_dbfs": round(self.audio.tracker.last_peak_dbfs, 1),
        }

    async def get_notifications(self):
        return self.store.notifications

    async def set_notifications(self, enabled: bool):
        self.store.set_notifications(enabled)

    async def list_displays(self):
        return connected_displays()

    async def read_logs(self):
        return read_log_tail(decky.DECKY_PLUGIN_LOG_DIR, LOG_TAIL_LINES)

    async def clear_logs(self):
        clear_logs(decky.DECKY_PLUGIN_LOG_DIR)

    async def is_reachable(self, host: str):
        tv = self.store.find_tv(host)
        if tv is None:
            return False
        try:
            return await select_driver(REGISTRY, tv["brand"]).reachable(host)
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False

    async def get_inputs(self, host: str):
        """Return cached inputs immediately when offline; refresh the cache when online."""
        tv = self.store.find_tv(host)
        if tv is None:
            return []
        driver = select_driver(REGISTRY, tv["brand"])
        try:
            if not await driver.reachable(host):
                return self.store.cached_inputs(host)
            inputs = await driver.list_inputs(host, tv["creds"])
        except Exception:  # noqa: BLE001 - fall back to the last known list
            return self.store.cached_inputs(host)
        if inputs:
            self.store.set_inputs(host, inputs)
            if not tv.get("mac"):  # backfill the MAC for Wake-on-LAN while the TV is up
                mac = await asyncio.to_thread(resolve_mac, host)
                if mac:
                    self.store.set_mac(host, mac)
            return inputs
        return self.store.cached_inputs(host)

    async def discover_tvs(self, brand: str):
        """Auto-discover TVs on the LAN for `brand`; [] if the brand can't or it fails."""
        try:
            return await select_driver(REGISTRY, brand).discover()
        except Exception:  # noqa: BLE001 - discovery is best-effort; the UI still allows manual entry
            return []

    async def pair_tv(self, host: str, name: str, brand: str):
        creds = await select_driver(REGISTRY, brand).pair(host)
        label = name or host
        mac = await asyncio.to_thread(resolve_mac, host)
        self.store.upsert_tv(host, label, brand, creds, mac)
        return {"host": host, "name": label, "brand": brand}

    async def remove_tv(self, host: str):
        self.store.remove_tv(host)

    async def switch_input(self, host: str, input_id: str):
        await self._set_input(self.store.find_tv(host), input_id)

    async def power_off_tv(self, host: str):
        """Power the TV off now (a manual press, so unconditional). The caller (the panel) runs
        this while the machine is awake and the network is up, so it actually reaches the TV — unlike
        hooking suspend, where the Wi-Fi is torn down before we could send anything."""
        tv, driver = self._resolve(host)
        await driver.power_off(tv["host"], tv["creds"])
        decky.logger.info(f"powered off {tv['name']}")

    async def volume_up(self, host: str):
        tv, driver = self._resolve(host)
        await driver.volume_up(tv["host"], tv["creds"])

    async def volume_down(self, host: str):
        tv, driver = self._resolve(host)
        await driver.volume_down(tv["host"], tv["creds"])

    def _resolve(self, host):
        """The paired TV and its driver, for commands that must fail loudly when it's unknown."""
        tv = self.store.find_tv(host)
        if tv is None:
            raise ValueError("unknown TV")
        return tv, select_driver(REGISTRY, tv["brand"])

    async def reapply_rules(self):
        """Re-assert the input rule for every currently-connected display — the equivalent
        of a console's "One Touch Play" when you press the controller's home button.

        Unlike the watch loop this ignores the appearance gate and the post-switch cooldown,
        since the press is explicit intent. It still honors the gamescope settle delay for a
        display we haven't seen yet (a dock-time reconfig may be in flight, and switching
        mid-reconfig can crash the Steam client) and never pulls an already-queued attempt
        earlier than the watch loop scheduled it; a steady, already-seen display switches
        right away. The driver no-ops when the TV is already on the target input, so pressing
        repeatedly is cheap. Reuses the drain/attempt path, so retries, wake, and the
        one-attempt-per-display guard all still apply. A live Remote Play session still wins:
        that press comes from a remote controller, so its user isn't at the TV either."""
        if not self.store.trigger_enabled("home") or self._paused_by_streaming():
            return
        now = time.monotonic()
        present = {display["id"] for display in connected_displays()}
        # A display we haven't seen yet may still be mid dock-reconfig, so give it the settle
        # delay. Fold present into `seen` so _drain — which only acts on seen displays — fires.
        appeared = present - self.seen
        self.seen |= present
        queued = self._enabled_displays(present)
        if not queued:
            return
        for display_id in queued:
            self._queue(display_id, now + SETTLE_SECONDS if display_id in appeared else now, now)
        decky.logger.info(f"reapply requested for {queued}")
        self._drain(now)

    def _enabled_displays(self, present):
        """Display ids with an enabled rule that are currently connected."""
        return [
            rule["display_id"]
            for rule in self.store.rules
            if rule.get("enabled") and rule["display_id"] in present
        ]

    async def set_rule(self, display_id: str, host: str, input_id: str, enabled: bool):
        self.store.set_rule(display_id, host, input_id, enabled)

    async def remove_rule(self, display_id: str):
        self.store.remove_rule(display_id)

    async def _set_input(self, tv, input_id):
        if tv is None:
            raise ValueError("unknown TV")
        if not await self._wake(tv):
            raise ConnectionError(f"{tv['name']} is unreachable and would not wake")
        await select_driver(REGISTRY, tv["brand"]).set_input(tv["host"], tv["creds"], input_id)

    async def _wake(self, tv):
        """Ensure the TV's control API is up, Wake-on-LAN-ing it from standby/off first.

        Returns True once it's reachable, False if it never came up within the budget
        (e.g. the TV is unplugged, or "Mobile TV On" / wake-over-LAN is disabled on it).
        """
        driver = select_driver(REGISTRY, tv["brand"])
        if await driver.reachable(tv["host"]):
            return True
        mac = tv.get("mac")
        if not mac:  # never learned it (e.g. paired while offline) — the ARP table may be ready now
            mac = await asyncio.to_thread(resolve_mac, tv["host"])
            if mac and is_valid_mac(mac):
                self.store.set_mac(tv["host"], mac)
        if not mac or not is_valid_mac(mac):  # can't wake without a usable MAC
            return False
        deadline = time.monotonic() + WAKE_TIMEOUT
        while True:
            for _ in range(WAKE_BURST):
                send_magic_packet(mac)
            await asyncio.sleep(WAKE_POLL)
            if await driver.reachable(tv["host"]):
                return True
            if time.monotonic() >= deadline:
                return False

    async def _watch(self):
        last_error = None
        suspended = self._suspended_seconds()
        while True:
            now_suspended = self._suspended_seconds()
            if now_suspended - suspended > RESUME_THRESHOLD:
                # We just woke from sleep. The display never looked like it left, so
                # forget it (and any cooldown) and let this poll re-apply the rules.
                decky.logger.info("resume detected; re-evaluating displays")
                self.seen = set()
                self.pending.clear()
                self.last_success.clear()
                # A Remote Play session can't survive the machine suspending, so any flag still set
                # is stale. Clearing it here is the safety net for a missed "session stopped".
                self.streaming = False
                self._resumed = True  # tag the next poll's re-appearances as the "wake" trigger
                # The capture was frozen too, so the silence it "measured" across the sleep is an
                # artifact rather than an observation — don't nudge on the strength of it.
                self.audio.tracker.mark_sound()
                self.last_nudge = None
            suspended = now_suspended
            last_error = await self._poll(last_error)
            try:
                self._keep_audio_awake()
            except Exception as error:  # noqa: BLE001 - a side feature must never stop the loop
                decky.logger.warning(f"audio keep-alive failed: {error}")
            await asyncio.sleep(POLL_SECONDS)

    @staticmethod
    def _suspended_seconds():
        # CLOCK_BOOTTIME includes time spent suspended; CLOCK_MONOTONIC does not. Their
        # difference is exactly how long the system has been asleep since boot.
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    async def _poll(self, last_error):
        """Apply rules once; log a persistent failure only when it changes."""
        try:
            await self._apply_rules()
            return None
        except Exception as error:  # noqa: BLE001 - never let the loop die
            message = str(error)
            if message != last_error:
                decky.logger.warning(f"auto-switch poll failed: {message}")
            return message

    async def _apply_rules(self):
        # Level-driven, not edge-driven: an appearance *queues* a rule, and we keep
        # attempting queued rules every poll until the switch actually lands. Firing once on
        # the appearance lost the switch whenever the network or TV wasn't ready in that one
        # window — exactly the case on cold boot, and often on resume.
        now = time.monotonic()
        appeared = self._take_appeared()
        if self._paused_by_streaming():
            # Someone is streaming from this machine, so they're not at the TV: leave its input
            # alone. Drop the queue too, or a rule queued moments before the session started
            # would still land mid-stream. `seen` stays current (it was just refreshed), so
            # ending the session doesn't make every display look newly docked. Consume the resume
            # flag as well so a wake that occurred during streaming is cleanly suppressed.
            self.pending.clear()
            self._resumed = False
            return
        reason = self._consume_trigger_reason()
        self._enqueue(appeared, now, reason)
        self._drain(now)

    def _consume_trigger_reason(self):
        """Why displays appeared this poll: "wake" right after a resume, "connect" otherwise
        (docking or booting up docked). Consumed only once the displays have been read, so a
        failed poll retries as "wake" rather than downgrading to "connect"."""
        reason = "wake" if self._resumed else "connect"
        self._resumed = False
        return reason

    def _take_appeared(self):
        present = {display["id"] for display in connected_displays()}
        appeared = present - self.seen
        self.seen = present
        return appeared

    def _enqueue(self, appeared, now, reason):
        """Queue a rule (for repeated attempts) when its display newly appears — unless the
        trigger for why it appeared (connect/wake) is switched off."""
        if not self.store.trigger_enabled(reason):
            return
        candidates = self._enabled_displays(appeared)
        for display_id in (did for did in candidates if not self._in_cooldown(did, now)):
            self._queue(display_id, now + SETTLE_SECONDS, now)

    def _in_cooldown(self, display_id, now):
        """Debounce the link flap a switch itself can cause: a re-appearance landing within
        COOLDOWN_SECONDS of this display's last *successful* switch is the flap, not a dock."""
        return now - self.last_success.get(display_id, -COOLDOWN_SECONDS) < COOLDOWN_SECONDS

    def _queue(self, did, after, now):
        """Queue a display for switch attempts until APPLY_BUDGET_SECONDS elapses, never pulling
        an already-queued attempt earlier than it was scheduled."""
        scheduled = self.pending.get(did)
        if scheduled is not None:
            after = max(after, scheduled["after"])
        self.pending[did] = {"after": after, "deadline": now + APPLY_BUDGET_SECONDS}

    def _drain(self, now):
        """Attempt each queued rule once per poll until it succeeds or its budget expires."""
        for rule in self.store.rules:
            did = rule["display_id"]
            slot = self.pending.get(did)
            if slot is None or did in self.inflight:
                continue
            if not rule.get("enabled"):  # disabled mid-budget — drop it from the queue
                self.pending.pop(did, None)
            elif now >= slot["deadline"]:
                self.pending.pop(did, None)
                decky.logger.info(f"auto-switch: gave up on {did} (not switchable within {APPLY_BUDGET_SECONDS}s)")
            elif did in self.seen and now >= slot["after"]:
                # Only switch while the display is actually connected, and only past the
                # gamescope settle delay. If it was undocked during settle we skip and let the
                # budget expire; a brief flap just defers the attempt to a later poll.
                self.inflight.add(did)
                self._spawn(self._attempt(rule, did))

    def _keep_audio_awake(self):
        """Nudge the TV's volume once this machine has been silent long enough for an ARC
        soundbar to have fallen asleep, so the next sound isn't clipped while the path wakes.

        Rides the same poll as the auto-switch, and owns the capture's lifecycle: it only runs
        while the setting is on and a TV is actually attached, because holding a capture stream
        keeps PipeWire from suspending the sink. Cheap and non-blocking — the nudge itself is
        spawned, since reaching the TV can take seconds and must not stall the switch loop.
        """
        settings = self.store.audio_keepalive
        target = self._nudge_target() if settings["enabled"] else None
        if target is None or self._paused_by_streaming():
            # Nothing to keep awake — or someone is streaming from this machine, so they aren't
            # at the TV and the soundbar may as well sleep. Drop the capture rather than pin the
            # sink open for nobody.
            self.audio.stop()
            return
        self.audio.configure(settings["dbfs"])
        self.audio.start()
        silent = self.audio.silent_seconds()
        if silent is None or silent < settings["seconds"] or self.nudging:
            return
        now = time.monotonic()
        if self.last_nudge is not None and now - self.last_nudge < NUDGE_COOLDOWN:
            return
        self.last_nudge = now  # stamped on the attempt, so an unreachable TV isn't retried every poll
        self.nudging = True
        self._spawn(self._nudge(target, silent))

    def _nudge_target(self):
        """The TV to keep awake: the one an enabled rule points at for a currently-connected
        screen, since that's the TV we know is attached to something live. Falls back to the
        selected TV for a setup with no rules at all.

        Reads `seen` rather than the sysfs displays: the poll that just ran refreshed it, so this
        adds no I/O to the loop."""
        for display_id in self._enabled_displays(self.seen):
            rule = next((r for r in self.store.rules if r["display_id"] == display_id), None)
            tv = self.store.find_tv(rule["host"]) if rule else None
            if tv is not None:
                return tv
        return self.store.find_tv(self.store.selected)

    async def _nudge(self, tv, silent):
        """One volume up/down round trip. Net-zero on the volume; the point is the CEC traffic
        it makes the TV emit."""
        try:
            driver = select_driver(REGISTRY, tv["brand"])
            # Only ever touch a TV that is already awake. This fires on its own schedule, so
            # never _wake() here — powering on a TV the user deliberately turned off, in a quiet
            # room, would be a far worse misfire than a clipped sound.
            if not await driver.reachable(tv["host"]):
                return
            await driver.volume_up(tv["host"], tv["creds"])
            await asyncio.sleep(NUDGE_GAP)
            await driver.volume_down(tv["host"], tv["creds"])
            # Re-arm: the next silence window starts now, not when the sound stopped.
            self.audio.tracker.mark_sound()
            decky.logger.info(f"audio keep-alive: nudged {tv['name']} after {int(silent)}s of silence")
        except Exception as error:  # noqa: BLE001 - the TV may drop mid-command; retry next window
            decky.logger.info(f"audio keep-alive nudge for {tv['name']} failed: {error}")
        finally:
            self.nudging = False

    def _spawn(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _attempt(self, rule, did):
        """One switch attempt. On success the rule leaves the queue; on any failure it stays
        queued and the next poll retries (within the budget), so a TV or network that becomes
        ready a moment later still gets switched."""
        try:
            tv = self.store.find_tv(rule["host"])
            if tv is None:
                self.pending.pop(did, None)
                return
            if not await self._wake(tv):
                return  # asleep/unreachable right now — leave it queued for the next poll
            if self._paused_by_streaming():
                # A session can start *while this attempt is running* — _wake alone can hold it
                # for WAKE_TIMEOUT. _apply_rules drops the queue when the pause begins, but it
                # cannot cancel an attempt already in flight, so this is the last check before we
                # touch the TV. Dropped rather than deferred, like any rule skipped mid-session.
                self.pending.pop(did, None)
                decky.logger.info(f"auto-switch: dropped {did}, a Remote Play session started")
                return
            driver = select_driver(REGISTRY, tv["brand"])
            changed = await driver.set_input(tv["host"], tv["creds"], rule["input_id"])
            self.last_success[did] = time.monotonic()
            self.pending.pop(did, None)
            # Only announce an actual change: the driver returns False when the TV was already on
            # this input, so pressing the Steam button while already docked stays quiet.
            if changed is not False:
                decky.logger.info(f"auto-switch: {tv['name']} -> {rule['input_id']}")
                await decky.emit("auto_switch", tv["name"], rule["input_id"])
        except Exception as error:  # noqa: BLE001 - SSAP may not be ready just after boot; retry next poll
            decky.logger.info(f"auto-switch attempt for {did} failed: {error}")
        finally:
            self.inflight.discard(did)
