from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import LoginSecret, MediumAccount
from app.errors import (
    AccountNotFoundError,
    AccountUnavailableError,
    CategoryNotFoundError,
    LoginCredentialsUnavailableError,
    PersistenceUnavailableError,
)
from app.utils.proxy import build_account_proxy


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, account_id: int) -> MediumAccount:
        row = (await self._session.execute(
            text(
                """
                SELECT a.id, a.platform, a.status, a.sync_status, a.`del`,
                       a.cookie_version, a.refresh_fence, a.refresh_result, a.refresh_code,
                       a.proxy_id, a.cookie_proxy_fingerprint,
                       p.id AS resolved_proxy_id, p.protocol AS proxy_protocol,
                       p.ip AS proxy_ip, p.port AS proxy_port,
                       p.username AS proxy_username, p.password AS proxy_password,
                       CASE
                           WHEN NULLIF(TRIM(COALESCE(a.cookie, '')), '') IS NOT NULL
                             OR NULLIF(TRIM(COALESCE(a.cookies, '')), '') IS NOT NULL
                           THEN 1
                           ELSE 0
                       END AS cookie_material_available
                FROM tb_medium_account AS a
                LEFT JOIN t_proxy_ips AS p
                  ON p.id = a.proxy_id
                 AND p.status = 1
                 AND p.deleted = 0
                 AND (p.expire_time IS NULL OR p.expire_time > CURRENT_TIMESTAMP)
                WHERE a.id = :account_id AND a.status = 1 AND a.`del` = 0
                """
            ),
            {"account_id": account_id},
        )).mappings().one_or_none()
        if row is None:
            exists = (await self._session.execute(
                text("SELECT status, `del` FROM tb_medium_account WHERE id = :account_id"),
                {"account_id": account_id},
            )).mappings().one_or_none()
            if exists is None:
                raise AccountNotFoundError("媒体账号不存在")
            raise AccountUnavailableError("媒体账号未启用或已删除")

        try:
            return self._account_from_row(row, include_payload=False)
        finally:
            await self._session.rollback()

    async def get_with_payload(self, account_id: int) -> MediumAccount:
        row = (await self._session.execute(
            text(
                """
                SELECT a.id, a.platform, a.status, a.sync_status, a.`del`,
                       a.cookie, a.cookies, a.session_id,
                       a.cookie_version, a.refresh_fence, a.refresh_result, a.refresh_code,
                       a.proxy_id, a.cookie_proxy_fingerprint,
                       p.id AS resolved_proxy_id, p.protocol AS proxy_protocol,
                       p.ip AS proxy_ip, p.port AS proxy_port,
                       p.username AS proxy_username, p.password AS proxy_password,
                       CASE
                           WHEN NULLIF(TRIM(COALESCE(a.cookie, '')), '') IS NOT NULL
                             OR NULLIF(TRIM(COALESCE(a.cookies, '')), '') IS NOT NULL
                           THEN 1
                           ELSE 0
                       END AS cookie_material_available
                FROM tb_medium_account AS a
                LEFT JOIN t_proxy_ips AS p
                  ON p.id = a.proxy_id
                 AND p.status = 1
                 AND p.deleted = 0
                 AND (p.expire_time IS NULL OR p.expire_time > CURRENT_TIMESTAMP)
                WHERE a.id = :account_id AND a.status = 1 AND a.`del` = 0
                """
            ),
            {"account_id": account_id},
        )).mappings().one_or_none()
        if row is None:
            exists = (await self._session.execute(
                text("SELECT status, `del` FROM tb_medium_account WHERE id = :account_id"),
                {"account_id": account_id},
            )).mappings().one_or_none()
            if exists is None:
                raise AccountNotFoundError("媒体账号不存在")
            raise AccountUnavailableError("媒体账号未启用或已删除")
        try:
            return self._account_from_row(row, include_payload=True)
        finally:
            await self._session.rollback()

    @staticmethod
    def _account_from_row(
        row: Mapping[str, Any], *, include_payload: bool
    ) -> MediumAccount:
        proxy = build_account_proxy(row)
        proxy_fingerprint = proxy.fingerprint if proxy is not None else None
        cookie_material_available = int(row["cookie_material_available"])
        cookie_proxy_fingerprint = row.get("cookie_proxy_fingerprint")
        cookie_matches_proxy = cookie_proxy_fingerprint == proxy_fingerprint
        return MediumAccount(
            id=int(row["id"]),
            platform=str(row["platform"]),
            status=int(row["status"]),
            sync_status=(
                int(row["sync_status"])
                if cookie_material_available and cookie_matches_proxy
                else 0
            ),
            deleted=int(row["del"]),
            cookie=row["cookie"] if include_payload else None,
            cookies=row["cookies"] if include_payload else None,
            session_id=row["session_id"] if include_payload else None,
            cookie_version=int(row["cookie_version"]),
            refresh_fence=int(row["refresh_fence"]),
            refresh_result=str(row["refresh_result"]),
            refresh_code=row["refresh_code"],
            proxy_id=row.get("proxy_id"),
            proxy=proxy,
            cookie_proxy_fingerprint=cookie_proxy_fingerprint,
        )

    async def get_login_secret(self, account_id: int) -> LoginSecret:
        row = (await self._session.execute(
            text(
                """
                SELECT account, password
                FROM tb_medium_account
                WHERE id = :account_id AND status = 1 AND `del` = 0
                """
            ),
            {"account_id": account_id},
        )).mappings().one_or_none()
        if row is None:
            raise AccountNotFoundError("媒体账号不存在")
        username = row["account"]
        password = row["password"]
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise LoginCredentialsUnavailableError("媒体账号未配置登录凭据")
        await self._session.rollback()
        return LoginSecret(username=username, password=password)

    async def claim_refresh(self, account_id: int, observed_version: int) -> int | None:
        try:
            result = await self._session.execute(
                text(
                    """
                    UPDATE tb_medium_account
                    SET refresh_fence = refresh_fence + 1,
                        refresh_result = 'pending',
                        refresh_code = NULL
                    WHERE id = :account_id AND cookie_version = :observed_version
                    """
                ),
                {"account_id": account_id, "observed_version": observed_version},
            )
            if result.rowcount != 1:
                await self._session.rollback()
                return None
            fence = (await self._session.execute(
                text("SELECT refresh_fence FROM tb_medium_account WHERE id = :account_id"),
                {"account_id": account_id},
            )).scalar_one()
            await self._session.commit()
            return int(fence)
        except Exception as error:
            await self._session.rollback()
            raise PersistenceUnavailableError("刷新状态持久化失败") from error

    async def save_login(
        self,
        account_id: int,
        observed_version: int,
        fence: int,
        *,
        cookie_header: str,
        cookies: str,
        session_id: str | None,
        proxy_fingerprint: str | None = None,
    ) -> int | None:
        try:
            result = await self._session.execute(
                text(
                    """
                    UPDATE tb_medium_account
                    SET cookie = :cookie_header, cookies = :cookies, session_id = :session_id,
                        sync_status = 1, cookie_version = cookie_version + 1,
                        refresh_result = 'ready', refresh_code = NULL,
                        cookie_proxy_fingerprint = :proxy_fingerprint
                    WHERE id = :account_id AND cookie_version = :observed_version
                      AND refresh_fence = :fence
                    """
                ),
                {
                    "account_id": account_id,
                    "observed_version": observed_version,
                    "fence": fence,
                    "cookie_header": cookie_header,
                    "cookies": cookies,
                    "session_id": session_id,
                    "proxy_fingerprint": proxy_fingerprint,
                },
            )
            if result.rowcount != 1:
                await self._session.rollback()
                return None
            version = (await self._session.execute(
                text("SELECT cookie_version FROM tb_medium_account WHERE id = :account_id"),
                {"account_id": account_id},
            )).scalar_one()
            await self._session.commit()
            return int(version)
        except Exception as error:
            await self._session.rollback()
            raise PersistenceUnavailableError("登录 Cookie 持久化失败") from error

    async def mark_refresh_failure(
        self,
        account_id: int,
        observed_version: int,
        fence: int,
        *,
        result: str,
        code: str,
    ) -> int | None:
        try:
            update = await self._session.execute(
                text(
                    """
                    UPDATE tb_medium_account
                    SET cookie = NULL, cookies = NULL, session_id = NULL,
                        sync_status = 0, cookie_version = cookie_version + 1,
                        refresh_result = :result, refresh_code = :code,
                        cookie_proxy_fingerprint = NULL
                    WHERE id = :account_id AND cookie_version = :observed_version
                      AND refresh_fence = :fence
                    """
                ),
                {
                    "account_id": account_id,
                    "observed_version": observed_version,
                    "fence": fence,
                    "result": result,
                    "code": code,
                },
            )
            if update.rowcount != 1:
                await self._session.rollback()
                return None
            version = (await self._session.execute(
                text("SELECT cookie_version FROM tb_medium_account WHERE id = :account_id"),
                {"account_id": account_id},
            )).scalar_one()
            await self._session.commit()
            return int(version)
        except Exception as error:
            await self._session.rollback()
            raise PersistenceUnavailableError("登录失败状态持久化失败") from error


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_value(self, platform: str, category_name: str) -> str:
        row = (await self._session.execute(
            text(
                """
                SELECT category_value
                FROM tb_medium_platform_category
                WHERE platform = :platform
                  AND category_name = :category_name
                  AND status = 1
                  AND `del` = 0
                """
            ),
            {"platform": platform, "category_name": category_name},
        )).mappings().one_or_none()
        if row is None:
            raise CategoryNotFoundError("该平台未配置此分类")
        return str(row["category_value"])
