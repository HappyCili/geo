from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from app.domain import Credentials, PlatformFieldValue, PublishRequirements, PublishResult
from app.schemas import ContentType
from app.utils.request import BaseRequest


class ArticlePublisher(BaseRequest, ABC):
    platform: str

    @abstractmethod
    async def publish_article(
        self,
        *,
        title: str,
        category: str | None,
        category_value: str | None,
        content: str,
        content_type: ContentType,
        credentials: Credentials,
        platform_fields: Mapping[str, PlatformFieldValue],
    ) -> PublishResult:
        """将文章发布到本平台；不需要分类的平台可接收空分类。"""

    async def get_requirements(
        self,
        *,
        category: str | None,
        category_value: str | None,
        credentials: Credentials,
    ) -> PublishRequirements:
        return PublishRequirements(publishable=True, captcha_required=False, fields=())
