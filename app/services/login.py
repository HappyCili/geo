from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from lxml import etree
from lxml import html as lxml_html

from app.domain import AccountProxy, LoginResult, LoginSecret
from app.utils.request import BaseRequest
from app.utils.proxy import httpx_client_kwargs, httpx_proxy_url


HEPAN_BASE_URL = "https://www.hepan.com"
HEPAN_LOGIN_PAGE_URL = (
    f"{HEPAN_BASE_URL}/member.php?mod=logging&action=login&referer="
)
HEPAN_LOGIN_AJAX_URL = f"{HEPAN_BASE_URL}/plugin.php"
HEPAN_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"
)
HEPAN_HUMAN_VERIFICATION_MARKER = "宝塔防火墙正在检查您的访问"
HEPAN_REQUEST_TIMEOUT_SECONDS = 15.0
LIEJU_ENTRY_URL = "https://hz.lieju.com/"
LIEJU_PUBLISH_WARMUP_URL = "https://post.lieju.com/"
LIEJU_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"
)
LIEJU_WAF_MARKER = "renderData"
LIEJU_AUTH_COOKIE = "lieju_passport"


async def _send_logged_request(
    requester: BaseRequest,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout_seconds: float,
    **kwargs: Any,
) -> httpx.Response:
    """Send through the shared logger while preserving a caller-owned session."""
    return await requester.request(
        method,
        url,
        timeout=timeout_seconds,
        allow_redirects=True,
        retry=1,
        update_cookie=False,
        _client=client,
        **kwargs,
    )


class LoginProvider(Protocol):
    async def login(
        self,
        secret: LoginSecret,
        proxy: AccountProxy | None = None,
    ) -> LoginResult:
        """通过受控边界执行一次登录。"""


class LiejuWafCookieSolver:
    """Use the platform's page script to derive its WAF cookie without credentials."""

    def __init__(
        self,
        *,
        node_binary: str = "node",
        script: Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        self._node_binary = node_binary
        self._project_root = Path(__file__).resolve().parents[2]
        self._script = script or self._project_root / "scripts" / "lieju_waf_cookie.mjs"
        self._timeout_seconds = timeout_seconds

    def solve(self, page_html: bytes) -> str:
        try:
            completed = subprocess.run(
                [self._node_binary, str(self._script)],
                input=page_html,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                cwd=str(self._project_root),
                env=self._child_environment(),
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("Lieju WAF 脚本运行失败") from error
        if completed.returncode != 0:
            raise ValueError("Lieju WAF 脚本未成功退出")
        try:
            payload: Any = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Lieju WAF 脚本输出无效") from error
        if not isinstance(payload, dict):
            raise ValueError("Lieju WAF 脚本未返回对象")
        raw_cookie = payload.get("cookie")
        if not isinstance(raw_cookie, str):
            raise ValueError("Lieju WAF 脚本未返回 Cookie")
        name, separator, value = raw_cookie.partition("=")
        if name != "acw_sc__v2" or not separator:
            raise ValueError("Lieju WAF Cookie 名称无效")
        cookie_value = value.split(";", 1)[0].strip()
        if not cookie_value:
            raise ValueError("Lieju WAF Cookie 为空")
        return cookie_value

    @staticmethod
    def _child_environment() -> dict[str, str]:
        return {
            name: value
            for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if (value := os.environ.get(name))
        }


class LiejuLoginProvider:
    """Refresh a Lieju session through the current phone/password login form."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        waf_cookie_solver: Callable[[bytes], str] | None = None,
        entry_url: str = LIEJU_ENTRY_URL,
        publish_warmup_url: str = LIEJU_PUBLISH_WARMUP_URL,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or httpx.AsyncClient
        self._requester = BaseRequest()
        self._waf_cookie_solver = waf_cookie_solver or LiejuWafCookieSolver().solve
        self._entry_url = entry_url
        self._publish_warmup_url = publish_warmup_url

    async def login(
        self,
        secret: LoginSecret,
        proxy: AccountProxy | None = None,
    ) -> LoginResult:
        try:
            async with self._client_factory(
                headers={"User-Agent": LIEJU_USER_AGENT},
                timeout=self._timeout_seconds,
                follow_redirects=True,
                **httpx_client_kwargs(proxy),
            ) as client:
                entry_page = await self._load_page(client, self._entry_url)
                if entry_page is None:
                    return LoginResult("protocol_error")
                if entry_page.status_code >= 500:
                    return LoginResult("network_error")
                if entry_page.is_error:
                    return LoginResult("protocol_error")
                login_url = self._discover_login_url(entry_page)
                if login_url is None:
                    return LoginResult("protocol_error")

                login_page = await self._load_page(client, login_url)
                if login_page is None:
                    return LoginResult("protocol_error")
                if login_page.status_code >= 500:
                    return LoginResult("network_error")
                if login_page.is_error:
                    return LoginResult("protocol_error")
                if self._is_verification_page(login_page):
                    return LoginResult("captcha_required")
                form = self._parse_login_form(login_page)
                if form is None:
                    return LoginResult("protocol_error")
                action, payload = form
                payload.extend((("username", secret.username), ("password", secret.password)))
                response = await _send_logged_request(
                    self._requester,
                    client,
                    "POST",
                    action,
                    timeout_seconds=self._timeout_seconds,
                    content=urlencode(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": self._origin(login_page),
                        "Referer": str(login_page.url),
                    },
                )
                if response.status_code >= 500:
                    return LoginResult("network_error")
                if response.is_error:
                    return LoginResult(self._failure_kind(response))
                if self._is_verification_page(response):
                    return LoginResult("captcha_required")
                if self._is_login_form(response):
                    return LoginResult("login_expired")

                warmup = await self._load_page(client, self._publish_warmup_url)
                if warmup is None:
                    return LoginResult("protocol_error")
                if warmup.status_code >= 500:
                    return LoginResult("network_error")
                if warmup.is_error:
                    return LoginResult("protocol_error")
                cookies = self._cookies_for_publish_host(client, self._publish_warmup_url)
        except httpx.RequestError:
            return LoginResult("network_error")

        if LIEJU_AUTH_COOKIE not in cookies:
            return LoginResult("login_expired")
        cookie_header = "; ".join(
            f"{name}={value}" for name, value in sorted(cookies.items())
        )
        return LoginResult("success", cookies=cookies, cookie_header=cookie_header)

    async def _load_page(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> httpx.Response | None:
        page = await _send_logged_request(
            self._requester,
            client,
            "GET",
            url,
            timeout_seconds=self._timeout_seconds,
        )
        if not self._is_waf_page(page):
            return page
        host = urlparse(str(page.url)).hostname
        if not self._is_lieju_host(host):
            return None
        try:
            cookie_value = await asyncio.to_thread(
                self._waf_cookie_solver, page.content
            )
        except (OSError, ValueError):
            return None
        client.cookies.set("acw_sc__v2", cookie_value, domain=host, path="/")
        return await _send_logged_request(
            self._requester,
            client,
            "GET",
            url,
            timeout_seconds=self._timeout_seconds,
        )

    @staticmethod
    def _is_waf_page(response: httpx.Response) -> bool:
        return LIEJU_WAF_MARKER in response.text

    @classmethod
    def _discover_login_url(cls, page: httpx.Response) -> str | None:
        document = cls._document(page.content)
        if document is None:
            return None
        for link in document.xpath("//a[@href]"):
            label = " ".join(link.text_content().split())
            candidate = urljoin(str(page.url), str(link.get("href")))
            parsed = urlparse(candidate)
            if (
                "登录" in label
                and cls._is_lieju_host(parsed.hostname)
                and parsed.path.rstrip("/") == "/login"
            ):
                return candidate
        return None

    @classmethod
    def _parse_login_form(
        cls, page: httpx.Response
    ) -> tuple[str, list[tuple[str, str]]] | None:
        document = cls._document(page.content)
        if document is None:
            return None
        forms = document.xpath(
            "//form[.//input[@name='username'] and .//input[@name='password']]"
        )
        if not forms:
            return None
        form = forms[0]
        action = urljoin(str(page.url), str(form.get("action") or ""))
        if not cls._is_lieju_host(urlparse(action).hostname):
            return None
        payload: list[tuple[str, str]] = []
        for field in form.xpath(".//input[@name]"):
            name = str(field.get("name"))
            field_type = str(field.get("type") or "text").lower()
            if name in {"username", "password"}:
                continue
            if field_type in {"hidden", "submit"} or (
                field_type in {"checkbox", "radio"}
                and field.get("checked") is not None
            ):
                payload.append((name, str(field.get("value") or "")))
        return action, payload

    @classmethod
    def _is_verification_page(cls, page: httpx.Response) -> bool:
        document = cls._document(page.content)
        if document is None:
            return False
        return bool(document.xpath("//form[@id='verifyForm']"))

    @classmethod
    def _is_login_form(cls, page: httpx.Response) -> bool:
        return cls._parse_login_form(page) is not None

    @classmethod
    def _failure_kind(cls, response: httpx.Response) -> str:
        return "captcha_required" if cls._is_verification_page(response) else "login_expired"

    @staticmethod
    def _document(raw_html: bytes) -> Any | None:
        try:
            return lxml_html.fromstring(raw_html)
        except (etree.ParserError, ValueError):
            return None

    @staticmethod
    def _is_lieju_host(host: str | None) -> bool:
        return host == "lieju.com" or bool(host and host.endswith(".lieju.com"))

    @staticmethod
    def _origin(page: httpx.Response) -> str:
        parsed = urlparse(str(page.url))
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _cookies_for_publish_host(
        client: httpx.AsyncClient,
        publish_url: str,
    ) -> dict[str, str]:
        host = urlparse(publish_url).hostname
        if not host:
            return {}
        selected: dict[str, tuple[int, str]] = {}
        for cookie in client.cookies.jar:
            if cookie.is_expired():
                continue
            domain = cookie.domain.lstrip(".")
            if domain == host:
                specificity = 2
            elif host.endswith(f".{domain}"):
                specificity = 1
            else:
                continue
            previous = selected.get(cookie.name)
            if previous is None or specificity >= previous[0]:
                selected[cookie.name] = (specificity, cookie.value)
        return {name: value for name, (_, value) in selected.items()}


class HepanLoginProvider:
    """通过 Hepan 公开登录表单，以手机号和密码刷新登录态。"""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._request_timeout_seconds = min(
            timeout_seconds, HEPAN_REQUEST_TIMEOUT_SECONDS
        )
        self._client_factory = client_factory or httpx.AsyncClient
        self._requester = BaseRequest()

    async def login(
        self,
        secret: LoginSecret,
        proxy: AccountProxy | None = None,
    ) -> LoginResult:
        try:
            return await asyncio.wait_for(
                self._login_with_session(secret, proxy),
                timeout=self._timeout_seconds,
            )
        except (asyncio.TimeoutError, httpx.RequestError):
            return LoginResult("network_error")

    async def _login_with_session(
        self,
        secret: LoginSecret,
        proxy: AccountProxy | None = None,
    ) -> LoginResult:
        async with self._client_factory(
            headers={"User-Agent": HEPAN_USER_AGENT},
            timeout=self._request_timeout_seconds,
            follow_redirects=True,
            **httpx_client_kwargs(proxy),
        ) as client:
            page, challenge_required = await self._load_login_page(client)
            if challenge_required:
                return LoginResult("captcha_required")
            if page.status_code >= 500:
                return LoginResult("network_error")
            if page.is_error:
                return LoginResult("protocol_error")
            context = self._parse_login_context(page.content)
            if context is None:
                return LoginResult("protocol_error")
            formhash, version = context
            response = await _send_logged_request(
                self._requester,
                client,
                "POST",
                HEPAN_LOGIN_AJAX_URL,
                timeout_seconds=self._request_timeout_seconds,
                params={
                    "id": "it618_members:ajax",
                    "ac": "login",
                    "formhash": formhash,
                },
                data={
                    "version": version,
                    "logintype": "1",
                    "userlogintype": "2",
                    "username": secret.username,
                    "password": secret.password,
                    "usertelcode": "",
                    "userquestionid": "0",
                    "useranswer": "",
                },
                headers={
                    "Accept": "text/html, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": HEPAN_BASE_URL,
                    "Referer": str(page.url),
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if response.status_code >= 500:
                return LoginResult("network_error")
            if self._is_human_verification(response):
                return LoginResult("captcha_required")
            if response.is_error:
                return LoginResult(
                    self._failure_kind(response.text, status_code=response.status_code)
                )
            if not self._is_success(response.text):
                return LoginResult(self._failure_kind(response.text))

            cookies = dict(client.cookies.items())
            if not cookies:
                return LoginResult("protocol_error")
            cookie_header = "; ".join(
                f"{name}={value}" for name, value in cookies.items()
            )
            session_id = next(
                (value for name, value in cookies.items() if name.lower().endswith("sid")),
                None,
            )
            return LoginResult(
                "success",
                cookies=cookies,
                cookie_header=cookie_header,
                session_id=session_id,
            )

    async def _load_login_page(
        self,
        client: httpx.AsyncClient,
    ) -> tuple[httpx.Response, bool]:
        page = await _send_logged_request(
            self._requester,
            client,
            "GET",
            HEPAN_LOGIN_PAGE_URL,
            timeout_seconds=self._request_timeout_seconds,
        )
        if self._is_client_reload_page(page):
            page = await _send_logged_request(
                self._requester,
                client,
                "GET",
                HEPAN_LOGIN_PAGE_URL,
                timeout_seconds=self._request_timeout_seconds,
            )
            if self._is_client_reload_page(page):
                return page, True
        if not self._is_human_verification(page):
            return page, False
        if not await self._complete_human_verification(client, page):
            return page, True
        page = await _send_logged_request(
            self._requester,
            client,
            "GET",
            HEPAN_LOGIN_PAGE_URL,
            timeout_seconds=self._request_timeout_seconds,
        )
        return page, self._is_human_verification(page)

    @staticmethod
    def _is_client_reload_page(response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        match = re.search(
            r"""window\.location(?:\.href)?\s*=\s*["'](?P<target>[^"']+)["']""",
            response.text,
            re.IGNORECASE,
        )
        return bool(
            match
            and urljoin(str(response.url), match.group("target")) == HEPAN_LOGIN_PAGE_URL
        )

    @classmethod
    def _is_human_verification(cls, response: httpx.Response) -> bool:
        return cls._has_human_verification_marker(response.text)

    @staticmethod
    def _has_human_verification_marker(response_text: str) -> bool:
        return HEPAN_HUMAN_VERIFICATION_MARKER in response_text or bool(
            re.search(r"<script[^>]+src=[\"'][^\"']*renji_", response_text, re.IGNORECASE)
        )

    async def _complete_human_verification(
        self,
        client: httpx.AsyncClient,
        challenge_page: httpx.Response,
    ) -> bool:
        script_match = re.search(
            r'<script[^>]+src=["\'](?P<src>[^"\']*renji_[^"\']+)["\']',
            challenge_page.text,
        )
        if script_match is None:
            return False
        script_response = await _send_logged_request(
            self._requester,
            client,
            "GET",
            urljoin(str(challenge_page.url), script_match.group("src")),
            timeout_seconds=self._request_timeout_seconds,
            headers={"Referer": str(challenge_page.url)},
        )
        if script_response.is_error:
            return False
        parameters = self._parse_human_verification_parameters(script_response.text)
        if parameters is None:
            return False
        verify_path, challenge_type, key, value = parameters
        proof = hashlib.md5(
            "".join(str(ord(character)) for character in value).encode("utf-8")
        ).hexdigest()
        verification = await _send_logged_request(
            self._requester,
            client,
            "GET",
            urljoin(str(challenge_page.url), verify_path),
            timeout_seconds=self._request_timeout_seconds,
            params={"type": challenge_type, "key": key, "value": proof},
            headers={
                "Referer": str(challenge_page.url),
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        return not verification.is_error

    @staticmethod
    def _parse_human_verification_parameters(
        script: str,
    ) -> tuple[str, str, str, str] | None:
        key_match = re.search(
            r"(?:var|let|const)\s+key\s*=\s*(?P<quote>[\"'])(?P<key>[^\"']+)"
            r"(?P=quote)\s*[;,]",
            script,
        )
        value_match = re.search(
            r"(?:var|let|const)\s+value\s*=\s*(?P<quote>[\"'])(?P<value>[^\"']+)"
            r"(?P=quote)",
            script,
        )
        if key_match is None:
            key_match = re.search(
                r"\bkey\s*=\s*(?P<quote>[\"'])(?P<key>[^\"']+)(?P=quote)",
                script,
            )
        if value_match is None:
            value_match = re.search(
                r"\bvalue\s*=\s*(?P<quote>[\"'])(?P<value>[^\"']+)(?P=quote)",
                script,
            )
        verification_match = re.search(
            r"[\"'](?P<path>/[^\"'?]+)\?type=(?P<type>[^\"'&]+)&key=[\"']"
            r"\s*\+\s*key\s*\+\s*[\"']&value=[\"']\s*\+\s*md5encode",
            script,
            re.IGNORECASE,
        )
        if key_match is None or value_match is None or verification_match is None:
            return None
        return (
            verification_match.group("path"),
            verification_match.group("type"),
            key_match.group("key"),
            value_match.group("value"),
        )

    @staticmethod
    def _parse_login_context(raw_html: bytes) -> tuple[str, str] | None:
        try:
            document = lxml_html.fromstring(raw_html.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, etree.ParserError, ValueError):
            return None
        formhashes = document.xpath("//input[@name='formhash']/@value")
        versions = document.xpath("//form[@id='it618_login']//input[@name='version']/@value")
        if not versions:
            versions = document.xpath("//input[@name='version']/@value")
        if not formhashes or not versions:
            return None
        formhash = str(formhashes[0]).strip()
        version = str(versions[0]).strip()
        return (formhash, version) if formhash and version else None

    @staticmethod
    def _is_success(response_text: str) -> bool:
        parts = response_text.split("it618_split")
        return (
            len(parts) > 1 and parts[1].strip() == "ok"
        ) or 'class="help"' in response_text

    @staticmethod
    def _failure_kind(response_text: str, *, status_code: int | None = None) -> str:
        normalized = response_text.lower()
        if (
            "captcha" in normalized
            or "验证码" in response_text
            or HepanLoginProvider._has_human_verification_marker(response_text)
            or status_code in {401, 403, 429}
        ):
            return "captcha_required"
        return "login_expired"


class CnblogsLoginProvider:
    def __init__(self, *, timeout_seconds: float, terminate_grace_seconds: float = 5.0) -> None:
        if terminate_grace_seconds <= 0:
            raise ValueError("terminate_grace_seconds 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._terminate_grace_seconds = terminate_grace_seconds
        self._project_root = Path(__file__).resolve().parents[2]
        self._script = self._project_root / "scripts" / "cnblogs_signin.py"

    async def login(
        self,
        secret: LoginSecret,
        proxy: AccountProxy | None = None,
    ) -> LoginResult:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._script),
                "--submit",
                "--credentials-stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._child_environment(proxy),
                cwd=str(self._project_root),
                start_new_session=True,
            )
        except OSError:
            return LoginResult("refresh_unavailable")
        assert process.stdin is not None
        payload = json.dumps({"username": secret.username, "password": secret.password}) + "\n"
        try:
            process.stdin.write(payload.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            stdout, _ = await asyncio.wait_for(process.communicate(), self._timeout_seconds)
        except asyncio.CancelledError:
            await self._terminate_and_reap(process)
            raise
        except asyncio.TimeoutError:
            await self._terminate_and_reap(process)
            return LoginResult("refresh_unavailable")
        except (BrokenPipeError, ConnectionResetError, OSError):
            await self._terminate_and_reap(process)
            return LoginResult("protocol_error")
        try:
            value: Any = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return LoginResult("protocol_error")
        if not isinstance(value, dict):
            return LoginResult("protocol_error")
        if value.get("success") is True:
            cookies = value.get("cookies")
            cookie_header = value.get("cookie_header")
            if self._valid_cookie_map(cookies) and isinstance(cookie_header, str) and cookie_header:
                session_id = value.get("session_id")
                return LoginResult(
                    "success",
                    cookies=cookies,
                    cookie_header=cookie_header,
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            return LoginResult("protocol_error")
        category = value.get("category")
        return LoginResult(category if isinstance(category, str) else "protocol_error")

    @staticmethod
    def _valid_cookie_map(value: object) -> bool:
        return isinstance(value, dict) and all(
            isinstance(name, str) and isinstance(cookie_value, str)
            for name, cookie_value in value.items()
        )

    def _child_environment(self, proxy: AccountProxy | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("CNBLOGS_PASSWORD", None)
        environment.pop("CNBLOGS_USERNAME", None)
        environment.pop("CNBLOGS_PROXY", None)
        existing_python_path = environment.get("PYTHONPATH")
        python_path_entries = [str(self._project_root)]
        if existing_python_path:
            python_path_entries.append(existing_python_path)
        environment["PYTHONPATH"] = os.pathsep.join(python_path_entries)
        if proxy is not None:
            environment["CNBLOGS_PROXY"] = httpx_proxy_url(proxy)
        return environment

    async def _terminate_and_reap(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), self._terminate_grace_seconds)
            return
        except asyncio.TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), self._terminate_grace_seconds)
        except asyncio.TimeoutError:
            return

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, signal_number: signal.Signals) -> None:
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return
