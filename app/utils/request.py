from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from typing import Any, Optional, Tuple, Union

import httpx

from app.logging_config import configure_app_logging


logger = logging.getLogger("app.request")

_BINARY_CONTENT_TYPES = (
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/x-",
    "audio/",
    "font/",
    "image/",
    "video/",
)


def _response_request(response: Any) -> Any | None:
    history = getattr(response, "history", None)
    if history:
        first_request = getattr(history[0], "request", None)
        if first_request is not None:
            return first_request
    try:
        return getattr(response, "request", None)
    except (RuntimeError, TypeError, ValueError):
        return None


def _request_content(request: Any) -> Any | None:
    if request is None:
        return None
    for attribute in ("content", "body"):
        try:
            value = getattr(request, attribute, None)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        if value is not None:
            return value
    return None


def _request_body_value(
    *,
    data: Any,
    json_data: Any,
    files: Any,
    content: Any,
) -> Any:
    if json_data is not None:
        return json_data
    if data is not None:
        return data
    if content is not None:
        return content
    return files


def _response_status(response: Any) -> int | str:
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", "?")
    return status


def _response_elapsed_seconds(response: Any) -> float:
    try:
        elapsed = getattr(response, "elapsed", None)
    except (RuntimeError, TypeError, ValueError):
        return 0.0
    if elapsed is None:
        return 0.0
    total_seconds = getattr(elapsed, "total_seconds", None)
    if not callable(total_seconds):
        return 0.0
    try:
        return float(total_seconds())
    except (RuntimeError, TypeError, ValueError):
        return 0.0


def _response_headers(response: Any) -> Mapping[str, Any]:
    headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _response_is_binary(response: Any) -> bool:
    content_type = str(_response_headers(response).get("content-type", "")).lower()
    if any(content_type.startswith(prefix) for prefix in _BINARY_CONTENT_TYPES):
        return True
    content = getattr(response, "content", b"")
    return isinstance(content, bytes) and b"\x00" in content[:1024]


def _response_debug_text(response: Any, limit: int | None = None) -> str:
    try:
        text = response.text
    except (AttributeError, RuntimeError, TypeError, ValueError):
        content = getattr(response, "content", b"")
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)
    value = str(text)
    return value if limit is None else value[:limit]


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class BaseRequest:
    """Shared async HTTP request implementation for project integrations."""

    retry_delay_seconds = 2.0
    response_debug_preview_length: int | None = None

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
        *,
        client_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        configure_app_logging()
        self._client_factory = client_factory or httpx.AsyncClient
        self._client_kwargs = dict(client_kwargs or {})
        self._client: Any | None = None
        self._client_lock: asyncio.Lock | None = None
        self.cookies: dict[str, str] = {}

    def _ensure_request_state(self) -> None:
        if not hasattr(self, "_client_factory"):
            self._client_factory = httpx.AsyncClient
        if not hasattr(self, "_client_kwargs"):
            self._client_kwargs = {}
        if not hasattr(self, "_client"):
            self._client = None
        if not hasattr(self, "_client_lock"):
            self._client_lock = None
        if not hasattr(self, "cookies"):
            self.cookies = {}

    async def _get_client(self) -> Any:
        self._ensure_request_state()
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        async with self._client_lock:
            if self._client is None:
                self._client = await _await_if_needed(
                    self._client_factory(**self._client_kwargs)
                )
            return self._client

    async def close_request_client(self) -> None:
        self._ensure_request_state()
        client, self._client = self._client, None
        close = getattr(client, "aclose", None)
        if callable(close):
            await _await_if_needed(close())

    async def __aenter__(self) -> "BaseRequest":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close_request_client()

    @staticmethod
    def _request_kwargs(
        *,
        params: Optional[dict[str, Any]],
        data: Optional[Union[dict[str, Any], Iterable[Tuple[str, Any]], bytes]],
        json: Optional[Any],
        headers: Optional[MutableMapping[str, str]],
        cookies: Optional[MutableMapping[str, str]],
        files: Optional[Mapping[str, Any]],
        auth: Optional[Union[Tuple[str, str], httpx.Auth]],
        timeout: Optional[Union[float, httpx.Timeout]],
        follow_redirects: bool,
        extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "params": params,
            "data": data,
            "json": json,
            "headers": headers,
            "cookies": cookies,
            "files": files,
            "auth": auth,
            "timeout": timeout,
            "follow_redirects": follow_redirects,
        }
        request_kwargs.update(extra)
        return request_kwargs

    @staticmethod
    def _fallback_request_kwargs(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in request_kwargs.items()
            if value is not None and key != "follow_redirects"
        }

    @staticmethod
    def _method_request_kwargs(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in request_kwargs.items()
            if value is not None and (key != "follow_redirects" or value)
        }

    @staticmethod
    def _requests_compatible_kwargs(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
        compatible = BaseRequest._method_request_kwargs(request_kwargs)
        compatible["allow_redirects"] = compatible.pop("follow_redirects", False)
        return compatible

    async def _send_once(
        self,
        client: Any,
        method: str,
        url: str,
        args: tuple[Any, ...],
        request_kwargs: Mapping[str, Any],
        *,
        stream: bool,
    ) -> Any:
        if stream and callable(getattr(client, "stream", None)):
            stream_method = client.stream
            async with stream_method(method, url, *args, **request_kwargs) as response:
                read = getattr(response, "aread", None)
                if callable(read):
                    await _await_if_needed(read())
                return response

        request_method = getattr(client, "request", None)
        if callable(request_method):
            try:
                return await _await_if_needed(
                    request_method(method, url, *args, **request_kwargs)
                )
            except TypeError:
                return await _await_if_needed(
                    request_method(
                        method,
                        url,
                        *args,
                        **self._requests_compatible_kwargs(request_kwargs),
                    )
                )

        method_request = getattr(client, method.lower(), None)
        if not callable(method_request):
            raise TypeError(f"HTTP client does not support {method.upper()} requests")
        try:
            return await _await_if_needed(
                method_request(
                    url,
                    *args,
                    **self._method_request_kwargs(request_kwargs),
                )
            )
        except TypeError:
            return await _await_if_needed(
                method_request(url, *args, **self._fallback_request_kwargs(request_kwargs))
            )

    def _is_binary_response(self, response: Any) -> bool:
        return _response_is_binary(response)

    def _get_response_debug_text(self, response: Any) -> str:
        return _response_debug_text(response)

    def _log_response(
        self,
        url: str,
        response: Any,
        *,
        method: str = "GET",
        headers: Optional[MutableMapping[str, str]],
        params: Optional[dict[str, Any]],
        data: Optional[Union[dict[str, Any], Iterable[Tuple[str, Any]], bytes]],
        json_data: Optional[Any],
        cookies: Optional[MutableMapping[str, str]],
        files: Optional[Mapping[str, Any]] = None,
        content: Any = None,
        log_request_info: bool,
        log_response_info: bool,
    ) -> None:
        request = _response_request(response)
        request_method = str(getattr(request, "method", method)).upper()
        request_url = str(getattr(request, "url", url))
        if request is None and params:
            request_url = str(httpx.URL(request_url, params=params))
        request_headers_value = getattr(request, "headers", None)
        if not isinstance(request_headers_value, Mapping):
            request_headers_value = dict(headers or {})
            if cookies:
                request_headers_value.setdefault(
                    "Cookie",
                    "; ".join(f"{name}={value}" for name, value in cookies.items()),
                )
        request_body = _request_content(request)
        if request_body is None:
            request_body = _request_body_value(
                data=data,
                json_data=json_data,
                files=files,
                content=content,
            )

        logger.info(
            "%s--->[%s]%.3fs",
            request_url,
            _response_status(response),
            _response_elapsed_seconds(response),
        )
        if log_request_info:
            logger.debug(
                "request method=%s request.url=%s request.headers=%r request.body=%r",
                request_method,
                request_url,
                dict(request_headers_value),
                request_body,
            )
        if log_response_info:
            logger.debug(
                "response status_code=%s response.url=%s elapsed=%.3fs response.text=%s",
                _response_status(response),
                str(getattr(response, "url", url)),
                _response_elapsed_seconds(response),
                self._get_response_debug_text(response),
            )

    def get_response_cookies(self, response: Any, update: bool = True) -> dict[str, str]:
        response_cookies = getattr(response, "cookies", None)
        jar = getattr(response_cookies, "jar", None)
        if jar is None:
            return {}
        new_cookies = {
            str(cookie.name): str(cookie.value)
            for cookie in jar
            if getattr(cookie, "name", None) is not None
        }
        if not update or not new_cookies:
            return new_cookies
        self.cookies.update(new_cookies)
        return dict(self.cookies)

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[Union[dict[str, Any], Iterable[Tuple[str, Any]], bytes]] = None,
        json: Optional[Any] = None,
        headers: Optional[MutableMapping[str, str]] = None,
        cookies: Optional[MutableMapping[str, str]] = None,
        files: Optional[Mapping[str, Any]] = None,
        auth: Optional[Union[Tuple[str, str], httpx.Auth]] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = 10,
        allow_redirects: bool = False,
        retry: int = 2,
        update_cookie: bool = True,
        log_request_info: bool = True,
        log_response_info: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one async request with a shared client, retry, logging and cookies.

        ``_client`` is an internal escape hatch for callers that already own an
        async client context. It keeps connection and cookie lifecycle unchanged
        while routing the actual HTTP call through this method.
        """
        client = kwargs.pop("_client", None)
        stream = bool(kwargs.pop("stream", False))
        retry_delay = kwargs.pop("retry_delay", self.retry_delay_seconds)
        follow_redirects = bool(kwargs.pop("follow_redirects", allow_redirects))
        if client is None:
            client = await self._get_client()

        request_kwargs = self._request_kwargs(
            params=params,
            data=data,
            json=json,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            follow_redirects=follow_redirects,
            extra=kwargs,
        )
        attempts = max(1, int(retry))
        last_exception: Exception | None = None
        response: Any | None = None
        for attempt in range(attempts):
            try:
                response = await self._send_once(
                    client,
                    method,
                    url,
                    args,
                    request_kwargs,
                    stream=stream,
                )
                break
            except Exception as error:
                last_exception = error
                logger.exception(
                    "request failed (%s/%s): %s request.url=%s request.params=%r "
                    "request.headers=%r request.cookies=%r request.auth=%r request.body=%r",
                    attempt + 1,
                    attempts,
                    method.upper(),
                    url,
                    params,
                    dict(headers or {}),
                    cookies,
                    auth,
                    _request_body_value(
                        data=data,
                        json_data=json,
                        files=files,
                        content=request_kwargs.get("content"),
                    ),
                )
                if attempt + 1 < attempts and retry_delay:
                    await asyncio.sleep(float(retry_delay))

        if response is None:
            if last_exception is not None:
                raise last_exception
            raise RuntimeError(f"request failed without response: {method} {url}")
        self._log_response(
            url,
            response,
            method=method,
            headers=headers,
            params=params,
            data=data,
            json_data=json,
            cookies=cookies,
            files=files,
            content=request_kwargs.get("content"),
            log_request_info=log_request_info,
            log_response_info=log_response_info,
        )
        if update_cookie:
            self.get_response_cookies(response)
        return response


class SyncRequestAdapter:
    """Synchronous companion for legacy CLI scripts and requests.Session users."""

    retry_delay_seconds = BaseRequest.retry_delay_seconds
    response_debug_preview_length = BaseRequest.response_debug_preview_length

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
        *,
        client_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        configure_app_logging()
        self._client_factory = client_factory or httpx.Client
        self._client_kwargs = dict(client_kwargs or {})
        self._client: Any | None = None
        self.cookies: dict[str, str] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(**self._client_kwargs)
        return self._client

    def close_request_client(self) -> None:
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "SyncRequestAdapter":
        return self

    def __exit__(self, *args: object) -> None:
        self.close_request_client()

    @staticmethod
    def _send_once(
        client: Any,
        method: str,
        url: str,
        args: tuple[Any, ...],
        request_kwargs: Mapping[str, Any],
    ) -> Any:
        request_method = getattr(client, "request", None)
        if callable(request_method):
            try:
                return request_method(method, url, *args, **request_kwargs)
            except TypeError:
                return request_method(
                    method,
                    url,
                    *args,
                    **BaseRequest._requests_compatible_kwargs(request_kwargs),
                )
        method_request = getattr(client, method.lower(), None)
        if not callable(method_request):
            raise TypeError(f"HTTP client does not support {method.upper()} requests")
        try:
            return method_request(
                url,
                *args,
                **BaseRequest._method_request_kwargs(request_kwargs),
            )
        except TypeError:
            return method_request(
                url,
                *args,
                **BaseRequest._fallback_request_kwargs(request_kwargs),
            )

    def get_response_cookies(self, response: Any, update: bool = True) -> dict[str, str]:
        response_cookies = getattr(response, "cookies", None)
        jar = getattr(response_cookies, "jar", None)
        if jar is None:
            return {}
        new_cookies = {
            str(cookie.name): str(cookie.value)
            for cookie in jar
            if getattr(cookie, "name", None) is not None
        }
        if not update or not new_cookies:
            return new_cookies
        self.cookies.update(new_cookies)
        return dict(self.cookies)

    def _is_binary_response(self, response: Any) -> bool:
        return _response_is_binary(response)

    def _get_response_debug_text(self, response: Any) -> str:
        return _response_debug_text(response)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        data: Optional[Union[dict[str, Any], Iterable[Tuple[str, Any]], bytes]] = None,
        json: Optional[Any] = None,
        headers: Optional[MutableMapping[str, str]] = None,
        cookies: Optional[MutableMapping[str, str]] = None,
        files: Optional[Mapping[str, Any]] = None,
        auth: Optional[Union[Tuple[str, str], httpx.Auth]] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = 10,
        allow_redirects: bool = False,
        retry: int = 2,
        update_cookie: bool = True,
        log_request_info: bool = True,
        log_response_info: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        client = kwargs.pop("_client", None) or self._get_client()
        retry_delay = kwargs.pop("retry_delay", self.retry_delay_seconds)
        follow_redirects = bool(kwargs.pop("follow_redirects", allow_redirects))
        request_kwargs = BaseRequest._request_kwargs(
            params=params,
            data=data,
            json=json,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            follow_redirects=follow_redirects,
            extra=kwargs,
        )
        attempts = max(1, int(retry))
        last_exception: Exception | None = None
        response: Any | None = None
        for attempt in range(attempts):
            try:
                response = self._send_once(client, method, url, args, request_kwargs)
                break
            except Exception as error:
                last_exception = error
                logger.exception(
                    "request failed (%s/%s): %s request.url=%s request.params=%r "
                    "request.headers=%r request.cookies=%r request.auth=%r request.body=%r",
                    attempt + 1,
                    attempts,
                    method.upper(),
                    url,
                    params,
                    dict(headers or {}),
                    cookies,
                    auth,
                    _request_body_value(
                        data=data,
                        json_data=json,
                        files=files,
                        content=request_kwargs.get("content"),
                    ),
                )
                if attempt + 1 < attempts and retry_delay:
                    time.sleep(float(retry_delay))

        if response is None:
            if last_exception is not None:
                raise last_exception
            raise RuntimeError(f"request failed without response: {method} {url}")
        BaseRequest._log_response(
            self,
            url,
            response,
            method=method,
            headers=headers,
            params=params,
            data=data,
            json_data=json,
            cookies=cookies,
            files=files,
            content=request_kwargs.get("content"),
            log_request_info=log_request_info,
            log_response_info=log_response_info,
        )
        if update_cookie:
            self.get_response_cookies(response)
        return response
