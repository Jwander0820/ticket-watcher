from pathlib import Path

import pytest

from ticket_watcher.config import QuietWindow, parse_config


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
