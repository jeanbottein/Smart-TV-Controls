"""Unit tests for the auto-switch queue — which displays get queued, when an attempt may run,
and the post-switch debounce — and for the audio keep-alive's decision to nudge. Pure
bookkeeping: no store, driver, network, or audio capture."""

import asyncio
import types

import pytest

import main
from main import APPLY_BUDGET_SECONDS, COOLDOWN_SECONDS, NUDGE_COOLDOWN


class FakeMonitor:
    """Stands in for SilenceMonitor: no subprocess, just a settable silence reading."""

    def __init__(self, silent=None):
        self.silent = silent
        self.started = False
        self.threshold = None
        self.rearms = 0
        self.tracker = types.SimpleNamespace(mark_sound=self._rearm, last_peak_dbfs=-90.0)

    def _rearm(self):
        self.rearms += 1
        self.silent = 0

    def configure(self, dbfs):
        self.threshold = dbfs

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def silent_seconds(self):
        return self.silent


class FakeStore:
    def __init__(self, rules, pause_when_streaming=True, triggers=(), keepalive=None, selected=""):
        self.rules = rules
        self.pause_when_streaming = pause_when_streaming
        self._triggers = dict(triggers)
        self.audio_keepalive = {"enabled": False, "seconds": 240, "dbfs": -50, **(keepalive or {})}
        self.selected = selected
        self.tvs = []

    def trigger_enabled(self, name):
        return self._triggers.get(name, True)

    def find_tv(self, host):
        return next((tv for tv in self.tvs if tv["host"] == host), None)


def _rule(display_id, enabled=True):
    return {"display_id": display_id, "host": "tv.lan", "input_id": "HDMI_1", "enabled": enabled}


@pytest.fixture
def plugin():
    instance = main.Plugin()
    instance.pending = {}
    instance.last_success = {}
    instance.seen = set()
    instance.inflight = set()
    instance.streaming = False
    instance._resumed = False
    instance.store = FakeStore([])
    instance.audio = FakeMonitor()
    instance.last_nudge = None
    instance.nudging = False
    instance.tasks = set()
    return instance


@pytest.fixture
def one_connected_display(monkeypatch):
    monkeypatch.setattr(main, "connected_displays", lambda: [{"id": "HDMI-1"}])


def test_queue_records_the_requested_time_and_a_budget_deadline(plugin):
    plugin._queue("HDMI-1", after=100, now=90)
    assert plugin.pending["HDMI-1"] == {"after": 100, "deadline": 90 + APPLY_BUDGET_SECONDS}


def test_queue_never_pulls_an_existing_attempt_earlier(plugin):
    plugin._queue("HDMI-1", after=100, now=90)
    plugin._queue("HDMI-1", after=95, now=90)
    assert plugin.pending["HDMI-1"]["after"] == 100


def test_queue_defers_an_existing_attempt_when_asked_to_wait_longer(plugin):
    plugin._queue("HDMI-1", after=100, now=90)
    plugin._queue("HDMI-1", after=120, now=90)
    assert plugin.pending["HDMI-1"]["after"] == 120


def test_queue_extends_the_deadline_on_requeue(plugin):
    plugin._queue("HDMI-1", after=100, now=90)
    plugin._queue("HDMI-1", after=100, now=150)
    assert plugin.pending["HDMI-1"]["deadline"] == 150 + APPLY_BUDGET_SECONDS


def test_a_display_never_switched_is_not_in_cooldown(plugin):
    assert plugin._in_cooldown("HDMI-1", now=0) is False


def test_a_display_switched_just_now_is_in_cooldown(plugin):
    plugin.last_success["HDMI-1"] = 100
    assert plugin._in_cooldown("HDMI-1", now=100 + COOLDOWN_SECONDS - 1) is True


def test_cooldown_expires_after_the_full_window(plugin):
    plugin.last_success["HDMI-1"] = 100
    assert plugin._in_cooldown("HDMI-1", now=100 + COOLDOWN_SECONDS) is False


def test_enabled_displays_returns_connected_displays_with_an_enabled_rule(plugin):
    plugin.store = FakeStore([_rule("HDMI-1"), _rule("HDMI-2")])
    assert plugin._enabled_displays({"HDMI-1", "HDMI-2"}) == ["HDMI-1", "HDMI-2"]


def test_enabled_displays_skips_a_disabled_rule(plugin):
    plugin.store = FakeStore([_rule("HDMI-1", enabled=False), _rule("HDMI-2")])
    assert plugin._enabled_displays({"HDMI-1", "HDMI-2"}) == ["HDMI-2"]


def test_enabled_displays_skips_a_disconnected_display(plugin):
    plugin.store = FakeStore([_rule("HDMI-1"), _rule("HDMI-2")])
    assert plugin._enabled_displays({"HDMI-2"}) == ["HDMI-2"]


def test_enabled_displays_is_empty_without_rules(plugin):
    assert plugin._enabled_displays({"HDMI-1"}) == []


def test_not_paused_while_nothing_is_streaming(plugin):
    assert plugin._paused_by_streaming() is False


def test_paused_while_a_session_streams_from_this_machine(plugin):
    asyncio.run(plugin.set_streaming(True))
    assert plugin._paused_by_streaming() is True


def test_the_session_ending_resumes_auto_switch(plugin):
    asyncio.run(plugin.set_streaming(True))
    asyncio.run(plugin.set_streaming(False))
    assert plugin._paused_by_streaming() is False


def test_never_paused_when_the_setting_is_off(plugin):
    plugin.store = FakeStore([], pause_when_streaming=False)
    asyncio.run(plugin.set_streaming(True))
    assert plugin._paused_by_streaming() is False


def test_the_indicator_reports_the_pause(plugin, one_connected_display):
    plugin.store = FakeStore([_rule("HDMI-1")])
    asyncio.run(plugin.set_streaming(True))
    assert asyncio.run(plugin.auto_switch_paused()) is True


def test_the_indicator_stays_quiet_when_no_display_is_connected(plugin, monkeypatch):
    monkeypatch.setattr(main, "connected_displays", lambda: [])
    plugin.store = FakeStore([_rule("HDMI-1")])
    asyncio.run(plugin.set_streaming(True))
    assert asyncio.run(plugin.auto_switch_paused()) is False


def test_the_indicator_stays_quiet_when_no_rule_could_have_fired(plugin):
    asyncio.run(plugin.set_streaming(True))
    assert asyncio.run(plugin.auto_switch_paused()) is False


def test_the_indicator_stays_quiet_when_every_rule_is_disabled(plugin):
    plugin.store = FakeStore([_rule("HDMI-1", enabled=False)])
    asyncio.run(plugin.set_streaming(True))
    assert asyncio.run(plugin.auto_switch_paused()) is False


def test_the_first_poll_after_a_resume_reads_as_the_wake_trigger(plugin):
    plugin._resumed = True
    assert plugin._consume_trigger_reason() == "wake"


def test_the_resume_flag_is_consumed_once(plugin):
    plugin._resumed = True
    plugin._consume_trigger_reason()
    assert plugin._consume_trigger_reason() == "connect"


def test_an_appearing_display_is_queued_when_nothing_is_streaming(plugin, one_connected_display):
    plugin.store = FakeStore([_rule("HDMI-1")])
    asyncio.run(plugin._apply_rules())
    assert "HDMI-1" in plugin.pending


def test_a_streaming_session_drops_the_queue_instead_of_switching(plugin, one_connected_display):
    plugin.store = FakeStore([_rule("HDMI-1")])
    plugin.pending = {"HDMI-1": {"after": 0, "deadline": 1e9}}
    asyncio.run(plugin.set_streaming(True))
    asyncio.run(plugin._apply_rules())
    assert plugin.pending == {}


@pytest.fixture
def switched(plugin, monkeypatch):
    """Make _attempt runnable off-device: a paired TV, no network, and a record of every
    set_input that actually reached a driver."""
    calls = []

    class FakeDriver:
        async def set_input(self, host, creds, input_id):
            calls.append(input_id)
            return True

    plugin.store.find_tv = lambda host: {"host": host, "name": "TV", "brand": "lg", "creds": {}}
    monkeypatch.setattr(main, "select_driver", lambda registry, brand: FakeDriver())
    return calls


def test_an_attempt_switches_when_nothing_is_streaming(plugin, switched):
    plugin.pending = {"HDMI-1": {"after": 0, "deadline": 1e9}}
    plugin._wake = _always_wakes
    asyncio.run(plugin._attempt(_rule("HDMI-1"), "HDMI-1"))
    assert switched == ["HDMI_1"]


def test_a_session_starting_mid_attempt_stops_the_switch_before_it_lands(plugin, switched):
    """_wake can hold an attempt for WAKE_TIMEOUT. A session starting inside that window clears
    the queue, but cannot cancel the already-spawned task — so the attempt must re-check."""
    plugin.pending = {"HDMI-1": {"after": 0, "deadline": 1e9}}

    async def wake_then_stream(tv):
        await plugin.set_streaming(True)  # the session starts while we wait on the TV
        return True

    plugin._wake = wake_then_stream
    asyncio.run(plugin._attempt(_rule("HDMI-1"), "HDMI-1"))
    assert switched == []
    assert plugin.pending == {}  # dropped, not deferred


async def _always_wakes(tv):
    return True


def test_a_paused_poll_clears_the_resume_flag_cleanly(plugin, one_connected_display):
    plugin._resumed = True
    asyncio.run(plugin.set_streaming(True))
    asyncio.run(plugin._apply_rules())
    assert plugin._resumed is False


def test_ending_streaming_resumes_normal_polling_for_new_appearances(plugin, monkeypatch):
    monkeypatch.setattr(main, "connected_displays", lambda: [])
    plugin.store = FakeStore([_rule("HDMI-1")])
    asyncio.run(plugin.set_streaming(True))
    asyncio.run(plugin._apply_rules())  # poll while streaming (no displays connected)

    # Streaming ends and a new display connects
    asyncio.run(plugin.set_streaming(False))
    monkeypatch.setattr(main, "connected_displays", lambda: [{"id": "HDMI-1"}])
    asyncio.run(plugin._apply_rules())
    assert "HDMI-1" in plugin.pending


# --- audio keep-alive -------------------------------------------------------------------------


def _tv(host="tv.lan"):
    return {"host": host, "name": "TV", "brand": "lg", "creds": {}}


@pytest.fixture
def keepalive(plugin, monkeypatch):
    """A plugin with the feature on, a connected screen whose rule points at a paired TV, and
    _spawn replaced by a recorder — so _keep_audio_awake's decision is observable without a
    running event loop."""
    plugin.store = FakeStore([_rule("HDMI-1")], keepalive={"enabled": True})
    plugin.store.tvs = [_tv()]
    plugin.seen = {"HDMI-1"}
    plugin.audio = FakeMonitor(silent=0)
    spawned = []

    def record(coro):
        spawned.append(coro)
        coro.close()  # never scheduled, so close it rather than leak a pending coroutine

    plugin._spawn = record
    return spawned


def test_the_capture_runs_while_the_feature_is_on_and_a_tv_is_attached(plugin, keepalive):
    plugin._keep_audio_awake()
    assert plugin.audio.started is True


def test_the_configured_floor_reaches_the_monitor(plugin, keepalive):
    plugin.store.audio_keepalive["dbfs"] = -62
    plugin._keep_audio_awake()
    assert plugin.audio.threshold == -62


def test_the_capture_stays_off_while_the_feature_is_off(plugin, keepalive):
    plugin.store.audio_keepalive["enabled"] = False
    plugin._keep_audio_awake()
    assert plugin.audio.started is False


def test_the_capture_stops_while_a_session_streams_from_this_machine(plugin, keepalive):
    """Whoever is streaming isn't at the TV, so the soundbar may as well sleep — and the sink
    shouldn't be pinned open for nobody."""
    plugin.audio.started = True
    asyncio.run(plugin.set_streaming(True))
    plugin._keep_audio_awake()
    assert plugin.audio.started is False


def test_the_capture_stops_when_no_tv_can_be_resolved(plugin, keepalive):
    plugin.store.tvs = []
    plugin.audio.started = True
    plugin._keep_audio_awake()
    assert plugin.audio.started is False


def test_no_nudge_before_the_silence_budget_elapses(plugin, keepalive):
    plugin.audio.silent = 239
    plugin._keep_audio_awake()
    assert keepalive == []


def test_a_nudge_fires_once_the_silence_budget_elapses(plugin, keepalive):
    plugin.audio.silent = 240
    plugin._keep_audio_awake()
    assert len(keepalive) == 1


def test_no_nudge_while_the_output_level_is_unmeasured(plugin, keepalive):
    """A capture that hasn't started (or just died) reports None — which is "don't know", not
    "silent", and must never be read as a reason to nudge."""
    plugin.audio.silent = None
    plugin._keep_audio_awake()
    assert keepalive == []


def test_a_shorter_configured_budget_nudges_sooner(plugin, keepalive):
    plugin.store.audio_keepalive["seconds"] = 60
    plugin.audio.silent = 61
    plugin._keep_audio_awake()
    assert len(keepalive) == 1


def test_the_nudge_does_not_repeat_inside_the_cooldown(plugin, keepalive):
    plugin.audio.silent = 240
    plugin._keep_audio_awake()
    plugin.nudging = False  # the first nudge finished, but the cooldown still stands
    plugin._keep_audio_awake()
    assert len(keepalive) == 1


def test_the_nudge_repeats_once_the_cooldown_expires(plugin, keepalive):
    plugin.audio.silent = 240
    plugin._keep_audio_awake()
    plugin.nudging = False
    plugin.last_nudge -= NUDGE_COOLDOWN
    plugin._keep_audio_awake()
    assert len(keepalive) == 2


def test_no_second_nudge_while_one_is_still_in_flight(plugin, keepalive):
    plugin.audio.silent = 240
    plugin.nudging = True
    plugin._keep_audio_awake()
    assert keepalive == []


def test_the_target_is_the_tv_the_connected_screen_rule_points_at(plugin, keepalive):
    plugin.store.tvs = [_tv("rule.lan"), _tv("selected.lan")]
    plugin.store.rules = [{**_rule("HDMI-1"), "host": "rule.lan"}]
    plugin.store.selected = "selected.lan"
    assert plugin._nudge_target()["host"] == "rule.lan"


def test_the_target_falls_back_to_the_selected_tv_without_a_matching_rule(plugin, keepalive):
    plugin.store.rules = []
    plugin.store.tvs = [_tv("selected.lan")]
    plugin.store.selected = "selected.lan"
    assert plugin._nudge_target()["host"] == "selected.lan"


def test_a_rule_for_a_disconnected_screen_does_not_pick_its_tv(plugin, keepalive):
    plugin.seen = set()
    plugin.store.tvs = [_tv("rule.lan")]
    plugin.store.selected = ""
    assert plugin._nudge_target() is None


@pytest.fixture
def nudged(plugin, monkeypatch):
    """Run _nudge against a fake driver, recording the commands that reached it."""
    calls = []

    class FakeDriver:
        def __init__(self, reachable=True):
            self._reachable = reachable

        async def reachable(self, host):
            return self._reachable

        async def volume_up(self, host, creds):
            calls.append("up")

        async def volume_down(self, host, creds):
            calls.append("down")

    monkeypatch.setattr(main, "NUDGE_GAP", 0)
    plugin.audio = FakeMonitor(silent=300)
    plugin.nudging = True
    plugin._driver = FakeDriver()
    monkeypatch.setattr(main, "select_driver", lambda registry, brand: plugin._driver)
    return calls, FakeDriver


def test_a_nudge_sends_volume_up_then_down(plugin, nudged):
    calls, _ = nudged
    asyncio.run(plugin._nudge(_tv(), 300))
    assert calls == ["up", "down"]


def test_a_nudge_rearms_the_silence_clock_so_the_next_window_starts_now(plugin, nudged):
    asyncio.run(plugin._nudge(_tv(), 300))
    assert plugin.audio.rearms == 1


def test_an_unreachable_tv_is_left_alone_and_never_woken(plugin, nudged):
    """This fires on its own schedule, so waking a TV the user deliberately turned off would be
    a far worse misfire than a clipped sound."""
    calls, FakeDriver = nudged
    plugin._driver = FakeDriver(reachable=False)
    plugin._wake = _never_called
    asyncio.run(plugin._nudge(_tv(), 300))
    assert calls == []
    assert plugin.audio.rearms == 0


def test_the_in_flight_flag_clears_after_a_nudge(plugin, nudged):
    asyncio.run(plugin._nudge(_tv(), 300))
    assert plugin.nudging is False


def test_the_in_flight_flag_clears_even_when_the_tv_fails_mid_command(plugin, nudged, monkeypatch):
    class Broken:
        async def reachable(self, host):
            return True

        async def volume_up(self, host, creds):
            raise ConnectionError("socket closed")

    monkeypatch.setattr(main, "select_driver", lambda registry, brand: Broken())
    asyncio.run(plugin._nudge(_tv(), 300))
    assert plugin.nudging is False


async def _never_called(tv):
    raise AssertionError("the keep-alive must never wake a TV")
