from __future__ import annotations

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


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, account_id: int) -> MediumAccount:
        row = (await self._session.execute(
            text(
                """
                SELECT id, platform, status, sync_status, `del`,
                       cookie_version, refresh_fence, refresh_result, refresh_code,
                       CASE
                           WHEN NULLIF(TRIM(COALESCE(cookie, '')), '') IS NOT NULL
                             OR NULLIF(TRIM(COALESCE(cookies, '')), '') IS NOT NULL
                           THEN 1
                           ELSE 0
                       END AS cookie_material_available
                FROM tb_medium_account
                WHERE id = :account_id AND status = 1 AND `del` = 0
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

        account = MediumAccount(
            id=int(row["id"]),
            platform=str(row["platform"]),
            status=int(row["status"]),
            sync_status=(
                int(row["sync_status"])
                if int(row["cookie_material_available"])
                else 0
            ),
            deleted=int(row["del"]),
            cookie=None,
            cookies=None,
            session_id=None,
            cookie_version=int(row["cookie_version"]),
            refresh_fence=int(row["refresh_fence"]),
            refresh_result=str(row["refresh_result"]),
            refresh_code=row["refresh_code"],
        )
        await self._session.rollback()
        return account

    async def get_with_payload(self, account_id: int) -> MediumAccount:
        row = (await self._session.execute(
            text(
                """
                SELECT id, platform, status, sync_status, `del`, cookie, cookies, session_id,
                       cookie_version, refresh_fence, refresh_result, refresh_code,
                       CASE
                           WHEN NULLIF(TRIM(COALESCE(cookie, '')), '') IS NOT NULL
                             OR NULLIF(TRIM(COALESCE(cookies, '')), '') IS NOT NULL
                           THEN 1
                           ELSE 0
                       END AS cookie_material_available
                FROM tb_medium_account
                WHERE id = :account_id AND status = 1 AND `del` = 0
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
        account = MediumAccount(
            id=int(row["id"]),
            platform=str(row["platform"]),
            status=int(row["status"]),
            sync_status=(
                int(row["sync_status"])
                if int(row["cookie_material_available"])
                else 0
            ),
            deleted=int(row["del"]),
            cookie=row["cookie"],
            cookies=row["cookies"],
            session_id=row["session_id"],
            cookie_version=int(row["cookie_version"]),
            refresh_fence=int(row["refresh_fence"]),
            refresh_result=str(row["refresh_result"]),
            refresh_code=row["refresh_code"],
        )
        await self._session.rollback()
        return account

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
    ) -> int | None:
        try:
            result = await self._session.execute(
                text(
                    """
                    UPDATE tb_medium_account
                    SET cookie = :cookie_header, cookies = :cookies, session_id = :session_id,
                        sync_status = 1, cookie_version = cookie_version + 1,
                        refresh_result = 'ready', refresh_code = NULL
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
                        refresh_result = :result, refresh_code = :code
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
