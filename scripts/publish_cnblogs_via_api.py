#!/usr/bin/env python3
"""通过项目 HTTP 接口发布现有的 CNBlogs 文章。

用法：
    .venv/bin/python scripts/publish_cnblogs_via_api.py ACCOUNT_ID

唯一的命令行参数为账号 ID。文章内容从项目根目录的
``摘星货蚁物流AI助手推荐.md`` 读取。接口地址可通过
``PUBLISH_API_BASE_URL`` 配置，默认使用本机 8000 端口；分类可通过
``CNBLOGS_CATEGORY`` 覆盖，且必须已在项目数据库中配置分类映射。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence

import httpx

from app.utils.request import SyncRequestAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTICLE_PATH = PROJECT_ROOT / "摘星货蚁物流AI助手推荐.md"
DEFAULT_CATEGORY = "行业资讯"
_TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_REQUEST_TIMEOUT_SECONDS = 180.0


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    with SyncRequestAdapter() as requester:
        return requester.request(method, url, **kwargs)


def _positive_account_id(value: str) -> int:
    try:
        account_id = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("账号 ID 必须是正整数") from error
    if account_id <= 0:
        raise argparse.ArgumentTypeError("账号 ID 必须是正整数")
    return account_id


def _category() -> str:
    category = os.environ.get("CNBLOGS_CATEGORY", DEFAULT_CATEGORY).strip()
    if not category:
        raise ValueError("CNBLOGS_CATEGORY 不能为空")
    return category


def _article() -> tuple[str, str]:
    try:
        content = ARTICLE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise RuntimeError(f"文章文件不存在：{ARTICLE_PATH}") from error
    if not content:
        raise RuntimeError(f"文章文件为空：{ARTICLE_PATH}")

    match = _TITLE_PATTERN.search(content)
    if match is None:
        raise RuntimeError(f"文章文件缺少一级标题：{ARTICLE_PATH}")
    return match.group(1).strip(), content


def build_payload(account_id: int) -> dict[str, Any]:
    """构造发布接口请求体；外部调用时仅需传入账号 ID。"""
    if account_id <= 0:
        raise ValueError("账号 ID 必须是正整数")
    title, content = _article()
    return {
        "account_id": account_id,
        "title": title,
        "category": _category(),
        "content": content,
        "content_type": "markdown",
        "platform_fields": {},
    }


def _api_base_url() -> str:
    base_url = os.environ.get("PUBLISH_API_BASE_URL", "http://127.0.0.1:8000")
    base_url = base_url.strip().rstrip("/")
    if not base_url:
        raise ValueError("PUBLISH_API_BASE_URL 不能为空")
    return base_url


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _raise_api_error(response: httpx.Response, body: Any) -> None:
    detail = body.get("detail", body) if isinstance(body, dict) else body
    raise RuntimeError(
        f"发布接口返回 HTTP {response.status_code}："
        f"{json.dumps(detail, ensure_ascii=False)}"
    )


def _verify_cnblogs_account(account_id: int, category: str) -> None:
    try:
        response = _request(
            "GET",
            f"{_api_base_url()}/cnblogs/articles/publish/requirements",
            params={"account_id": account_id, "category": category},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"发布接口请求失败：{error}") from error

    body = _response_body(response)
    if response.is_error:
        _raise_api_error(response, body)
    if not isinstance(body, dict) or body.get("platform") != "cnblogs":
        raise RuntimeError(f"账号 {account_id} 不是可用的 CNBlogs 账号")


def publish_article(account_id: int) -> dict[str, Any]:
    """确认账号平台后调用项目发布接口，并返回成功响应。"""
    payload = build_payload(account_id)
    _verify_cnblogs_account(account_id, payload["category"])
    try:
        response = _request(
            "POST",
            f"{_api_base_url()}/cnblogs/articles/publish",
            json=payload,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"发布接口请求失败：{error}") from error

    body = _response_body(response)
    if response.is_error:
        _raise_api_error(response, body)
    if not isinstance(body, dict):
        raise RuntimeError("发布接口未返回 JSON 对象")
    return body


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account_id", type=_positive_account_id, help="CNBlogs 媒体账号 ID")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        response = publish_article(args.account_id)
    except (RuntimeError, ValueError) as error:
        print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
