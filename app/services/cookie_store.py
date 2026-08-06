from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.credentials import get_credentials
from app.domain import Credentials, MediumAccount
from app.errors import CredentialError, LoginExpiredError
from app.services.observability import RefreshTelemetry


class CookieStore:
    def __init__(
        self,
        redis_client: Any | None,
        *,
        ttl_seconds: int,
        payload_loader: Callable[[int], Awaitable[MediumAccount]] | None = None,
        telemetry: RefreshTelemetry | None = None,
    ) -> None:
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._payload_loader = payload_loader
        self._telemetry = telemetry or RefreshTelemetry.from_settings()

    @property
    def telemetry(self) -> RefreshTelemetry:
        return self._telemetry

    @staticmethod
    def key(account_id: int) -> str:
        return f"media:cookie:v1:{account_id}"

    async def load(self, account: MediumAccount) -> Credentials:
        if account.sync_status != 1 or account.refresh_result != "ready":
            self._telemetry.emit(
                "cookie_unavailable",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                result=account.refresh_result,
                code=account.refresh_code,
            )
            raise LoginExpiredError("媒体账号登录态已过期")
        fallback_reason = "redis_unavailable"
        if self._redis is not None:
            try:
                raw = await self._redis.get(self.key(account.id))
                if raw:
                    record = json.loads(raw)
                    if (
                        isinstance(record, dict)
                        and record.get("version") == account.cookie_version
                        and record.get("result") == "ready"
                        and record.get("proxy_fingerprint") == account.proxy_fingerprint
                        and self._valid_cookie_map(record.get("cookies"))
                        and isinstance(record.get("cookie_header"), str)
                        and bool(record.get("cookie_header"))
                    ):
                        self._telemetry.emit(
                            "cookie_cache_hit",
                            account_id=account.id,
                            platform=account.platform,
                            version=account.cookie_version,
                        )
                        return Credentials(
                            cookie_header=record["cookie_header"],
                            cookies=record["cookies"],
                            session_id=record.get("session_id"),
                            proxy_fingerprint=record.get("proxy_fingerprint"),
                        )
                fallback_reason = "redis_miss_or_version_mismatch"
            except Exception as error:
                fallback_reason = "redis_error"
                fallback_error_type = type(error).__name__
            else:
                fallback_error_type = None
        else:
            fallback_error_type = None
        self._telemetry.emit(
            "cookie_cache_fallback",
            account_id=account.id,
            platform=account.platform,
            version=account.cookie_version,
            redis_fallback=True,
            result=fallback_reason,
            error_type=fallback_error_type,
        )

        source = account
        if self._payload_loader is not None:
            source = await self._payload_loader(account.id)
            if source.cookie_version != account.cookie_version:
                raise LoginExpiredError("媒体账号 Cookie 版本已变化")
            if source.cookie_proxy_fingerprint != account.proxy_fingerprint:
                raise LoginExpiredError("媒体账号代理已变化，需要刷新登录态")
            if source.sync_status != 1 or source.refresh_result != "ready":
                raise LoginExpiredError("媒体账号登录态已过期")
        try:
            credentials = get_credentials(source)
        except CredentialError as error:
            raise LoginExpiredError("媒体账号未配置可用 Cookie") from error
        await self.write_ready(
            source.id,
            source.cookie_version,
            credentials,
            platform=source.platform,
        )
        return credentials

    @staticmethod
    def _valid_cookie_map(value: object) -> bool:
        return isinstance(value, dict) and all(
            isinstance(name, str) and isinstance(cookie_value, str)
            for name, cookie_value in value.items()
        )

    async def write_ready(
        self,
        account_id: int,
        version: int,
        credentials: Credentials,
        *,
        platform: str = "unknown",
    ) -> None:
        if self._redis is None:
            return
        record = {
            "version": version,
            "result": "ready",
            "code": None,
            "proxy_fingerprint": credentials.proxy_fingerprint,
            "cookie_header": credentials.cookie_header,
            "cookies": credentials.cookies,
            "session_id": credentials.session_id,
        }
        try:
            await self._redis.set(
                self.key(account_id), json.dumps(record, separators=(",", ":")), ex=self._ttl_seconds
            )
            self._telemetry.emit(
                "cookie_cache_write_ready",
                account_id=account_id,
                platform=platform,
                version=version,
                result="ready",
            )
        except Exception as error:
            self._telemetry.emit(
                "cookie_cache_write_failed",
                account_id=account_id,
                platform=platform,
                version=version,
                result="ready",
                redis_fallback=True,
                error_type=type(error).__name__,
            )

    async def write_failure(
        self,
        account_id: int,
        version: int,
        result: str,
        code: str,
        *,
        platform: str = "unknown",
    ) -> None:
        if self._redis is None:
            return
        record = {"version": version, "result": result, "code": code}
        try:
            await self._redis.set(
                self.key(account_id), json.dumps(record, separators=(",", ":")), ex=60
            )
            self._telemetry.emit(
                "cookie_cache_write_failure",
                account_id=account_id,
                platform=platform,
                version=version,
                result=result,
                code=code,
            )
        except Exception as error:
            self._telemetry.emit(
                "cookie_cache_write_failed",
                account_id=account_id,
                platform=platform,
                version=version,
                result=result,
                code=code,
                redis_fallback=True,
                error_type=type(error).__name__,
            )
