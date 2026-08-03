from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """从环境变量或项目根目录 .env 读取运行时配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = Field(validation_alias="DB_HOST")
    db_port: int = Field(default=3306, gt=0, le=65535, validation_alias="DB_PORT")
    db_user: str = Field(validation_alias="DB_USER")
    db_password: str = Field(validation_alias="DB_PASSWORD")
    db_name: str = Field(validation_alias="DB_NAME")
    db_charset: str = Field(default="utf8mb4", validation_alias="DB_CHARSET")
    cnblogs_username: str | None = Field(
        default=None, validation_alias="CNBLOGS_USERNAME"
    )
    cnblogs_password: SecretStr | None = Field(
        default=None, validation_alias="CNBLOGS_PASSWORD"
    )
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    redis_url: str = Field(validation_alias="REDIS_URL", min_length=1)
    redis_timeout_seconds: float = Field(
        default=1.0, gt=0, validation_alias="REDIS_TIMEOUT_SECONDS"
    )
    cookie_cache_ttl_seconds: int = Field(
        default=86_400, gt=0, validation_alias="COOKIE_CACHE_TTL_SECONDS"
    )
    refresh_lock_ttl_seconds: int = Field(
        default=90, gt=0, validation_alias="REFRESH_LOCK_TTL_SECONDS"
    )
    refresh_lock_renew_seconds: int = Field(
        default=20, gt=0, validation_alias="REFRESH_LOCK_RENEW_SECONDS"
    )
    refresh_wait_seconds: float = Field(
        default=80.0, gt=0, validation_alias="REFRESH_WAIT_SECONDS"
    )
    login_timeout_seconds: float = Field(
        default=75.0, gt=0, validation_alias="LOGIN_TIMEOUT_SECONDS"
    )
    publish_deadline_seconds: float = Field(
        default=150.0, gt=0, validation_alias="PUBLISH_DEADLINE_SECONDS"
    )
    observability_account_salt: str | None = Field(
        default=None,
        min_length=16,
        validation_alias="OBSERVABILITY_ACCOUNT_SALT",
    )
    lieju_tdc_payload_path: Path | None = Field(
        default=None, validation_alias="LIEJU_TDC_PAYLOAD_PATH"
    )
    lieju_tdc_dynamic: bool = Field(
        default=False, validation_alias="LIEJU_TDC_DYNAMIC"
    )
    lieju_tdc_runtime_timeout_seconds: float = Field(
        default=20,
        gt=0,
        validation_alias="LIEJU_TDC_RUNTIME_TIMEOUT_SECONDS",
    )
    lieju_tdc_sha256: str | None = Field(
        default=None, validation_alias="LIEJU_TDC_SHA256"
    )
    lieju_tdc_key: str | None = Field(default=None, validation_alias="LIEJU_TDC_KEY")
    lieju_captcha_answer: str | None = Field(
        default=None, validation_alias="LIEJU_CAPTCHA_ANSWER"
    )
    lieju_chaojiying_username: str | None = Field(
        default=None, validation_alias="LIEJU_CHAOJIYING_USERNAME"
    )
    lieju_chaojiying_password: str | None = Field(
        default=None, validation_alias="LIEJU_CHAOJIYING_PASSWORD"
    )
    lieju_chaojiying_soft_id: str | None = Field(
        default=None, validation_alias="LIEJU_CHAOJIYING_SOFT_ID"
    )
    lieju_chaojiying_type: int = Field(
        default=9602, gt=0, validation_alias="LIEJU_CHAOJIYING_TYPE"
    )
    lieju_chaojiying_point_index: int = Field(
        default=0, ge=0, validation_alias="LIEJU_CHAOJIYING_POINT_INDEX"
    )
    lieju_chaojiying_api_base: str = Field(
        default="https://upload.chaojiying.net",
        validation_alias="LIEJU_CHAOJIYING_API_BASE",
    )
    lieju_chaojiying_timeout_seconds: float = Field(
        default=30,
        gt=0,
        validation_alias="LIEJU_CHAOJIYING_TIMEOUT_SECONDS",
    )
    lieju_captcha_max_attempts: int = Field(
        default=1, ge=1, le=5, validation_alias="LIEJU_CAPTCHA_MAX_ATTEMPTS"
    )
    lieju_captcha_browser_enabled: bool = Field(
        default=False, validation_alias="LIEJU_CAPTCHA_BROWSER_ENABLED"
    )
    lieju_captcha_browser_executable_path: Path | None = Field(
        default=None,
        validation_alias="LIEJU_CAPTCHA_BROWSER_EXECUTABLE_PATH",
    )
    lieju_captcha_browser_headless: bool = Field(
        default=True, validation_alias="LIEJU_CAPTCHA_BROWSER_HEADLESS"
    )
    lieju_captcha_browser_timeout_seconds: float = Field(
        default=30,
        gt=0,
        validation_alias="LIEJU_CAPTCHA_BROWSER_TIMEOUT_SECONDS",
    )
    lieju_tdc_feature_flags: str | None = Field(
        default=None, validation_alias="LIEJU_TDC_FEATURE_FLAGS"
    )
    lieju_tdc_is_new_entry: int = Field(
        default=0, ge=0, le=1, validation_alias="LIEJU_TDC_IS_NEW_ENTRY"
    )
    lieju_tdc_pow_timeout_ms: int = Field(
        default=30_000, gt=0, validation_alias="LIEJU_TDC_POW_TIMEOUT_MS"
    )
    lieju_captcha_timeout_seconds: float = Field(
        default=15, gt=0, validation_alias="LIEJU_CAPTCHA_TIMEOUT_SECONDS"
    )
    lieju_capture_request_path: Path | None = Field(
        default=None, validation_alias="LIEJU_CAPTURE_REQUEST_PATH"
    )
    lieju_tdc_callback: str | None = Field(
        default=None, validation_alias="LIEJU_TDC_CALLBACK"
    )

    @property
    def database_url(self) -> URL:
        return URL.create(
            "mysql+asyncmy",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": self.db_charset},
        )

    def model_post_init(self, __context: object) -> None:
        if self.refresh_lock_renew_seconds >= self.refresh_lock_ttl_seconds:
            raise ValueError("REFRESH_LOCK_RENEW_SECONDS 必须小于 REFRESH_LOCK_TTL_SECONDS")
        if max(self.refresh_wait_seconds, self.login_timeout_seconds) > self.publish_deadline_seconds:
            raise ValueError("刷新等待与登录超时不得超过发布总 deadline")

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("REDIS_URL 不能为空")
        return value


def get_settings() -> Settings:
    return Settings()
