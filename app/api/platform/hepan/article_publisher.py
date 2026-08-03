from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin

import httpx
import markdown
from lxml import html as lxml_html

from app.config import get_settings
from app.domain import Credentials, PlatformFieldValue, PublishResult
from app.errors import LoginExpiredError, PublisherConfigurationError, UpstreamPublishError
from app.utils.publisher_contract import ArticlePublisher
from app.schemas import ContentType


BASE_URL = "https://www.hepan.com"
PUBLISH_URL = f"{BASE_URL}/portal.php"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"


class HepanPublisher(ArticlePublisher):
    platform = "hepan"

    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
        super().__init__(client_factory=client_factory)
        self._timeout = get_settings().request_timeout_seconds

    @staticmethod
    def _content_for_platform(content: str, content_type: ContentType) -> str:
        if content_type is ContentType.MARKDOWN:
            return markdown.markdown(content, extensions=["extra", "sane_lists"], output_format="html5")
        return content

    @staticmethod
    def _parse_formhash(raw_html: bytes) -> str:
        document = lxml_html.fromstring(raw_html.decode("utf-8", errors="replace"))
        values = document.xpath("//form[@id='articleform']//input[@name='formhash']/@value")
        if not values or not values[0]:
            raise UpstreamPublishError("Hepan 未返回发布令牌，登录态可能已过期")
        return str(values[0])

    @staticmethod
    def _is_login_response(response: httpx.Response) -> bool:
        headers = getattr(response, "headers", {})
        location = headers.get("location", "").lower()
        if getattr(response, "is_redirect", False) and "login" in location:
            return True
        document = lxml_html.fromstring(response.content.decode("utf-8", errors="replace"))
        return bool(document.xpath("//form[contains(@action, 'login') or @id='loginform']"))

    @staticmethod
    def _parse_result(raw_html: bytes, response_url: str) -> tuple[bool, str | None, str]:
        document = lxml_html.fromstring(raw_html.decode("utf-8", errors="replace"))
        result_text = " ".join(document.text_content().split())
        edit_links = document.xpath("//a[contains(@href, 'op=edit') and contains(@href, 'aid=')]/@href")
        article_url = urljoin(response_url, str(edit_links[0])) if edit_links else None
        return "发布文章成功" in result_text, article_url, result_text

    @staticmethod
    def _build_parts(title: str, category_id: str, content: str, formhash: str) -> list[tuple[str, tuple[Any, ...]]]:
        def field(name: str, value: Any) -> tuple[str, tuple[None, str]]:
            return name, (None, "" if value is None else str(value))

        return [
            field("title", title),
            field("highlight_style[0]", ""),
            field("highlight_style[1]", ""),
            field("highlight_style[2]", ""),
            field("highlight_style[3]", ""),
            field("htmlname", ""),
            field("oldhtmlname", ""),
            field("pagetitle", ""),
            field("catid", category_id),
            field("from", ""),
            field("fromurl", ""),
            field("dateline", ""),
            field("from_idtype", "tid"),
            field("from_id", "0"),
            field("id", "0"),
            field("idtype", "tid"),
            field("url", ""),
            field("author", ""),
            field("conver", ""),
            field("newalbum", "请输入相册名称"),
            ("file", ("", b"", "application/octet-stream")),
            field("view_albumid", "none"),
            ("file", ("", b"", "application/octet-stream")),
            field("content", content),
            field("summary", ""),
            field("aid", ""),
            field("cid", ""),
            field("attach_ids", "0"),
            field("articlesubmit", "true"),
            field("formhash", formhash),
        ]

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
        category_id = category_value.strip()
        if not category_id or "," in category_id:
            raise PublisherConfigurationError(f"Hepan 分类 {category} 必须配置为单个分类 ID")

        headers = {"Cookie": credentials.cookie_header, "User-Agent": USER_AGENT}
        params = {"mod": "portalcp", "ac": "article", "catid": category_id}
        try:
            async with self._client_factory(headers=headers, timeout=self._timeout) as client:
                edit_response = await self.request(
                    "GET", PUBLISH_URL, params=params, _client=client
                )
                if self._is_login_response(edit_response):
                    raise LoginExpiredError("Hepan 登录态已过期")
                edit_response.raise_for_status()
                formhash = self._parse_formhash(edit_response.content)
                parts = self._build_parts(
                    title,
                    category_id,
                    self._content_for_platform(content, content_type),
                    formhash,
                )
                publish_response = await self.request(
                    "POST",
                    PUBLISH_URL,
                    params={"mod": "portalcp", "ac": "article"},
                    files=parts,
                    allow_redirects=True,
                    _client=client,
                )
                if self._is_login_response(publish_response):
                    raise LoginExpiredError("Hepan 登录态已过期")
                publish_response.raise_for_status()
        except LoginExpiredError:
            raise
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            raise UpstreamPublishError("Hepan 发布请求失败", status) from error

        success, article_url, _ = self._parse_result(publish_response.content, str(publish_response.url))
        if not success:
            raise UpstreamPublishError("Hepan 未确认发布成功", publish_response.status_code)
        return PublishResult(
            success=True,
            http_status=publish_response.status_code,
            article_url=article_url,
            message="Hepan 发布成功",
        )
