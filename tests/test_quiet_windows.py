import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ticket_watcher.config import Config, QuietWindow, parse_config
from ticket_watcher.schedule import normal_next_check


def polling(windows):
    return parse_config({"polling": {"quiet_windows": windows}}, Path("."))


def test_quiet_windows_parse_compound_rules():
    config = polling(
        [
            {"start": "00:00", "end": "07:00", "interval_seconds": [10800, 12600]},
            {"start": "20:00", "end": "23:59", "interval_seconds": [2700, 3600]},
            {"start": "12:00", "end": "13:00", "pause": True},
        ]
    )
    assert config.quiet_windows == (
        QuietWindow(0, 420, (10800, 12600)),
        QuietWindow(1200, 0, (2700, 3600)),
        QuietWindow(720, 780, None),
    )


def test_quiet_windows_default_empty_and_wrap_midnight():
    assert parse_config({}, Path(".")).quiet_windows == ()
    window = polling([{"start": "22:00", "end": "06:00", "pause": True}]).quiet_windows[0]
    assert 1380 in window.minutes() and 300 in window.minutes()
    assert 360 not in window.minutes() and 1319 not in window.minutes()


@pytest.mark.parametrize(
    "windows",
    [
        {"start": "00:00"},
        [{"start": "0:00", "end": "07:00", "pause": True}],
        [{"start": "25:00", "end": "07:00", "pause": True}],
        [{"start": "07:00", "end": "07:00", "pause": True}],
        [{"start": "00:00", "end": "07:00"}],
        [{"start": "00:00", "end": "07:00", "pause": True, "interval_seconds": [300, 600]}],
        [{"start": "00:00", "end": "07:00", "pause": False}],
        [{"start": "00:00", "end": "07:00", "interval_seconds": [60, 120]}],
        [{"start": "00:00", "end": "07:00", "interval_seconds": [900, 600]}],
        [{"start": "00:00", "end": "07:00", "pause": True, "extra": 1}],
        [
            {"start": "22:00", "end": "02:00", "pause": True},
            {"start": "01:00", "end": "05:00", "interval_seconds": [3600, 5400]},
        ],
        [{"start": "00:00", "end": "01:00", "pause": True}] * 13,
    ],
)
def test_invalid_quiet_windows_fail_clearly(windows):
    with pytest.raises(ValueError):
        polling(windows)


class Edge:
    def __init__(self, high=False):
        self.high = high

    def randint(self, low, high):
        return high if self.high else low


TAIPEI = ZoneInfo("Asia/Taipei")
WINDOWS = (
    QuietWindow(0, 420, (10800, 12600)),
    QuietWindow(1200, 0, (2700, 3600)),
    QuietWindow(720, 780, None),
)


def at(hour, minute, day=20):
    return datetime(2026, 10, day, hour, minute, tzinfo=TAIPEI).timestamp()


@pytest.mark.parametrize(
    ("now", "expected", "high"),
    [
        (at(10, 0), at(10, 5), False),  # outside windows: normal interval
        (at(1, 0), at(4, 0), False),  # 3 hours overnight
        (at(5, 30), at(7, 1), False),  # capped at window end + jitter
        (at(5, 30), at(7, 10), True),
        (at(21, 0), at(21, 45), False),
        (at(23, 30), at(0, 1, day=21), False),  # 23:59 means midnight
        (at(12, 30), at(13, 1), False),  # paused window waits until it ends
        (at(11, 58), at(13, 1), False),  # never schedule into a paused window
    ],
)
def test_normal_next_check_follows_quiet_windows(now, expected, high):
    config = Config(quiet_windows=WINDOWS)
    assert normal_next_check(config, now, Edge(high)) == expected


def test_quiet_window_slows_normal_mode_but_not_active_mode(harness):
    h, w = harness, harness["watcher"]
    # The harness clock is 16:00 in Taipei.
    w.config = h["replace_config"](quiet_windows=(QuietWindow(900, 1080, (3600, 3600)),))
    h["source"].push("SOLD_OUT")
    asyncio.run(w.check("test"))
    assert w.store.target("test")["next_check"] == h["clock"]() + 3600
    h["clock"].advance(3600)
    h["source"].push("TEMPORARILY_UNAVAILABLE")
    asyncio.run(w.check("test"))
    state = w.store.target("test")
    assert state["mode"] == "ACTIVE" and state["next_check"] == h["clock"]() + 60
