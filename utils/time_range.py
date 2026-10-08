"""UTC instants for browser-local date filters, with legacy UTC parsing."""
from datetime import datetime, timezone


def utc_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc))


class TimeRange:
    def __init__(self, start_at: str = "", end_before: str = ""):
        self.start = self._bound(start_at)
        self.end = self._bound(end_before)
        if self.start and self.end and self.start >= self.end:
            raise ValueError("开始时间必须早于结束时间")

    @staticmethod
    def _bound(value: str) -> datetime | None:
        if not value:
            return None
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("时间范围必须包含时区")
        return result.astimezone(timezone.utc)

    def contains(self, value: str) -> bool:
        if self.start is None and self.end is None:
            return True
        try:
            instant = utc_datetime(value)
        except (ValueError, TypeError, AttributeError):
            return False
        return (self.start is None or instant >= self.start) and (self.end is None or instant < self.end)
