from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import (
    AccountNotFoundError,
    AccountUnavailableError,
    CaptchaRequiredError,
    CategoryNotFoundError,
    LoginExpiredError,
    LoginFailedError,
    LoginNetworkError,
    LoginProtocolError,
    PersistenceUnavailableError,
    PlatformFieldsValidationError,
    PlatformMismatchError,
    PublishLimitError,
    PublishError,
    ProxyUnavailableError,
    PublisherConfigurationError,
    RefreshUnavailableError,
    UnsupportedPlatformError,
    UpstreamPublishError,
)
from app.services import PublishService


def get_publish_service(session: AsyncSession = Depends(get_session)) -> PublishService:
    return PublishService(session)


def _detail(code: str, message: str, **extra: object) -> dict[str, object]:
    return {"code": code, "message": message, **extra}


def http_error(error: PublishError) -> HTTPException:
    if isinstance(error, AccountNotFoundError):
        return HTTPException(status_code=404, detail=_detail("account_not_found", str(error)))
    if isinstance(error, PlatformMismatchError):
        return HTTPException(status_code=422, detail=_detail("platform_mismatch", str(error)))
    if isinstance(error, CaptchaRequiredError):
        return HTTPException(status_code=409, detail=_detail("captcha_required", str(error)))
    if isinstance(error, ProxyUnavailableError):
        return HTTPException(status_code=409, detail=_detail("proxy_unavailable", str(error)))
    if isinstance(error, LoginFailedError):
        return HTTPException(status_code=409, detail=_detail("login_failed", str(error)))
    if isinstance(error, LoginExpiredError):
        return HTTPException(status_code=409, detail=_detail("login_expired", str(error)))
    if isinstance(error, PublishLimitError):
        return HTTPException(status_code=429, detail=_detail("publish_limit_reached", str(error)))
    if isinstance(error, (AccountUnavailableError,)):
        return HTTPException(status_code=409, detail=_detail("account_unavailable", str(error)))
    if isinstance(error, PlatformFieldsValidationError):
        return HTTPException(
            status_code=422,
            detail=_detail(
                "invalid_platform_fields",
                str(error),
                missing_fields=error.missing_fields,
                unknown_fields=error.unknown_fields,
                invalid_fields=error.invalid_fields,
            ),
        )
    if isinstance(error, (CategoryNotFoundError, PublisherConfigurationError)):
        return HTTPException(status_code=422, detail=_detail("invalid_request", str(error)))
    if isinstance(error, UnsupportedPlatformError):
        return HTTPException(status_code=501, detail=_detail("unsupported_platform", str(error)))
    if isinstance(error, LoginNetworkError):
        return HTTPException(status_code=502, detail=_detail("login_network_error", str(error)))
    if isinstance(error, LoginProtocolError):
        return HTTPException(status_code=502, detail=_detail("login_protocol_error", str(error)))
    if isinstance(error, (RefreshUnavailableError, PersistenceUnavailableError)):
        code = "refresh_unavailable" if isinstance(error, RefreshUnavailableError) else "persistence_unavailable"
        return HTTPException(status_code=503, detail=_detail(code, str(error)))
    if isinstance(error, UpstreamPublishError):
        return HTTPException(status_code=502, detail=_detail("upstream_publish_error", str(error)))
    return HTTPException(status_code=500, detail=_detail("internal_error", "发布服务异常"))
