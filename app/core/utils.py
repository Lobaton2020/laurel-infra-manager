from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC. `datetime.utcnow` esta deprecado desde Python 3.12."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
