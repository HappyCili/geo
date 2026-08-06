from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


PlatformFieldValue = str | list[str]
FieldControlType = Literal["text", "textarea", "select", "radio", "checkbox"]


@dataclass(frozen=True)
class AccountProxy:
    """Validated proxy configuration bound to one media account."""

    id: int
    protocol: str
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    fingerprint: str


@dataclass(frozen=True)
class MediumAccount:
    id: int
    platform: str
    status: int
    sync_status: int
    deleted: int
    cookie: str | None
    cookies: str | None
    session_id: str | None
    cookie_version: int = 0
    refresh_fence: int = 0
    refresh_result: str = "ready"
    refresh_code: str | None = None
    proxy_id: int | None = None
    proxy: AccountProxy | None = None
    cookie_proxy_fingerprint: str | None = None

    @property
    def proxy_fingerprint(self) -> str | None:
        return self.proxy.fingerprint if self.proxy is not None else None


@dataclass(frozen=True)
class LoginSecret:
    username: str
    password: str


@dataclass(frozen=True)
class LoginResult:
    kind: str
    cookies: dict[str, str] | None = None
    cookie_header: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class Credentials:
    cookie_header: str
    cookies: dict[str, str]
    session_id: str | None
    proxy_fingerprint: str | None = None


@dataclass(frozen=True)
class PublishResult:
    success: bool
    http_status: int
    article_url: str | None
    message: str


@dataclass(frozen=True)
class FieldOption:
    value: str
    label: str


@dataclass(frozen=True)
class PublishFieldRequirement:
    key: str
    label: str
    control_type: FieldControlType
    required: bool
    multiple: bool
    default: PlatformFieldValue | None
    options: tuple[FieldOption, ...]
    validation: str | None
    validation_message: str | None


@dataclass(frozen=True)
class PublishRequirements:
    publishable: bool
    captcha_required: bool
    fields: tuple[PublishFieldRequirement, ...]
