"""api/admin/schedules._validate：cron / 旧 hour / interval 归一化与校验（无 HTTP）。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from codereview_ai.api.admin.schedules import _validate


def test_poll_normalizes_interval_seconds():
    assert _validate("poll", {}) == {"interval_seconds": 3600}
    assert _validate("poll", {"interval_seconds": 60}) == {"interval_seconds": 60}


def test_daily_cron_passes_through():
    assert _validate("daily", {"cron": "0 9 * * 1-5"}) == {"cron": "0 9 * * 1-5"}
    assert _validate("daily", {"cron": "*/30 * * * *"}) == {"cron": "*/30 * * * *"}


def test_daily_invalid_cron_raises():
    with pytest.raises(HTTPException) as ei:
        _validate("daily", {"cron": "not a cron"})
    assert ei.value.status_code == 400


def test_daily_legacy_hour_migrates_to_cron():
    assert _validate("daily", {"hour": 9}) == {"cron": "0 9 * * *"}
    assert _validate("daily", {"hour": 18}) == {"cron": "0 18 * * *"}


def test_daily_default_cron():
    assert _validate("daily", {}) == {"cron": "0 9 * * *"}


def test_daily_out_of_range_hour_raises():
    with pytest.raises(HTTPException) as ei:
        _validate("daily", {"hour": 25})
    assert ei.value.status_code == 400
