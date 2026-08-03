from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from app.config import get_settings


class RefreshTelemetry:
    """Emit JSON events without leaking account identifiers or credentials."""

    def __init__(self, correlation_salt: str) -> None:
        self._correlation_salt = correlation_salt.encode("utf-8")
        self._logger = logging.getLogger("app.cookie_refresh")

    @classmethod
    def from_settings(cls) -> "RefreshTelemetry":
        settings = get_settings()
        return cls(settings.observability_account_salt or settings.db_password)

    def emit(
        self,
        event: str,
        *,
        account_id: int,
        platform: str,
        version: int | None = None,
        fence: int | None = None,
        trigger: str | None = None,
        waiting: bool | None = None,
        redis_fallback: bool | None = None,
        result: str | None = None,
        code: str | None = None,
        retry_count: int | None = None,
        error_type: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "account_correlation": self._account_correlation(account_id),
            "platform": platform,
        }
        for key, value in {
            "version": version,
            "fence": fence,
            "trigger": trigger,
            "waiting": waiting,
            "redis_fallback": redis_fallback,
            "result": result,
            "code": code,
            "retry_count": retry_count,
            "error_type": error_type,
        }.items():
            if value is not None:
                payload[key] = value
        self._logger.info("cookie_refresh %s", json.dumps(payload, separators=(",", ":")))

    def _account_correlation(self, account_id: int) -> str:
        return hmac.new(
            self._correlation_salt,
            str(account_id).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()[:20]
