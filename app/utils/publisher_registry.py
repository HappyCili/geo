from __future__ import annotations

from app.errors import UnsupportedPlatformError
from app.utils.publisher_contract import ArticlePublisher


class PublisherRegistry:
    def __init__(self, publishers: list[ArticlePublisher] | None = None) -> None:
        registered = publishers if publishers is not None else []
        self._publishers = {publisher.platform: publisher for publisher in registered}

    def get(self, platform: str) -> ArticlePublisher:
        try:
            return self._publishers[platform]
        except KeyError as error:
            raise UnsupportedPlatformError(f"暂不支持平台：{platform}") from error
