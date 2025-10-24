# /tokens/core.py
from datetime import datetime, timedelta, timezone

ISO_MON = 0  # Monday

def iso_week_monday_utc(ts: datetime) -> datetime:
    """
    برمی‌گرداند دوشنبهٔ همین هفته (00:00:00) به وقت UTC.
    مرجع هفته = ISO 8601 (شروع دوشنبه).  Idempotent anchor.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    delta_days = (ts.weekday() - ISO_MON) % 7
    monday = (ts - timedelta(days=delta_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday

def next_iso_week_monday_utc(ts: datetime) -> datetime:
    base = iso_week_monday_utc(ts)
    return base + timedelta(days=7)
