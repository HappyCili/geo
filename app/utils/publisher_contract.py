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
        category: str,
        category_value: str,
        content: str,
        content_type: ContentType,
        credentials: Credentials,
        platform_fields: Mapping[str, PlatformFieldValue],
    ) -> PublishResult:
        """将文章发布到本平台。category 保留用户传入的中文分类名。"""

    async def get_requirements(
        self,
        *,
        category: str,
        category_value: str,
        credentials: Credentials,
    ) -> PublishRequirements:
        return PublishRequirements(publishable=True, captcha_required=False, fields=())
