from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ContentType(str, Enum):
    MARKDOWN = "markdown"
    HTML = "html"


class BasePublishArticleRequest(BaseModel):
    account_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1)
    content_type: ContentType = ContentType.MARKDOWN
    platform_fields: dict[str, str | list[str]] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("category")
    @classmethod
    def trim_optional_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls.trim_required_text(value)

    @field_validator("content")
    @classmethod
    def require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("platform_fields")
    @classmethod
    def validate_platform_fields(
        cls, value: dict[str, str | list[str]]
    ) -> dict[str, str | list[str]]:
        if any(not key.strip() for key in value):
            raise ValueError("platform_fields keys must not be blank")
        return value


class PublishArticleRequest(BasePublishArticleRequest):
    category: str = Field(min_length=1, max_length=128)


class CnblogsPublishArticleRequest(BasePublishArticleRequest):
    pass


class PublishArticleResponse(BaseModel):
    account_id: int
    platform: str
    success: bool
    http_status: int
    article_url: str | None = None
    message: str


class PublishFieldOptionResponse(BaseModel):
    value: str
    label: str


class PublishFieldRequirementResponse(BaseModel):
    key: str
    label: str
    control_type: Literal["text", "textarea", "select", "radio", "checkbox"]
    required: bool
    multiple: bool = False
    default: str | list[str] | None = None
    options: list[PublishFieldOptionResponse] = Field(default_factory=list)
    validation: str | None = None
    validation_message: str | None = None


class PublishRequirementsResponse(BaseModel):
    account_id: int
    platform: str
    category: str | None
    publishable: bool
    captcha_required: bool
    fields: list[PublishFieldRequirementResponse] = Field(default_factory=list)
