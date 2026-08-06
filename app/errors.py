from __future__ import annotations


class PublishError(Exception):
    """发布流程中可转换为 HTTP 响应的业务错误。"""


class AccountNotFoundError(PublishError):
    pass


class AccountUnavailableError(PublishError):
    pass


class ProxyUnavailableError(AccountUnavailableError):
    pass


class LoginCredentialsUnavailableError(AccountUnavailableError):
    pass


class CategoryNotFoundError(PublishError):
    pass


class CredentialError(PublishError):
    pass


class LoginExpiredError(CredentialError):
    def __init__(self, message: str, *, retry_safe: bool = False) -> None:
        super().__init__(message)
        self.retry_safe = retry_safe


class PlatformMismatchError(PublishError):
    pass


class RefreshUnavailableError(PublishError):
    pass


class PersistenceUnavailableError(PublishError):
    pass


class LoginNetworkError(PublishError):
    pass


class LoginProtocolError(PublishError):
    pass


class UnsupportedPlatformError(PublishError):
    pass


class PublisherConfigurationError(PublishError):
    pass


class PlatformFieldsValidationError(PublisherConfigurationError):
    def __init__(
        self,
        message: str,
        *,
        missing_fields: list[str] | None = None,
        unknown_fields: list[str] | None = None,
        invalid_fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing_fields = missing_fields or []
        self.unknown_fields = unknown_fields or []
        self.invalid_fields = invalid_fields or {}


class CaptchaRequiredError(PublishError):
    pass


class UpstreamPublishError(PublishError):
    def __init__(self, message: str, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


def http_status_for_error(error: PublishError) -> int:
    if isinstance(error, AccountNotFoundError):
        return 404
    if isinstance(error, (AccountUnavailableError, CredentialError, CaptchaRequiredError)):
        return 409
    if isinstance(error, (CategoryNotFoundError, PublisherConfigurationError, PlatformMismatchError)):
        return 422
    if isinstance(error, (RefreshUnavailableError, PersistenceUnavailableError)):
        return 503
    if isinstance(error, (LoginNetworkError, LoginProtocolError)):
        return 502
    if isinstance(error, UnsupportedPlatformError):
        return 501
    if isinstance(error, UpstreamPublishError):
        return 502
    return 500
