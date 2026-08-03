#!/usr/bin/env python3
"""使用从 hepan.com 抓取的请求发布门户文章。

依赖安装：
    .venv/bin/python -m pip install -r requirements.txt

使用方法：
    # 修改 hepan_config.json 中的 cookie、title 和 content，然后运行：
    .venv/bin/python hepan_publish.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import markdown
from lxml import html as lxml_html

from app.utils.request import SyncRequestAdapter


BASE_URL = "https://www.hepan.com"
EDIT_URL = f"{BASE_URL}/portal.php?mod=portalcp&ac=article&catid=121"
PUBLISH_URL = f"{BASE_URL}/portal.php?mod=portalcp&ac=article"
CONFIG_PATH = Path(__file__).with_name("hepan_config.json")
_REQUESTER = SyncRequestAdapter()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

# 浏览器抓包得到的请求头。Cookie、multipart boundary 和 Content-Length 均为动态值。
# HTTP/2 伪请求头（:authority/:method/:path/:scheme）由 HTTP 客户端生成，
# 此处列出仅用于记录浏览器原始请求。
CAPTURED_BROWSER_HEADERS = {
    ":authority": "www.hepan.com",
    ":method": "POST",
    ":path": "/portal.php?mod=portalcp&ac=article",
    ":scheme": "https",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Cache-Control": "max-age=0",
    "Content-Length": "<generated from multipart body>",
    "Content-Type": "multipart/form-data; boundary=<generated boundary>",
    "Cookie": "<hepan_config.json 中的 cookie>",
    "Origin": "null",
    "Priority": "u=0, i",
    "Sec-CH-UA": (
        '"Google Chrome";v="143", "Chromium";v="143", '
        '"Not A(Brand";v="24"'
    ),
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": USER_AGENT,
}

# requests 会自动生成 Accept-Encoding、Cookie、Content-Length、Content-Type
# 以及传输层请求头。不要手动设置 Content-Type，否则请求头中的 boundary
# 可能与 multipart 请求体使用的 boundary 不一致。
PUBLISH_HEADERS = {
    "Accept": CAPTURED_BROWSER_HEADERS["Accept"],
    "Accept-Language": CAPTURED_BROWSER_HEADERS["Accept-Language"],
    "Cache-Control": "max-age=0",
    "Origin": "null",
    "Priority": "u=0, i",
    "Sec-CH-UA": CAPTURED_BROWSER_HEADERS["Sec-CH-UA"],
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": USER_AGENT,
}

GET_HEADERS = {
    "Accept": CAPTURED_BROWSER_HEADERS["Accept"],
    "Accept-Language": CAPTURED_BROWSER_HEADERS["Accept-Language"],
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": USER_AGENT,
}


def decode_html(html: str | bytes) -> str:
    """将站点返回的 UTF-8 HTML 统一转换为字符串。"""
    if isinstance(html, bytes):
        return html.decode("utf-8", errors="replace")
    return html


def load_config(config_path: Path = CONFIG_PATH) -> tuple[str, str, str]:
    """从 JSON 配置文件读取 Cookie、文章标题和 Markdown 正文。"""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"配置文件不存在：{config_path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"配置文件不是有效 JSON：{error}") from error

    cookie = config.get("cookie")
    title = config.get("title")
    content = config.get("content")
    if not isinstance(cookie, str):
        raise TypeError("配置项 cookie 必须是字符串")
    if not isinstance(title, str):
        raise TypeError("配置项 title 必须是字符串")
    if not isinstance(content, str):
        raise TypeError("配置项 content 必须是字符串")
    return cookie, title, content


def markdown_to_html(content: str) -> str:
    """将 Markdown 正文转换为门户编辑器可接收的 HTML。"""
    return markdown.markdown(
        content,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )


def parse_formhash(html: str | bytes) -> str:
    """从文章表单中提取当前 CSRF 令牌 formhash。"""
    document = lxml_html.fromstring(decode_html(html))
    values = document.xpath(
        "//form[@id='articleform']//input[@name='formhash']/@value"
    )
    if not values or not values[0]:
        raise RuntimeError(
            "未找到 articleform/formhash；Cookie 可能已过期，"
            "或者当前账号没有文章发布权限"
        )
    return str(values[0])


def parse_publish_result(html: str | bytes) -> tuple[bool, str | None, str]:
    """返回发布状态、文章编辑链接和规范化后的响应文本。"""
    document = lxml_html.fromstring(decode_html(html))
    text = " ".join(document.text_content().split())
    edit_links = document.xpath(
        "//a[contains(@href, 'op=edit') and contains(@href, 'aid=')]/@href"
    )
    edit_href = str(edit_links[0]) if edit_links else None
    return "发布文章成功" in text, edit_href, text


def build_multipart_parts(
    title: str, content: str, formhash: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """按照浏览器抓包顺序构造 multipart 请求字段。"""

    def text(name: str, value: Any) -> tuple[str, tuple[None, str]]:
        return name, (None, "" if value is None else str(value))

    parts: list[tuple[str, tuple[Any, ...]]] = [
        text("title", title),
        text("highlight_style[0]", ""),
        text("highlight_style[1]", ""),
        text("highlight_style[2]", ""),
        text("highlight_style[3]", ""),
        text("htmlname", ""),
        text("oldhtmlname", ""),
        text("pagetitle", ""),
        text("catid", "121"),
        text("from", ""),
        text("fromurl", ""),
        text("dateline", ""),
        text("from_idtype", "tid"),
        text("from_id", "0"),
        text("id", "0"),
        text("idtype", "tid"),
        text("url", ""),
        text("author", ""),
        text("conver", ""),
        text("newalbum", "请输入相册名称"),
        ("file", ("", b"", "application/octet-stream")),
        text("view_albumid", "none"),
        ("file", ("", b"", "application/octet-stream")),
        text("content", content),
        text("summary", ""),
        text("aid", ""),
        text("cid", ""),
        text("attach_ids", "0"),
        text("articlesubmit", "true"),
        text("formhash", formhash),
    ]
    return parts


def new_session(cookie_header: str) -> requests.Session:
    session = requests.Session()
    session.headers["Cookie"] = cookie_header
    return session


def get_live_formhash(session: requests.Session, timeout: float) -> str:
    response = _REQUESTER.request(
        "GET",
        EDIT_URL,
        headers=GET_HEADERS,
        timeout=timeout,
        _client=session,
    )
    response.raise_for_status()
    return parse_formhash(response.content)


def send_publish_request(
    session: requests.Session,
    parts: list[tuple[str, tuple[Any, ...]]],
    timeout: float,
) -> requests.Response:
    return _REQUESTER.request(
        "POST",
        PUBLISH_URL,
        headers=PUBLISH_HEADERS,
        files=parts,
        timeout=timeout,
        allow_redirects=True,
        _client=session,
    )


def publish_article(
    title: str, content: str, cookie_header: str
) -> dict[str, Any]:
    """将 Markdown 正文转换为 HTML 后提交发布文章。"""
    title = title.strip()
    if not 1 <= len(title) <= 80:
        raise ValueError("标题长度必须为 1 到 80 个字符")
    if not isinstance(content, str):
        raise TypeError("content 必须是字符串")

    cookie_header = cookie_header.strip()
    if not cookie_header:
        raise RuntimeError("请在 hepan_config.json 中填写浏览器的完整 Cookie 请求头")

    timeout = 30.0
    session = new_session(cookie_header)
    formhash = get_live_formhash(session, timeout)
    html_content = markdown_to_html(content)
    parts = build_multipart_parts(title, html_content, formhash)
    response = send_publish_request(session, parts, timeout)
    response.raise_for_status()

    success, edit_href, result_text = parse_publish_result(response.content)
    article_url = urljoin(response.url, edit_href) if edit_href else None
    result = {
        "http_status": response.status_code,
        "success": success,
        "response_url": response.url,
        "edit_url": article_url,
    }
    if not success:
        raise RuntimeError(f"发布失败：{result_text[:500]}")
    return result


if __name__ == "__main__":
    cookie, title, content = load_config()

    if not title:
        raise SystemExit("请先在 hepan_config.json 中填写 title 和 content")

    publish_result = publish_article(title, content, cookie)
    print(json.dumps(publish_result, ensure_ascii=False, indent=2))
