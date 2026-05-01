"""
Dhanam Entitlement Helper

Checks the user's subscription tier via the Dhanam billing API.
Per SoC: Eido does not own billing or entitlement state.

Usage:
    await check_entitlement(user, "capture_limit", db)
"""
import logging
import httpx
from eido_api.auth import JanuaUser
from eido_api.config import get_settings
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)
settings = get_settings()
_DHANAM_URL = getattr(settings, "dhanam_url", "http://dhanam-api:8000")
_TIMEOUT = httpx.Timeout(10.0)

TIER_LIMITS = {
    "free":   {"captures_per_month": 5,   "max_resolution": "1k",   "raw_export": False},
    "pro":    {"captures_per_month": 100,  "max_resolution": "8k",   "raw_export": True},
    "studio": {"captures_per_month": 9999, "max_resolution": "full", "raw_export": True},
}


async def get_user_tier(user: JanuaUser) -> str:
    """
    Resolve the user's active tier from Dhanam.
    Falls back to the tier embedded in the Janua JWT claim if Dhanam is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_DHANAM_URL}/api/v1/entitlements/{user.id}",
                headers={"X-Service": "eido"},
            )
            if resp.status_code == 200:
                return resp.json().get("tier", user.tier)
    except Exception as exc:
        logger.warning("Dhanam unreachable for user %s: %s — using JWT claim", user.id, exc)
    return user.tier  # JWT fallback


async def require_tier(user: JanuaUser, minimum_tier: str) -> None:
    """
    Raise HTTP 402 if the user's tier is below the required minimum.
    Tier order: free < pro < studio
    """
    ORDER = {"free": 0, "pro": 1, "studio": 2}
    tier = await get_user_tier(user)
    if ORDER.get(tier, 0) < ORDER.get(minimum_tier, 0):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"This feature requires a {minimum_tier.capitalize()} subscription. "
                "Upgrade at eido.cam/upgrade."
            ),
        )
