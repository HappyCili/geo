from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import Credentials, LoginSecret, MediumAccount, PublishRequirements, PublishResult
from app.errors import (
    LoginExpiredError,
    PlatformMismatchError,
    RefreshUnavailableError,
)
from app.repositories import AccountRepository, CategoryRepository
from app.schemas import PublishArticleRequest
from app.services.cookie_store import CookieStore
from app.services.login import (
    CnblogsLoginProvider,
    HepanLoginProvider,
    LiejuLoginProvider,
)
from app.services.refresh import RefreshCoordinator
from app.services.unit_of_work import UnitOfWork
from app.utils.publisher_registry import PublisherRegistry


class PublishOrchestrator:
    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        unit_of_work: UnitOfWork | None = None,
        accounts: AccountRepository | None = None,
        categories: CategoryRepository | None = None,
        registry: PublisherRegistry | None = None,
        cookie_store: CookieStore | None = None,
        refresh: RefreshCoordinator | None = None,
        publish_deadline_seconds: float | None = None,
    ) -> None:
        if unit_of_work is None and session is not None:
            unit_of_work = UnitOfWork(session)
        if accounts is None and unit_of_work is not None:
            accounts = unit_of_work.accounts
        if categories is None and unit_of_work is not None:
            categories = unit_of_work.categories
        if accounts is None or categories is None:
            if session is None:
                raise ValueError("session 或 repositories 为必填参数")
            accounts = AccountRepository(session)
            categories = CategoryRepository(session)
        self._unit_of_work = unit_of_work
        self._accounts = accounts
        self._categories = categories
        if registry is None:
            from app.api.platform import create_default_publisher_registry

            registry = create_default_publisher_registry()
        self._registry = registry

        if cookie_store is None:
            redis_client = self._create_redis_client() if session is not None else None
            from app.config import get_settings

            cookie_store = CookieStore(
                redis_client,
                ttl_seconds=get_settings().cookie_cache_ttl_seconds,
                payload_loader=getattr(accounts, "get_with_payload", None),
            )
        self._cookie_store = cookie_store
        self._telemetry = cookie_store.telemetry
        if publish_deadline_seconds is None:
            from app.config import get_settings

            publish_deadline_seconds = get_settings().publish_deadline_seconds
        self._publish_deadline_seconds = publish_deadline_seconds

        if refresh is None and session is not None:
            from app.config import get_settings

            settings = get_settings()
            fallback_login_secret = self._configured_login_secret(settings)
            fallback_login_secrets = (
                {"cnblogs": fallback_login_secret}
                if fallback_login_secret is not None
                else None
            )
            refresh = RefreshCoordinator(
                accounts,
                cookie_store,
                {
                    "cnblogs": CnblogsLoginProvider(
                        timeout_seconds=settings.login_timeout_seconds
                    ),
                    "hepan": HepanLoginProvider(
                        timeout_seconds=settings.login_timeout_seconds
                    ),
                    "lieju": LiejuLoginProvider(
                        timeout_seconds=settings.login_timeout_seconds
                    ),
                },
                cookie_store._redis,
                lock_ttl_seconds=settings.refresh_lock_ttl_seconds,
                renew_seconds=settings.refresh_lock_renew_seconds,
                wait_seconds=settings.refresh_wait_seconds,
                fallback_login_secrets=fallback_login_secrets,
            )
        self._refresh = refresh

    @staticmethod
    def _create_redis_client() -> Any:
        from redis.asyncio import from_url

        from app.config import get_settings

        settings = get_settings()
        return from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_timeout_seconds,
            socket_timeout=settings.redis_timeout_seconds,
        )

    @staticmethod
    def _configured_login_secret(settings: Any) -> LoginSecret | None:
        username = getattr(settings, "cnblogs_username", None)
        password_value = getattr(settings, "cnblogs_password", None)
        if hasattr(password_value, "get_secret_value"):
            password = password_value.get_secret_value()
        else:
            password = password_value
        if not isinstance(username, str) or not username.strip():
            return None
        if not isinstance(password, str) or not password.strip():
            return None
        return LoginSecret(username=username.strip(), password=password.strip())

    async def publish(
        self, request: PublishArticleRequest, expected_platform: str
    ) -> tuple[str, PublishResult]:
        account: MediumAccount | None = None

        async def publish_with_tracking() -> tuple[str, PublishResult]:
            nonlocal account
            account = await self._accounts.get_active(request.account_id)
            return await self._publish_from_account(account, request, expected_platform)

        try:
            return await asyncio.wait_for(
                publish_with_tracking(),
                timeout=self._publish_deadline_seconds,
            )
        except asyncio.TimeoutError as error:
            if account is not None:
                self._telemetry.emit(
                    "publish_deadline_exceeded",
                    account_id=account.id,
                    platform=account.platform,
                    version=account.cookie_version,
                )
            raise RefreshUnavailableError("发布请求超时") from error

    async def _publish_from_account(
        self,
        account: MediumAccount,
        request: PublishArticleRequest,
        expected_platform: str,
    ) -> tuple[str, PublishResult]:
        self._assert_platform(account, expected_platform)
        category_value = await self._categories.get_value(account.platform, request.category)

        if account.sync_status == 0:
            self._telemetry.emit(
                "publish_refresh_triggered",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                trigger="persisted",
                retry_count=0,
            )
            return await self._refresh_and_publish(
                account, request, category_value
            )

        try:
            credentials = await self._cookie_store.load(account)
        except LoginExpiredError:
            self._telemetry.emit(
                "publish_refresh_triggered",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                trigger="missing_credentials",
                retry_count=0,
            )
            return await self._refresh_and_publish(
                account, request, category_value
            )

        try:
            result = await self._publish_once(account, request, category_value, credentials)
        except LoginExpiredError as error:
            self._telemetry.emit(
                "publish_refresh_triggered",
                account_id=account.id,
                platform=account.platform,
                version=account.cookie_version,
                trigger="runtime",
                retry_count=0,
                result="login_expired",
            )
            if not error.retry_safe:
                raise
            try:
                result = await self._refresh_then_retry(
                    account,
                    request,
                    category_value,
                )
            except LoginExpiredError:
                raise
        return account.platform, result

    async def _refresh_and_publish(
        self,
        account: MediumAccount,
        request: PublishArticleRequest,
        category_value: str,
    ) -> tuple[str, PublishResult]:
        refreshed_account, credentials = await self._refresh_credentials(account)
        result = await self._publish_once(
            refreshed_account,
            request,
            category_value,
            credentials,
        )
        return refreshed_account.platform, result

    async def _refresh_then_retry(
        self,
        account: MediumAccount,
        request: PublishArticleRequest,
        category_value: str,
    ) -> PublishResult:
        if account.platform not in {"cnblogs", "hepan", "lieju"} or self._refresh is None:
            raise LoginExpiredError("媒体账号登录态已过期")

        async def retry(
            refreshed_account: MediumAccount,
            credentials: Credentials,
        ) -> PublishResult:
            self._telemetry.emit(
                "publish_retry_started",
                account_id=refreshed_account.id,
                platform=refreshed_account.platform,
                version=refreshed_account.cookie_version,
                fence=refreshed_account.refresh_fence,
                retry_count=1,
            )
            result = await self._publish_once(
                refreshed_account,
                request,
                category_value,
                credentials,
            )
            self._telemetry.emit(
                "publish_retry_completed",
                account_id=refreshed_account.id,
                platform=refreshed_account.platform,
                version=refreshed_account.cookie_version,
                fence=refreshed_account.refresh_fence,
                result="success",
                retry_count=1,
            )
            return result

        result = await self._refresh.refresh(account, retry=retry)
        if not isinstance(result, PublishResult):
            raise RefreshUnavailableError("刷新后未执行发布重试")
        return result

    async def get_requirements(
        self, account_id: int, category: str, expected_platform: str
    ) -> tuple[str, PublishRequirements]:
        account = await self._accounts.get_active(account_id)
        self._assert_platform(account, expected_platform)
        category_value = await self._categories.get_value(account.platform, category)
        credentials = await self._cookie_store.load(account)
        publisher = self._registry.get(account.platform)
        requirements = await publisher.get_requirements(
            category=category,
            category_value=category_value,
            credentials=credentials,
        )
        return account.platform, requirements

    async def _refresh_credentials(
        self, account: MediumAccount,
    ) -> tuple[MediumAccount, Credentials]:
        if account.platform not in {"cnblogs", "hepan", "lieju"} or self._refresh is None:
            raise LoginExpiredError("媒体账号登录态已过期")
        result = await self._refresh.refresh(account)
        if not isinstance(result, tuple):
            raise RefreshUnavailableError("刷新未返回登录凭据")
        return result

    async def _publish_once(
        self,
        account: MediumAccount,
        request: PublishArticleRequest,
        category_value: str,
        credentials: Credentials,
    ) -> PublishResult:
        publisher = self._registry.get(account.platform)
        return await publisher.publish_article(
            title=request.title,
            category=request.category,
            category_value=category_value,
            content=request.content,
            content_type=request.content_type,
            credentials=credentials,
            platform_fields=request.platform_fields,
        )

    @staticmethod
    def _assert_platform(account: MediumAccount, expected_platform: str) -> None:
        if account.platform != expected_platform:
            raise PlatformMismatchError("路由平台与媒体账号平台不一致")


PublishService = PublishOrchestrator
