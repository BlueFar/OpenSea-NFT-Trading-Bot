from datetime import datetime, date, time, timedelta, timezone
from typing import List, Tuple, Dict, Any
import pytz

def get_timezone(tz_name: str = "Asia/Kolkata") -> pytz.BaseTzInfo:
    """Returns the pytz timezone object, falling back to UTC if invalid."""
    try:
        return pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return pytz.UTC

def now_utc() -> datetime:
    """Returns current UTC datetime."""
    return datetime.now(timezone.utc)

def now_local(tz_name: str = "Asia/Kolkata") -> datetime:
    """Returns current datetime in configured timezone."""
    tz = get_timezone(tz_name)
    return datetime.now(tz)

def current_daily_folder_name(tz_name: str = "Asia/Kolkata") -> str:
    """Returns YYYY-MM-DD string in configured timezone."""
    return now_local(tz_name).strftime("%Y-%m-%d")

def get_seven_complete_calendar_days(tz_name: str = "Asia/Kolkata") -> List[date]:
    """
    Returns the last 7 complete calendar days (yesterday down to 7 days ago)
    in the configured timezone, in chronological order.
    """
    local_today = now_local(tz_name).date()
    days = [local_today - timedelta(days=i) for i in range(7, 0, -1)]
    return days

def get_calendar_day_utc_bounds(target_date: date, tz_name: str = "Asia/Kolkata") -> Tuple[int, int]:
    """
    Given a calendar date in local timezone, returns (start_unix_timestamp, end_unix_timestamp)
    representing [00:00:00, 23:59:59.999999] in UTC seconds.
    """
    tz = get_timezone(tz_name)
    local_start = tz.localize(datetime.combine(target_date, time.min))
    local_end = tz.localize(datetime.combine(target_date, time.max))
    
    start_utc_ts = int(local_start.astimezone(timezone.utc).timestamp())
    end_utc_ts = int(local_end.astimezone(timezone.utc).timestamp())
    return start_utc_ts, end_utc_ts

def parse_iso_or_timestamp(val: Any) -> datetime:
    """Safely parses an ISO string or Unix timestamp to UTC datetime."""
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, str):
        # Handle formats like 2024-01-01, 2024-01-01T00:00:00Z, etc.
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        try:
            return datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return now_utc()
