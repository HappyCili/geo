from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from app.domain import Credentials, MediumAccount
from app.errors import CredentialError


def _parse_cookie_header(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for fragment in value.split(";"):
        name, separator, cookie_value = fragment.strip().partition("=")
        if separator and name:
            result[name] = cookie_value
    return result


def _pairs_from_cookie_list(values: Iterable[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Cookie 数组成员必须为对象")
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            raise ValueError("Cookie 数组成员缺少 name 或 value")
        result[name] = value
    return result


def _parse_cookie_json(raw: str) -> dict[str, str]:
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        if not all(isinstance(name, str) and isinstance(value, str) for name, value in parsed.items()):
            raise ValueError("Cookie 对象必须是字符串键值对")
        return dict(parsed)
    if isinstance(parsed, list):
        return _pairs_from_cookie_list(parsed)
    raise ValueError("cookies 必须为对象或数组 JSON")


def get_credentials(account: MediumAccount) -> Credentials:
    """在公共层将数据库 Cookie 字段规范化为平台 model 可用的凭据。"""
    parsed: dict[str, str] = {}
    if account.cookies:
        try:
            parsed = _parse_cookie_json(account.cookies)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}

    if parsed:
        cookie_header = "; ".join(f"{name}={value}" for name, value in parsed.items())
    elif account.cookie and account.cookie.strip():
        cookie_header = account.cookie.strip()
        parsed = _parse_cookie_header(cookie_header)
    else:
        raise CredentialError("媒体账号未配置可用 Cookie")

    if not parsed:
        raise CredentialError("媒体账号 Cookie 格式无效")
    return Credentials(
        cookie_header=cookie_header,
        cookies=parsed,
        session_id=account.session_id.strip() if account.session_id else None,
    )
