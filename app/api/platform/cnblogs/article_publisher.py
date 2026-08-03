from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import httpx

from app.config import get_settings
from app.domain import Credentials, PlatformFieldValue, PublishResult
from app.errors import LoginExpiredError, PublisherConfigurationError, UpstreamPublishError
from app.utils.publisher_contract import ArticlePublisher
from app.schemas import ContentType


API_URL = "https://i.cnblogs.com/api/posts"
REFERER = "https://i.cnblogs.com/articles/edit"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"
SESSION_VALIDATION_ERROR = "会话校验失败"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CnblogsPublisher(ArticlePublisher):
    platform = "cnblogs"

    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
        super().__init__(client_factory=client_factory)
        self._timeout = get_settings().request_timeout_seconds

    @staticmethod
    def _category_ids(category: str, category_value: str) -> list[int]:
        parts = [part.strip() for part in category_value.split(",") if part.strip()]
        if not parts:
            raise PublisherConfigurationError(f"CNBlogs 分类 {category} 未配置分类 ID")
        try:
            return [int(part) for part in parts]
        except ValueError as error:
            raise PublisherConfigurationError(f"CNBlogs 分类 {category} 的分类 ID 必须为整数") from error

    @staticmethod
    def _headers(credentials: Credentials) -> dict[str, str]:
        xsrf_token = credentials.cookies.get("XSRF-TOKEN")
        if not xsrf_token:
            raise UpstreamPublishError("CNBlogs Cookie 缺少 XSRF-TOKEN")
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": credentials.cookie_header,
            "Origin": "https://i.cnblogs.com",
            "Referer": REFERER,
            "User-Agent": USER_AGENT,
            "X-XSRF-TOKEN": unquote(xsrf_token),
            "sessionId": credentials.session_id or str(uuid.uuid4()),
        }

    @staticmethod
    def _article_url(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("url", "postUrl", "link"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            return CnblogsPublisher._article_url(data)
        return None

    @staticmethod
    def _is_login_redirect(response: httpx.Response) -> bool:
        headers = getattr(response, "headers", {})
        location = headers.get("location", "").lower()
        return bool(getattr(response, "is_redirect", False) and "login" in location)

    @staticmethod
    def _is_session_validation_failure(response: httpx.Response) -> bool:
        """识别 CNBlogs 将失效防伪会话包装为 HTTP 400 的业务响应。"""
        if response.status_code != 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        errors = payload.get("errors")
        return isinstance(errors, list) and any(
            isinstance(error, str) and SESSION_VALIDATION_ERROR in error
            for error in errors
        )

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
        payload = {
            "id": None,
            "postType": 2,
            "accessPermission": 0,
            "title": title,
            "url": None,
            "postBody": content,
            "categoryIds": self._category_ids(category, category_value),
            "categories": None,
            "collectionIds": [],
            "inSiteCandidate": False,
            "inSiteHome": False,
            "siteCategoryId": None,
            "blogTeamIds": None,
            "isPublished": True,
            "displayOnHomePage": False,
            "isAllowComments": True,
            "includeInMainSyndication": False,
            "isPinned": False,
            "showBodyWhenPinned": False,
            "isOnlyForRegisterUser": False,
            "isUpdateDateAdded": True,
            "entryName": None,
            "description": None,
            "featuredImage": None,
            "tags": None,
            "password": None,
            "publishAt": None,
            "datePublished": _utc_timestamp(),
            "dateUpdated": None,
            "isMarkdown": content_type is ContentType.MARKDOWN,
            "isDraft": True,
            "isAigc": False,
            "autoDesc": None,
            "changePostType": False,
            "blogId": 0,
            "author": None,
            "removeScript": False,
            "clientInfo": None,
            "changeCreatedTime": False,
            "canChangeCreatedTime": False,
            "isContributeToImpressiveBugActivity": False,
            "usingEditorId": 5,
            "sourceUrl": None,
        }
        try:
            async with self._client_factory(
                headers=self._headers(credentials), timeout=self._timeout
            ) as client:
                response = await self.request("POST", API_URL, json=payload, _client=client)
                if (
                    response.status_code in {401, 403}
                    or self._is_login_redirect(response)
                    or self._is_session_validation_failure(response)
                ):
                    raise LoginExpiredError("CNBlogs 登录态已过期", retry_safe=True)
                response.raise_for_status()
        except LoginExpiredError:
            raise
        except httpx.HTTPError as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            raise UpstreamPublishError("CNBlogs 发布请求失败", status) from error

        try:
            response_payload = response.json()
        except ValueError:
            raise UpstreamPublishError("CNBlogs 发布响应格式无效", response.status_code)
        if not isinstance(response_payload, dict) or not response_payload:
            raise UpstreamPublishError("CNBlogs 发布响应格式无效", response.status_code)
        response_status = response_payload.get("status")
        if response_payload.get("success") is False or response_status == "error" or response_status == 0:
            raise UpstreamPublishError("CNBlogs 发布未确认成功", response.status_code)
        return PublishResult(
            success=True,
            http_status=response.status_code,
            article_url=self._article_url(response_payload),
            message="CNBlogs 发布成功",
        )
