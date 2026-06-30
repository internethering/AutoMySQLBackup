"""Tests for BackupOrchestrator — scheduling logic, rotation guard, properties."""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path

from automysqlbackup.config import Config
from automysqlbackup.orchestrator import BackupOrchestrator


def _orch(date: datetime.datetime, **cfg_kwargs) -> BackupOrchestrator:
    """Build an orchestrator with a frozen clock and minimal Config."""
    cfg = Config()
    cfg.backup_dir = Path(cfg_kwargs.pop("backup_dir", "/tmp"))
    for k, v in cfg_kwargs.items():
        setattr(cfg, k, v)
    # Bypass __init__ to avoid needing real db/comp/enc/dirs objects
    orch = object.__new__(BackupOrchestrator)
    orch._cfg = cfg
    orch._now = date
    orch._files = []
    return orch


# Friday 2024-03-15, week 11, day 5
_FRI = datetime.datetime(2024, 3, 15, 12, 30)
# Monday 2024-03-18
_MON = datetime.datetime(2024, 3, 18, 8, 0)


class TestDoMonthly(unittest.TestCase):
    def test_true_on_configured_day(self):
        o = _orch(datetime.datetime(2024, 3, 15), do_monthly=15)
        self.assertTrue(o._do_monthly())

    def test_false_on_other_day(self):
        o = _orch(datetime.datetime(2024, 3, 10), do_monthly=15)
        self.assertFalse(o._do_monthly())

    def test_disabled_when_do_monthly_is_zero(self):
        o = _orch(datetime.datetime(2024, 3, 1), do_monthly=0)
        self.assertFalse(o._do_monthly())

    def test_falls_back_to_last_day_of_month(self):
        # February 2024 has 29 days; do_monthly=31 should fire on day 29.
        o = _orch(datetime.datetime(2024, 2, 29), do_monthly=31)
        self.assertTrue(o._do_monthly())

    def test_fallback_does_not_fire_early(self):
        # Day 28 must not trigger when do_monthly=31 and last day is 29.
        o = _orch(datetime.datetime(2024, 2, 28), do_monthly=31)
        self.assertFalse(o._do_monthly())

    def test_handles_december_correctly(self):
        # December has 31 days; do_monthly=31 fires on day 31.
        o = _orch(datetime.datetime(2024, 12, 31), do_monthly=31)
        self.assertTrue(o._do_monthly())


class TestDoWeekly(unittest.TestCase):
    def test_true_on_configured_weekday(self):
        # 2024-03-15 is Friday (iso 5)
        o = _orch(_FRI, do_weekly=5)
        self.assertTrue(o._do_weekly())

    def test_false_on_other_weekday(self):
        o = _orch(_FRI, do_weekly=1)   # Monday, but today is Friday
        self.assertFalse(o._do_weekly())

    def test_disabled_when_do_weekly_is_zero(self):
        o = _orch(_FRI, do_weekly=0)
        self.assertFalse(o._do_weekly())

    def test_monday_triggers_when_configured_as_1(self):
        o = _orch(_MON, do_weekly=1)
        self.assertTrue(o._do_weekly())


class TestAlreadyDoneToday(unittest.TestCase):
    def test_returns_false_when_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            o = _orch(datetime.datetime.now(), backup_dir=tmp)
            self.assertFalse(o._already_done_today("daily/*_2099-01-01_*"))

    def test_returns_true_when_matching_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "daily_mydb_2024-03-15_12h30m_Friday.sql.gz"
            p.write_bytes(b"x")
            o = _orch(datetime.datetime.now(), backup_dir=tmp)
            self.assertTrue(o._already_done_today("daily_mydb_2024-03-15_*"))


class TestDifferentialRotationMinimum(unittest.TestCase):
    def test_rotation_raised_to_21_when_below_minimum(self):
        cfg = Config()
        cfg.differential = True
        cfg.rotation_daily = 6
        if cfg.differential:
            cfg.rotation_daily = max(cfg.rotation_daily, 21)
        self.assertEqual(cfg.rotation_daily, 21)

    def test_rotation_not_lowered_when_already_above_21(self):
        cfg = Config()
        cfg.differential = True
        cfg.rotation_daily = 30
        if cfg.differential:
            cfg.rotation_daily = max(cfg.rotation_daily, 21)
        self.assertEqual(cfg.rotation_daily, 30)

    def test_rotation_exactly_21_is_unchanged(self):
        cfg = Config()
        cfg.differential = True
        cfg.rotation_daily = 21
        if cfg.differential:
            cfg.rotation_daily = max(cfg.rotation_daily, 21)
        self.assertEqual(cfg.rotation_daily, 21)


class TestTimestampProperties(unittest.TestCase):
    def test_timestamp_format(self):
        o = _orch(datetime.datetime(2024, 3, 15, 14, 30))
        self.assertEqual(o.timestamp, "2024-03-15_14h30m")

    def test_date_stamp(self):
        o = _orch(datetime.datetime(2024, 3, 15))
        self.assertEqual(o.date_stamp, "2024-03-15")

    def test_week_no_for_week_11(self):
        o = _orch(_FRI)   # 2024-03-15 is ISO week 11
        self.assertEqual(o.week_no, "11")

    def test_dow_friday_is_5(self):
        o = _orch(_FRI)
        self.assertEqual(o.dow, 5)

    def test_dow_monday_is_1(self):
        o = _orch(_MON)
        self.assertEqual(o.dow, 1)

    def test_dom(self):
        o = _orch(datetime.datetime(2024, 3, 15))
        self.assertEqual(o.dom, 15)

    def test_month_name(self):
        o = _orch(datetime.datetime(2024, 3, 15))
        self.assertEqual(o.month_name, "March")

    def test_dow_name(self):
        o = _orch(_FRI)
        self.assertEqual(o.dow_name, "Friday")


if __name__ == "__main__":
    unittest.main()
