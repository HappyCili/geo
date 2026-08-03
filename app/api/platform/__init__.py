"""Platform-specific publication API modules."""

from app.utils.publisher_registry import PublisherRegistry


def create_default_publisher_registry() -> PublisherRegistry:
    """Build the application registry without coupling the utility layer to platforms."""
    from app.api.platform.cnblogs.article_publisher import CnblogsPublisher
    from app.api.platform.hepan.article_publisher import HepanPublisher
    from app.api.platform.lieju.article_publisher import LiejuPublisher

    return PublisherRegistry([HepanPublisher(), CnblogsPublisher(), LiejuPublisher()])


__all__ = ["create_default_publisher_registry"]
