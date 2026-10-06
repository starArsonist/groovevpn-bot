from datetime import datetime, timezone


def utc_now() -> datetime:
    """Текущее время UTC без tzinfo - в таком виде время хранится в SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_unix(moment: datetime) -> int:
    """Naive UTC datetime -> unix-секунды (формат `expire` в Marzban)."""
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def from_unix(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
