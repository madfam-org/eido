"""
Selva Auto-Tagging Service

When a capture reaches READY status, Eido calls Selva (autoswarm-office,
the MADFAM OpenAI-compatible LLM router) to generate semantic tags.

Per solarpunk-foundry conventions: never call OpenAI/Anthropic directly.
All LLM calls route through Selva at /v1.
"""
import json
import logging
from typing import Any

import httpx

from eido_api.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_SELVA_URL = getattr(settings, "selva_url", "http://autoswarm-api:8000")
_MAX_TAGS = 8
_TIMEOUT = httpx.Timeout(30.0)


async def auto_tag_capture(capture_id: str) -> None:
    """
    Call Selva /v1/chat/completions to generate semantic tags for a capture.
    Merges generated tags with user-supplied tags. Silently no-ops on failure.
    """
    from sqlalchemy import select, update
    from eido_api.db.session import async_session_maker
    from eido_api.models import Capture

    async with async_session_maker() as db:
        result = await db.execute(select(Capture).where(Capture.id == capture_id))
        capture = result.scalar_one_or_none()
        if not capture:
            return
        title = capture.title or "untitled"
        mode = capture.mode.value if capture.mode else "3dgs"
        existing_tags: list[str] = capture.tags or []

    payload: dict[str, Any] = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "Output only a valid JSON array of strings."},
            {"role": "user", "content": (
                f"Generate up to {_MAX_TAGS} concise lowercase tags for a 3D capture.\n"
                f"Title: \"{title}\"\nMode: {mode}\nExisting tags: {existing_tags}\n"
                "Return ONLY a JSON array of strings."
            )},
        ],
        "max_tokens": 150,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_SELVA_URL}/v1/chat/completions",
                json=payload,
                headers={"X-Eido-Capture-ID": capture_id},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
        new_tags: list[str] = json.loads(content)
        if not isinstance(new_tags, list):
            raise ValueError("non-list response")
        merged = list(dict.fromkeys(existing_tags + [t.lower().strip() for t in new_tags[:_MAX_TAGS]]))
        async with async_session_maker() as db:
            await db.execute(update(Capture).where(Capture.id == capture_id).values(tags=merged))
            await db.commit()
        logger.info("Auto-tagged capture %s: %s", capture_id, merged)
    except Exception as exc:
        logger.warning("auto_tag failed for %s: %s (tags unchanged)", capture_id, exc)
