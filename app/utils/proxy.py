from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from app.domain import AccountProxy
from app.errors import ProxyUnavailableError


_SUPPORTED_PROTOCOLS = {"http", "https"}


def build_account_proxy(row: Mapping[str, Any]) -> AccountProxy | None:
    """Build a proxy at the database trust boundary.

    ``proxy_id`` is read from the account row while the remaining values come
    from the active/valid joined proxy row.  A missing joined row therefore
    means that an explicitly assigned proxy is unusable, not that the account
    should fall back to a direct connection.
    """
    assigned_id = row.get("proxy_id")
    if assigned_id is None:
        return None

    resolved_id = row.get("resolved_proxy_id")
    if resolved_id is None:
        raise ProxyUnavailableError("媒体账号绑定的代理不存在、已失效或已过期")

    try:
        proxy_id = int(resolved_id)
    except (TypeError, ValueError) as error:
        raise ProxyUnavailableError("媒体账号绑定的代理 ID 无效") from error

    protocol = _required_text(row.get("proxy_protocol"), "代理协议").lower()
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ProxyUnavailableError(
            f"媒体账号绑定的代理协议不受支持：{protocol or '<empty>'}"
        )
    host = _required_text(row.get("proxy_ip"), "代理地址")
    port = _proxy_port(row.get("proxy_port"))
    username = _optional_text(row.get("proxy_username"))
    password = _optional_text(row.get("proxy_password"))
    if bool(username) != bool(password):
        raise ProxyUnavailableError("媒体账号绑定的代理认证信息不完整")

    fingerprint_source = "\0".join(
        (protocol, host, str(port), username, password)
    ).encode("utf-8")
    fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
    return AccountProxy(
        id=proxy_id,
        protocol=protocol,
        host=host,
        port=port,
        username=username,
        password=password,
        fingerprint=fingerprint,
    )


def httpx_client_kwargs(proxy: AccountProxy | None) -> dict[str, str]:
    if proxy is None:
        return {}
    return {"proxy": httpx_proxy_url(proxy)}


def httpx_proxy_url(proxy: AccountProxy) -> str:
    authority = _authority_host(proxy.host)
    if proxy.username:
        authority = (
            f"{quote(proxy.username, safe='')}:{quote(proxy.password, safe='')}@"
            f"{authority}"
        )
    return f"{proxy.protocol}://{authority}:{proxy.port}"


def playwright_client_kwargs(proxy: AccountProxy | None) -> dict[str, dict[str, str]]:
    if proxy is None:
        return {}
    options = {
        "server": f"{proxy.protocol}://{_authority_host(proxy.host)}:{proxy.port}"
    }
    if proxy.username:
        options["username"] = proxy.username
        options["password"] = proxy.password
    return {"proxy": options}


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProxyUnavailableError(f"媒体账号绑定的{label}为空")
    value = value.strip()
    if label == "代理地址":
        _validate_host(value)
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
    return value


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProxyUnavailableError("媒体账号绑定的代理认证信息无效")
    return value


def _proxy_port(value: Any) -> int:
    if isinstance(value, bool):
        raise ProxyUnavailableError("媒体账号绑定的代理端口无效")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ProxyUnavailableError("媒体账号绑定的代理端口无效") from error
    if not 1 <= port <= 65535:
        raise ProxyUnavailableError("媒体账号绑定的代理端口超出范围")
    return port


def _authority_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _validate_host(host: str) -> None:
    if any(char.isspace() or char in "/?#@" for char in host):
        raise ProxyUnavailableError("媒体账号绑定的代理地址无效")
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if ":" in candidate:
        try:
            ipaddress.ip_address(candidate)
        except ValueError as error:
            raise ProxyUnavailableError("媒体账号绑定的 IPv6 代理地址无效") from error
        return
    if not re.fullmatch(r"[A-Za-z0-9.-]+", candidate):
        raise ProxyUnavailableError("媒体账号绑定的代理地址无效")
