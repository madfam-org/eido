"""Redis connection management — job queue and pub/sub."""
import json
import logging
from typing import Any

import redis.asyncio as redis

from eido_api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_redis: redis.Redis | None = None
JOB_QUEUE_KEY = "eido:jobs:pending"


async def init_redis() -> None:
    global _redis
    _redis = redis.from_url(str(settings.redis_url), decode_responses=True)
    await _redis.ping()
    logger.info("Redis connected.")


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.close()
        _redis = None


def get_redis() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised.")
    return _redis


async def enqueue_job(job_data: dict[str, Any]) -> None:
    if _redis is None:
        raise RuntimeError("Redis not initialised.")
    await _redis.lpush(JOB_QUEUE_KEY, json.dumps(job_data))


async def get_job_status(job_id: str) -> dict[str, Any] | None:
    if _redis is None:
        raise RuntimeError("Redis not initialised.")
    data = await _redis.hgetall(f"eido:job:{job_id}")
    return dict(data) if data else None
