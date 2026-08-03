from __future__ import annotations

from app.errors import UnsupportedPlatformError
from app.models.base import ArticlePublisher
from app.models.cnblogs import CnblogsPublisher
from app.models.hepan import HepanPublisher
from app.models.lieju import LiejuPublisher


class PublisherRegistry:
    def __init__(self, publishers: list[ArticlePublisher] | None = None) -> None:
        registered = publishers or [HepanPublisher(), CnblogsPublisher(), LiejuPublisher()]
        self._publishers = {publisher.platform: publisher for publisher in registered}

    def get(self, platform: str) -> ArticlePublisher:
        try:
            return self._publishers[platform]
        except KeyError as error:
            raise UnsupportedPlatformError(f"暂不支持平台：{platform}") from error
