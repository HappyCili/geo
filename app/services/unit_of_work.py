from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import AccountRepository, CategoryRepository


class UnitOfWork:
    """绑定单次请求的短生命周期数据库会话与仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.accounts = AccountRepository(session)
        self.categories = CategoryRepository(session)

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            await self.session.rollback()
