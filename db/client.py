import asyncio
from datetime import timezone

from motor.motor_asyncio import AsyncIOMotorClient

from core.config import MONGO_URI, MONGO_DB_NAME, MONGO_TIMEOUT_MS

_client = None
_client_loop = None


def get_db():
    """Returns the database handle.

    Long-running deployments (polling / webhook server / gunicorn) reuse one
    client for the process lifetime. Serverless (api/webhook.py) runs
    `asyncio.run()` per invocation, and Motor clients are bound to the event
    loop that created them — so when we find ourselves on a different loop,
    the client is rebuilt instead of raising 'attached to a different loop'
    on the second warm invocation.
    """
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _client is not None and loop is not None and _client_loop is not loop:
        try:
            _client.close()
        except Exception:
            pass
        _client = None

    if _client is None:
        # tz_aware=True (+ tzinfo=UTC) makes every BSON date PyMongo/Motor
        # decodes come back as an *aware* UTC datetime. Without this, dates
        # are decoded as naive datetimes while db/repository.py's utcnow()
        # writes timezone-aware ones — comparing the two then raises
        # "can't compare offset-naive and offset-aware datetimes".
        _client = AsyncIOMotorClient(
            MONGO_URI,
            serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
            tz_aware=True,
            tzinfo=timezone.utc,
        )
        _client_loop = loop
    return _client[MONGO_DB_NAME]


async def ping_db():
    """Fails fast with a clear message if Mongo isn't reachable, instead of
    letting the first real query blow up deep inside a random handler."""
    await get_db().command("ping")
