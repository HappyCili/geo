from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any

from app.domain import Credentials, LoginResult, LoginSecret, MediumAccount, PublishResult
from app.errors import (
    CaptchaRequiredError,
    LoginNetworkError,
    LoginProtocolError,
    LoginCredentialsUnavailableError,
    LoginExpiredError,
    RefreshUnavailableError,
)
from app.repositories import AccountRepository
from app.services.cookie_store import CookieStore
from app.services.login import LoginProvider
from app.services.observability import RefreshTelemetry


_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class RefreshCoordinator:
    def __init__(
        self,
        accounts: AccountRepository,
        cookies: CookieStore,
        login: LoginProvider | Mapping[str, LoginProvider],
        redis_client: Any | None,
        *,
        lock_ttl_seconds: int,
        renew_seconds: int,
        wait_seconds: float,
        telemetry: RefreshTelemetry | None = None,
        fallback_login_secret: LoginSecret | None = None,
        fallback_login_secrets: Mapping[str, LoginSecret] | None = None,
    ) -> None:
        self._accounts = accounts
        self._cookies = cookies
        self._login = login
        self._redis = redis_client
        self._lock_ttl_ms = lock_ttl_seconds * 1000
        self._renew_seconds = renew_seconds
        self._wait_seconds = wait_seconds
        self._telemetry = telemetry or cookies.telemetry
        self._fallback_login_secret = fallback_login_secret
        self._fallback_login_secrets = dict(fallback_login_secrets or {})

    @staticmethod
    def _lock_key(account_id: int) -> str:
        return f"media:refresh-lock:v1:{account_id}"

    async def refresh(
        self,
        account: MediumAccount,
        *,
        retry: Callable[[MediumAccount, Credentials], Awaitable[PublishResult]] | None = None,
    ) -> tuple[MediumAccount, Credentials] | PublishResult:
        if self._redis is None:
            self._telemetry.emit(
                "refresh_lock_unavailable",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                trigger="persisted" if account.sync_status == 0 else "runtime",
            )
            raise RefreshUnavailableError("Redis 刷新锁不可用")
        token = secrets.token_urlsafe(24)
        try:
            acquired = await self._redis.set(
                self._lock_key(account.id), token, nx=True, px=self._lock_ttl_ms
            )
        except Exception as error:
            self._telemetry.emit(
                "refresh_lock_unavailable",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                trigger="persisted" if account.sync_status == 0 else "runtime",
                error_type=type(error).__name__,
            )
            raise RefreshUnavailableError("Redis 刷新锁不可用") from error
        if not acquired:
            self._telemetry.emit(
                "refresh_waiter",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                waiting=True,
            )
            return await self._wait_for_result(account)

        lost = asyncio.Event()
        self._telemetry.emit(
            "refresh_owner",
            account_id=account.id,
            platform=account.platform,
            version=account.cookie_version,
            waiting=False,
        )
        renew_task = asyncio.create_task(self._renew(account, token, lost))
        login_task: asyncio.Task[LoginResult] | None = None
        lost_task: asyncio.Task[bool] | None = None
        try:
            current = await self._accounts.get_active(account.id)
            if current.cookie_version != account.cookie_version:
                self._telemetry.emit(
                    "refresh_shared_result",
                    account_id=account.id,
                    platform=account.platform,
                    version=current.cookie_version,
                    result=current.refresh_result,
                    code=current.refresh_code,
                )
                return await self._shared_result(current)
            fence = await self._accounts.claim_refresh(account.id, account.cookie_version)
            if fence is None:
                self._telemetry.emit(
                    "refresh_claim_not_owned",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                )
                return await self._shared_result(await self._accounts.get_active(account.id))
            self._telemetry.emit(
                "refresh_claimed",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=fence,
                result="pending",
            )
            try:
                secret = await self._accounts.get_login_secret(account.id)
            except LoginCredentialsUnavailableError:
                fallback_login_secret = self._fallback_login_secrets.get(
                    account.platform,
                    self._fallback_login_secret,
                )
                if fallback_login_secret is None:
                    raise
                self._telemetry.emit(
                    "refresh_login_secret_fallback",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                    fence=fence,
                    result="configured_fallback",
                )
                secret = fallback_login_secret
            login = self._login_for_platform(account.platform)
            if login is None:
                return await self._persist_failure(account, fence, "refresh_unavailable")
            login_task = asyncio.create_task(login.login(secret))
            lost_task = asyncio.create_task(lost.wait())
            done, _ = await asyncio.wait({login_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)
            if lost_task in done:
                login_task.cancel()
                await asyncio.gather(login_task, return_exceptions=True)
                self._telemetry.emit(
                    "refresh_lease_lost",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                    fence=fence,
                )
                raise RefreshUnavailableError("刷新锁租约已失效")
            lost_task.cancel()
            await asyncio.gather(lost_task, return_exceptions=True)
            if lost.is_set():
                self._telemetry.emit(
                    "refresh_lease_lost",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                    fence=fence,
                )
                raise RefreshUnavailableError("刷新锁租约已失效")
            result = login_task.result()
            self._telemetry.emit(
                "refresh_login_completed",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=fence,
                result=result.kind,
            )
            if result.kind != "success" or result.cookies is None or result.cookie_header is None:
                return await self._persist_failure(account, fence, result.kind)
            serialized = json.dumps(result.cookies, separators=(",", ":"))
            version = await self._accounts.save_login(
                account.id,
                account.cookie_version,
                fence,
                cookie_header=result.cookie_header,
                cookies=serialized,
                session_id=result.session_id,
            )
            if version is None:
                self._telemetry.emit(
                    "refresh_save_not_owned",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                    fence=fence,
                )
                return await self._shared_result(await self._accounts.get_active(account.id))
            credentials = Credentials(result.cookie_header, result.cookies, result.session_id)
            await self._cookies.write_ready(
                account.id,
                version,
                credentials,
                platform=account.platform,
            )
            refreshed_account = replace(
                account,
                cookie=result.cookie_header,
                cookies=serialized,
                session_id=result.session_id,
                sync_status=1,
                cookie_version=version,
                refresh_fence=fence,
                refresh_result="ready",
                refresh_code=None,
            )
            self._telemetry.emit(
                "refresh_ready",
                account_id=account.id,
                platform=account.platform,
                version=version,
                fence=fence,
                result="ready",
            )
            if retry is None:
                return refreshed_account, credentials
            await self._confirm_retry_ownership(refreshed_account, token, lost)
            try:
                return await retry(refreshed_account, credentials)
            except LoginExpiredError:
                await self._persist_failure(refreshed_account, fence, "login_expired")
                raise LoginExpiredError("媒体账号登录态仍然失效")
        finally:
            for task in (login_task, lost_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (login_task, lost_task) if task is not None),
                return_exceptions=True,
            )
            renew_task.cancel()
            await asyncio.gather(renew_task, return_exceptions=True)
            await self._release(account, token)

    def _login_for_platform(self, platform: str) -> LoginProvider | None:
        if isinstance(self._login, Mapping):
            return self._login.get(platform)
        return self._login

    async def _renew(self, account: MediumAccount, token: str, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self._renew_seconds)
            try:
                renewed = await self._redis.eval(
                    _RENEW, 1, self._lock_key(account.id), token, self._lock_ttl_ms
                )
            except Exception as error:
                self._telemetry.emit(
                    "refresh_lease_lost",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                    error_type=type(error).__name__,
                )
                lost.set()
                return
            if not renewed:
                self._telemetry.emit(
                    "refresh_lease_lost",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                )
                lost.set()
                return

    async def _release(self, account: MediumAccount, token: str) -> None:
        try:
            await self._redis.eval(_RELEASE, 1, self._lock_key(account.id), token)
        except Exception as error:
            self._telemetry.emit(
                "refresh_release_failed",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                error_type=type(error).__name__,
            )
            return

    async def _confirm_retry_ownership(
        self,
        account: MediumAccount,
        token: str,
        lost: asyncio.Event,
    ) -> None:
        if lost.is_set():
            self._telemetry.emit(
                "publish_retry_not_owned",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=account.refresh_fence,
                result="lease_lost",
                retry_count=1,
            )
            raise RefreshUnavailableError("刷新锁租约已失效")
        try:
            renewed = await self._redis.eval(
                _RENEW,
                1,
                self._lock_key(account.id),
                token,
                self._lock_ttl_ms,
            )
        except Exception as error:
            self._telemetry.emit(
                "publish_retry_not_owned",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=account.refresh_fence,
                result="lock_unavailable",
                retry_count=1,
                error_type=type(error).__name__,
            )
            raise RefreshUnavailableError("重试前无法确认刷新锁") from error
        if not renewed:
            self._telemetry.emit(
                "publish_retry_not_owned",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=account.refresh_fence,
                result="lease_lost",
                retry_count=1,
            )
            raise RefreshUnavailableError("刷新锁租约已失效")

        current = await self._accounts.get_active(account.id)
        if (
            current.cookie_version != account.cookie_version
            or current.refresh_fence != account.refresh_fence
            or current.refresh_result != "ready"
            or current.sync_status != 1
        ):
            self._telemetry.emit(
                "publish_retry_not_owned",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                fence=account.refresh_fence,
                result="fence_changed",
                retry_count=1,
            )
            raise RefreshUnavailableError("重试前刷新状态已变化")
        self._telemetry.emit(
            "publish_retry_owner_confirmed",
            account_id=account.id,
            platform=account.platform,
            version=account.cookie_version,
            fence=account.refresh_fence,
            retry_count=1,
        )

    async def _wait_for_result(self, account: MediumAccount) -> tuple[MediumAccount, Credentials]:
        deadline = asyncio.get_running_loop().time() + self._wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            current = await self._accounts.get_active(account.id)
            if current.cookie_version > account.cookie_version:
                self._telemetry.emit(
                    "refresh_wait_resolved",
                    account_id=account.id,
                    platform=account.platform,
                    version=current.cookie_version,
                    waiting=True,
                    result=current.refresh_result,
                    code=current.refresh_code,
                )
                return await self._shared_result(current)
        self._telemetry.emit(
            "refresh_wait_timeout",
            account_id=account.id,
            platform=account.platform,
            version=account.cookie_version,
            waiting=True,
        )
        raise RefreshUnavailableError("等待账号刷新超时")

    async def _shared_result(self, account: MediumAccount) -> tuple[MediumAccount, Credentials]:
        if account.refresh_result == "ready" and account.sync_status == 1:
            return account, await self._cookies.load(account)
        self._raise_failure(account.refresh_code)

    async def _persist_failure(
        self, account: MediumAccount, fence: int, kind: str
    ) -> tuple[MediumAccount, Credentials]:
        result, code = {
            "login_expired": ("login_expired", "login_expired"),
            "captcha_required": ("login_expired", "captcha_required"),
            "network_error": ("transient_failure", "login_network_error"),
            "refresh_unavailable": ("transient_failure", "refresh_unavailable"),
        }.get(kind, ("transient_failure", "login_protocol_error"))
        version = await self._accounts.mark_refresh_failure(
            account.id, account.cookie_version, fence, result=result, code=code
        )
        if version is not None:
            await self._cookies.write_failure(
                account.id,
                version,
                result,
                code,
                platform=account.platform,
            )
            self._telemetry.emit(
                "refresh_failure",
                account_id=account.id,
                platform=account.platform,
                version=version,
                fence=fence,
                result=result,
                code=code,
            )
            self._raise_failure(code)
        return await self._shared_result(await self._accounts.get_active(account.id))

    @staticmethod
    def _raise_failure(code: str | None) -> None:
        if code == "captcha_required":
            raise CaptchaRequiredError("登录需要验证码")
        if code == "login_expired":
            raise LoginExpiredError("登录凭据失效")
        if code == "login_network_error":
            raise LoginNetworkError("登录网络请求失败")
        if code == "refresh_unavailable":
            raise RefreshUnavailableError("登录刷新不可用")
        raise LoginProtocolError("登录响应无效")
