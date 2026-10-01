"""The Claude API health names the plan of the credential the bridge actually loaded.

A client of this bridge (cutkit's studio) may say "subscription" and start a model call only when the bridge says,
from its own running process and without a call, that its login is a Claude Pro or Max subscription. The health
reported provider, upstream and the token's life, but dropped the plan the loaded OAuth credential carries
(`subscriptionType`), so the client could not tell a subscription from an API-billed login.

`auth.subscription_type` is "pro" or "max" as that credential names itself, else None: never another plan taken as
Pro/Max, never the separate profile (~/.claude/.account.json), which may describe another account, and never filled
in by a refresh, a login or a call: these tests reach no network.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from ccproxy.auth.storage.generic import GenericJsonStorage
from ccproxy.plugins.claude_api.config import ClaudeAPISettings
from ccproxy.plugins.claude_api.health import claude_api_health_check
from ccproxy.plugins.oauth_claude.manager import ClaudeApiTokenManager
from ccproxy.plugins.oauth_claude.models import ClaudeCredentials, ClaudeOAuthToken


HOUR_MS = 3_600_000


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


async def _auth_health(
    home: Path, plan: str | None, expires_at: int | None, used: bool = False
) -> dict:
    """The health's auth block for a bridge whose stored credential names `plan`, with a profile beside it that says
    otherwise (Max when the credential names none, Pro when it does), the home directory moved to `home`; `used`:
    the bridge loaded its credential to call with it first (load_credentials may fill a missing plan in)."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    profile = {
        "account": {
            "uuid": "another-account",
            "has_claude_max": plan is None,
            "has_claude_pro": plan is not None,
        }
    }
    (home / ".claude" / ".account.json").write_text(json.dumps(profile))
    storage = GenericJsonStorage(home / "credentials.json", ClaudeCredentials)
    manager = ClaudeApiTokenManager(storage=storage)
    if expires_at is not None:
        await manager.save_credentials(
            ClaudeCredentials(
                claudeAiOauth=ClaudeOAuthToken(
                    accessToken=SecretStr("access"),
                    refreshToken=SecretStr("refresh"),
                    expiresAt=expires_at,
                    subscriptionType=plan,
                )
            )
        )
    with patch("pathlib.Path.home", return_value=home):
        if used:
            await manager.load_credentials()
        result = await claude_api_health_check(
            ClaudeAPISettings(), credentials_manager=manager, version="test"
        )
    stored = (
        json.loads((home / "credentials.json").read_text())
        if expires_at is not None
        else None
    )
    return {
        **result.details["auth"],
        "stored_plan": (stored or {}).get("claudeAiOauth", {}).get("subscriptionType"),
    }


@pytest.mark.asyncio
async def test_the_health_names_the_max_plan_the_loaded_credential_carries(
    tmp_path: Path,
) -> None:
    auth = await _auth_health(tmp_path, "max", _now_ms() + HOUR_MS)
    assert (
        auth["token_available"],
        auth["token_expired"],
        auth["subscription_type"],
    ) == (True, False, "max")


@pytest.mark.asyncio
async def test_a_profile_that_says_otherwise_is_not_the_credentials_plan(
    tmp_path: Path,
) -> None:
    named = await _auth_health(
        tmp_path / "named", "max", _now_ms() + HOUR_MS
    )  # the profile beside it says Pro
    assert named["subscription_type"] == "max"
    unnamed = await _auth_health(
        tmp_path / "unnamed", None, _now_ms() + HOUR_MS
    )  # the profile says Max
    assert (unnamed["token_available"], unnamed["subscription_type"]) == (True, None)
    # reading the health wrote nothing into the credential
    assert unnamed["stored_plan"] is None
    # the bridge's own token path fills the missing plan in from that profile and
    # saves it: still not this login's own word
    filled = await _auth_health(
        tmp_path / "filled", None, _now_ms() + HOUR_MS, used=True
    )
    assert (filled["stored_plan"], filled["subscription_type"]) == ("max", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["free", "team", "enterprise", "unknown", ""])
async def test_no_other_plan_is_taken_for_pro_or_max(tmp_path: Path, plan: str) -> None:
    auth = await _auth_health(tmp_path, plan, _now_ms() + HOUR_MS)
    assert auth["subscription_type"] is None


@pytest.mark.asyncio
async def test_no_credential_and_an_expired_one_are_said_as_such(
    tmp_path: Path,
) -> None:
    missing = await _auth_health(tmp_path / "missing", None, None)
    assert (missing["token_available"], missing["subscription_type"]) == (False, None)
    expired = await _auth_health(tmp_path / "expired", "max", _now_ms() - HOUR_MS)
    assert (expired["token_available"], expired["token_expired"]) == (
        False,
        True,
    )  # a client starts nothing on it
