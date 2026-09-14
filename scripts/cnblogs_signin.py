#!/usr/bin/env python3
"""Reproduce the current CNBlogs password sign-in request without a browser.

The script performs the Angular client's session warm-up, runs the official
AliyunCaptchaV2 JavaScript in a local Node sandbox, and builds the binary login
envelope. It is dry-run by default; ``--submit`` performs the real request.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

# Allow the documented ``python scripts/cnblogs_signin.py`` invocation from any
# working directory while keeping the subprocess project root explicit.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import msgpack
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.api.platform.cnblogs.browser_profile import (
    BROWSER_MAJOR_VERSION,
    CLIENT_HINT_HEADERS,
    USER_AGENT,
)
from app.utils.request import SyncRequestAdapter


BASE_URL = "https://account.cnblogs.com"
DEFAULT_RETURN_URL = "https://vip.cnblogs.com/my"
PUBLISH_HOST = "i.cnblogs.com"
PUBLISH_WARMUP_URL = f"https://{PUBLISH_HOST}/articles/edit"
CONTENT_TYPE = "application/vnd.cnblogs.em"
INITIAL_MSGPACK_BUFFER_SIZE = 2048
ALIYUN_RUNTIME_PATH = PROJECT_ROOT / "scripts" / "analyze_aliyun_captcha.mjs"
ALIYUN_CAPTCHA_JS_PATH = (
    PROJECT_ROOT / "artifacts" / "cnblogs-signin" / "aliyun" / "AliyunCaptcha.js"
)
CNBLOGS_CAPTCHA_JS_PATH = (
    PROJECT_ROOT / "artifacts" / "cnblogs-signin" / "711-es2015.js"
)
ALIYUN_HTTP_REQUEST_PREFIX = "ALIYUN_HTTP_REQUEST "

CAPTCHA_PROVIDER_NAMES = {
    0: "none",
    1: "geetest",
    2: "recaptcha",
    4: "aliyunCaptcha",
    5: "aliyunCaptchaV2",
}
DEFAULT_CAPTCHA_ATTEMPTS = 2
MAX_CAPTCHA_ATTEMPTS = 3
_REQUESTER = SyncRequestAdapter()


class CaptchaChallengeError(ValueError):
    """The captcha runtime did not obtain a usable token for this session."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _js_round(value: float) -> int:
    return math.floor(value + 0.5)


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError(f"{endpoint} did not return JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{endpoint} did not return a JSON object")
    return payload


def _return_url_query(return_url: str) -> str:
    parsed = urlparse(return_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("return URL must be an absolute HTTP(S) URL")
    return "returnUrl=" + quote(return_url, safe=":")


@dataclass(frozen=True)
class EncryptionContext:
    key_id: int
    public_key_der: bytes
    server_timestamp_ms: int
    time_offset_ms: int
    xsrf_token: str


@dataclass(frozen=True)
class PreparedSignin:
    page_url: str
    sign_in_url: str
    encryption: EncryptionContext
    sign_in_options: Mapping[str, Any]
    captcha_options: Mapping[str, Any]
    call_sequence: tuple[str, ...]


@dataclass(frozen=True)
class AliyunCaptchaConfig:
    prefix: str
    scene_id: str
    region: str = "cn"


def parse_encryption_context(
    options: Mapping[str, Any],
    *,
    request_started_ms: int,
    response_received_ms: int,
) -> EncryptionContext:
    encoded = options.get("encryption")
    xsrf_token = options.get("xsrfToken")
    if not isinstance(encoded, str) or not isinstance(xsrf_token, str):
        raise ValueError("/api/options is missing encryption or xsrfToken")

    try:
        unpacked = msgpack.unpackb(base64.b64decode(encoded), raw=False)
    except (ValueError, TypeError, msgpack.ExtraData, msgpack.FormatError) as error:
        raise ValueError("/api/options returned invalid encryption metadata") from error

    if not isinstance(unpacked, list) or len(unpacked) != 3:
        raise ValueError("encryption metadata must be [keyId, publicKey, timestamp]")
    key_id, public_key_der, server_timestamp_ms = unpacked
    if not isinstance(key_id, int) or not isinstance(public_key_der, bytes):
        raise ValueError("encryption metadata has invalid key fields")
    if not isinstance(server_timestamp_ms, int):
        raise ValueError("encryption metadata has an invalid timestamp")

    midpoint_ms = (request_started_ms + response_received_ms) / 2
    return EncryptionContext(
        key_id=key_id,
        public_key_der=public_key_der,
        server_timestamp_ms=server_timestamp_ms,
        time_offset_ms=_js_round(server_timestamp_ms - midpoint_ms),
        xsrf_token=xsrf_token,
    )


def prepare_signin(
    client: httpx.Client,
    *,
    return_url: str = DEFAULT_RETURN_URL,
    clock_ms: Callable[[], int] = _now_ms,
) -> PreparedSignin:
    query = _return_url_query(return_url)
    page_url = f"{BASE_URL}/signin?{query}"
    sign_in_url = f"{BASE_URL}/api/sign-in?{query}"
    options_url = f"{BASE_URL}/api/options"

    page_response = _REQUESTER.request("GET", page_url, _client=client)
    page_response.raise_for_status()

    options_started_ms = clock_ms()
    options_response = _REQUESTER.request("GET", options_url, _client=client)
    options_received_ms = clock_ms()
    options_response.raise_for_status()
    options_payload = _json_object(options_response, "/api/options")
    encryption = parse_encryption_context(
        options_payload,
        request_started_ms=options_started_ms,
        response_received_ms=options_received_ms,
    )

    sign_in_options_response = _REQUESTER.request("GET", sign_in_url, _client=client)
    sign_in_options_response.raise_for_status()
    sign_in_options = _json_object(sign_in_options_response, "GET /api/sign-in")

    captcha_url = (
        f"{BASE_URL}/api/captcha?timestamp={clock_ms()}&action=SignIn"
    )
    captcha_response = _REQUESTER.request("GET", captcha_url, _client=client)
    captcha_response.raise_for_status()
    captcha_options = _json_object(captcha_response, "/api/captcha")

    return PreparedSignin(
        page_url=page_url,
        sign_in_url=sign_in_url,
        encryption=encryption,
        sign_in_options=sign_in_options,
        captcha_options=captcha_options,
        call_sequence=(page_url, options_url, sign_in_url, captcha_url),
    )


def build_login_payload(
    *,
    username: str,
    password: str,
    captcha: Mapping[str, Any],
    remember: bool,
) -> dict[str, Any]:
    username = username.strip()
    password = password.strip()
    if not username or not password:
        raise ValueError("username and password are required after trimming")
    return {
        "captcha": dict(captcha),
        "username": username,
        "password": password,
        "isRemember": remember,
    }


def _browser_array_buffer(encoded: bytes) -> bytes:
    if len(encoded) <= INITIAL_MSGPACK_BUFFER_SIZE:
        capacity = INITIAL_MSGPACK_BUFFER_SIZE
    else:
        capacity = len(encoded) * 2
    return encoded.ljust(capacity, b"\x00")


def encrypt_login_payload(
    payload: Mapping[str, Any],
    encryption: EncryptionContext,
    *,
    clock_ms: Callable[[], int] = _now_ms,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> bytes:
    plaintext = json.dumps(
        dict(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    aes_key = random_bytes(16)
    iv = random_bytes(12)
    if len(aes_key) != 16 or len(iv) != 12:
        raise ValueError("random byte source returned an unexpected length")

    timestamp_ms = clock_ms() + encryption.time_offset_ms
    if timestamp_ms < 0 or timestamp_ms >= 1 << 64:
        raise ValueError("adjusted timestamp is outside uint64 range")
    additional_data = timestamp_ms.to_bytes(8, "big")

    encryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).encryptor()
    encryptor.authenticate_additional_data(additional_data)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    tag = encryptor.tag[:12]

    public_key = serialization.load_der_public_key(encryption.public_key_der)
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise ValueError("/api/options public key is not RSA")

    key_material = msgpack.packb(
        [aes_key, iv, tag, additional_data], use_bin_type=True
    )
    encrypted_key_material = public_key.encrypt(
        key_material,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    envelope = msgpack.packb(
        [1, encryption.key_id, encrypted_key_material, ciphertext],
        use_bin_type=True,
    )
    return _browser_array_buffer(envelope)


def submit_signin(
    client: httpx.Client,
    prepared: PreparedSignin,
    body: bytes,
) -> httpx.Response:
    return _REQUESTER.request(
        "POST",
        prepared.sign_in_url,
        content=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": CONTENT_TYPE,
            "Origin": BASE_URL,
            "Referer": prepared.page_url,
            "X-XSRF-TOKEN": prepared.encryption.xsrf_token,
        },
        _client=client,
    )


def captcha_provider_names(options: Mapping[str, Any]) -> list[str]:
    providers = options.get("providers", [])
    if not isinstance(providers, list):
        return []
    return [CAPTCHA_PROVIDER_NAMES.get(value, f"unknown:{value}") for value in providers]


def placeholder_captcha(options: Mapping[str, Any]) -> dict[str, str]:
    providers = options.get("providers", [])
    if isinstance(providers, list):
        for provider in providers:
            if provider in {4, 5}:
                return {
                    "captchaVerifyParam": "CAPTCHA_TOKEN",
                    "fp": "FINGERPRINT",
                }
            if provider == 2:
                return {"g-recaptcha-response": "CAPTCHA_TOKEN"}
    return {}


def parse_aliyun_captcha_config(
    options: Mapping[str, Any],
) -> AliyunCaptchaConfig:
    providers = options.get("providers")
    if not isinstance(providers, list) or 5 not in providers:
        raise ValueError("CNBlogs did not advertise AliyunCaptchaV2")
    value = options.get("aliyunCaptchaV2")
    if not isinstance(value, Mapping):
        raise ValueError("/api/captcha is missing aliyunCaptchaV2 configuration")

    prefix = value.get("prefix")
    scene_id = value.get("sceneId")
    region = value.get("region", "cn")
    for field_name, field_value in (("prefix", prefix), ("sceneId", scene_id)):
        if not isinstance(field_value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", field_value
        ):
            raise ValueError(f"aliyunCaptchaV2.{field_name} has an invalid format")
    if region != "cn":
        raise ValueError("only the Aliyun cn region is supported")
    return AliyunCaptchaConfig(prefix=prefix, scene_id=scene_id, region=region)


def _validate_aliyun_url(value: str) -> str:
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    allowed_host = hostname == "g.alicdn.com" or hostname == "aliyuncs.com"
    allowed_host = allowed_host or hostname.endswith(".aliyuncs.com")
    if (
        parsed.scheme != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"Aliyun runtime requested a disallowed URL: {value}")
    return value


def _aliyun_bridge_response(
    client: httpx.Client,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    request_id = request["id"]
    method = request["method"]
    url = request["url"]
    headers = request["headers"]
    body = request["body"]
    if method not in {"GET", "POST"}:
        raise ValueError(f"Aliyun runtime requested unsupported method {method!r}")
    if not isinstance(url, str):
        raise ValueError("Aliyun runtime request URL is not a string")
    if not isinstance(headers, Mapping) or not isinstance(body, str):
        raise ValueError("Aliyun runtime request has an invalid shape")

    request_headers = {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in {"connection", "content-length", "host"}
    }
    response = _REQUESTER.request(
        method,
        _validate_aliyun_url(url),
        headers=request_headers,
        content=body.encode("utf-8") if body else None,
        _client=client,
    )
    return {
        "id": request_id,
        "status": response.status_code,
        "headers": dict(response.headers),
        "bodyBase64": base64.b64encode(response.content).decode("ascii"),
    }


def _serve_aliyun_http_bridge(
    client: httpx.Client,
    process: Any,
    diagnostics: list[str],
    bridge_failures: list[str],
) -> None:
    assert process.stderr is not None
    assert process.stdin is not None
    for raw_line in process.stderr:
        line = raw_line.rstrip("\r\n")
        if not line.startswith(ALIYUN_HTTP_REQUEST_PREFIX):
            if line:
                diagnostics.append(line)
            continue
        request: Any = None
        try:
            request = json.loads(line[len(ALIYUN_HTTP_REQUEST_PREFIX) :])
            if not isinstance(request, Mapping):
                raise ValueError("Aliyun HTTP bridge request is not an object")
            reply = _aliyun_bridge_response(client, request)
        except Exception as error:  # The bridge must reply so the JS can fail over.
            bridge_failures.append(str(error))
            request_id = request.get("id") if isinstance(request, Mapping) else None
            reply = {"id": request_id, "error": str(error)}
        try:
            process.stdin.write(json.dumps(reply, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            return


def _aliyun_runtime_environment(
    config: AliyunCaptchaConfig,
    page_url: str,
) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ")
        if name in os.environ
    }
    environment.update(
        {
            "ALIYUN_HTTP_BRIDGE": "1",
            "ALIYUN_ASSERT_FINGERPRINT_SHIMS": "1",
            "CNBLOGS_CAPTCHA_SOURCE": str(CNBLOGS_CAPTCHA_JS_PATH),
            "ALIYUN_OUTPUT_MODE": "captcha",
            "ALIYUN_PAGE_URL": page_url,
            "ALIYUN_PREFIX": config.prefix,
            "ALIYUN_REGION": config.region,
            "ALIYUN_RUN_CHALLENGE": "1",
            "ALIYUN_RUN_INIT": "1",
            "ALIYUN_SCENE_ID": config.scene_id,
            "ALIYUN_SIMULATE_ACTIVITY": "1",
            "ALIYUN_CHALLENGE_TIMEOUT_MS": "8000",
            "ALIYUN_BROWSER_MAJOR_VERSION": BROWSER_MAJOR_VERSION,
            "ALIYUN_USER_AGENT": USER_AGENT,
        }
    )
    return environment


def _validate_aliyun_success_token(token: str, scene_id: str) -> None:
    try:
        decoded = json.loads(base64.b64decode(token, validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Aliyun returned an invalid success token") from error
    if (
        not isinstance(decoded, dict)
        or decoded.get("sceneId") != scene_id
        or decoded.get("isSign") is not True
        or not isinstance(decoded.get("certifyId"), str)
        or not decoded["certifyId"]
        or not isinstance(decoded.get("securityToken"), str)
        or not decoded["securityToken"]
    ):
        raise ValueError("Aliyun success token has an invalid payload")


def _validate_cnblogs_fingerprint(value: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("CNBlogs fingerprint has an invalid encoding") from error
    permutation = decoded[16:]
    if (
        len(decoded) != 32
        or len(set(permutation)) != 16
        or any(index > 15 for index in permutation)
    ):
        raise ValueError("CNBlogs fingerprint has an invalid payload")


def generate_captcha(
    client: httpx.Client,
    prepared: PreparedSignin,
    *,
    timeout_seconds: float = 30.0,
    node_binary: str = "node",
    process_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, str]:
    """Generate AliyunCaptchaV2 through the official JavaScript implementation."""

    if timeout_seconds <= 0:
        raise ValueError("captcha timeout must be positive")
    config = parse_aliyun_captcha_config(prepared.captcha_options)
    if (
        not ALIYUN_RUNTIME_PATH.is_file()
        or not ALIYUN_CAPTCHA_JS_PATH.is_file()
        or not CNBLOGS_CAPTCHA_JS_PATH.is_file()
    ):
        raise ValueError("Aliyun CAPTCHA runtime files are missing")

    process = process_factory(
        [node_binary, str(ALIYUN_RUNTIME_PATH), str(ALIYUN_CAPTCHA_JS_PATH)],
        cwd=PROJECT_ROOT,
        env=_aliyun_runtime_environment(config, prepared.page_url),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    diagnostics: list[str] = []
    bridge_failures: list[str] = []
    bridge_thread = threading.Thread(
        target=_serve_aliyun_http_bridge,
        args=(client, process, diagnostics, bridge_failures),
        daemon=True,
    )
    bridge_thread.start()
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise CaptchaChallengeError("Aliyun CAPTCHA runtime timed out") from error
    finally:
        bridge_thread.join(timeout=2)

    assert process.stdout is not None
    output_text = process.stdout.read()
    if return_code != 0:
        detail = diagnostics[-1] if diagnostics else "no diagnostic output"
        raise CaptchaChallengeError(
            f"Aliyun CAPTCHA runtime exited with {return_code}: {detail}"
        )
    try:
        result = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise CaptchaChallengeError("Aliyun CAPTCHA runtime returned invalid JSON") from error
    if not isinstance(result, dict):
        raise CaptchaChallengeError("Aliyun CAPTCHA runtime returned a non-object result")
    if result.get("success") is not True:
        detail = result.get("error")
        if not isinstance(detail, str) or not detail:
            detail = bridge_failures[-1] if bridge_failures else "verification failed"
        raise CaptchaChallengeError(f"Aliyun CAPTCHA verification failed: {detail}")
    token = result.get("captchaVerifyParam")
    if not isinstance(token, str) or not token:
        raise CaptchaChallengeError("Aliyun CAPTCHA runtime did not return captchaVerifyParam")
    fingerprint = result.get("fp")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise CaptchaChallengeError("Aliyun CAPTCHA runtime did not return CNBlogs fingerprint")
    try:
        _validate_aliyun_success_token(token, config.scene_id)
        _validate_cnblogs_fingerprint(fingerprint)
    except ValueError as error:
        raise CaptchaChallengeError(str(error)) from error
    return {"captchaVerifyParam": token, "fp": fingerprint}


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _signin_response_succeeded(response: httpx.Response, body: Any) -> bool:
    return _signin_response_category(response, body) == "success"


def _signin_response_category(response: httpx.Response, body: Any) -> str:
    if response.status_code in {401, 403}:
        return "login_expired"
    if not response.is_success:
        return "network_error" if response.status_code >= 500 else "login_expired"
    if not isinstance(body, Mapping):
        return "protocol_error"
    if body.get("status") == 0 or body.get("success") is False:
        body_text = json.dumps(body, ensure_ascii=False).lower()
        if "captcha" in body_text or "verification" in body_text:
            return "captcha_required"
        return "login_expired"
    return "success"


def _captcha_attempts(value: str) -> int:
    try:
        attempts = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("captcha attempts must be an integer") from error
    if not 1 <= attempts <= MAX_CAPTCHA_ATTEMPTS:
        raise argparse.ArgumentTypeError(
            f"captcha attempts must be between 1 and {MAX_CAPTCHA_ATTEMPTS}"
        )
    return attempts


def _submit_signin_attempt(
    client: httpx.Client,
    *,
    username: str,
    password: str,
    return_url: str,
    remember: bool,
    captcha_timeout_seconds: float,
    node_binary: str,
    body_output: Path | None,
) -> dict[str, Any]:
    prepared = prepare_signin(client, return_url=return_url)
    captcha = generate_captcha(
        client,
        prepared,
        timeout_seconds=captcha_timeout_seconds,
        node_binary=node_binary,
    )
    payload = build_login_payload(
        username=username,
        password=password,
        captcha=captcha,
        remember=remember,
    )
    body = encrypt_login_payload(payload, prepared.encryption)
    if body_output:
        body_output.parent.mkdir(parents=True, exist_ok=True)
        body_output.write_bytes(body)

    response = submit_signin(client, prepared, body)
    response_body = _response_body(response)
    category = _signin_response_category(response, response_body)
    result: dict[str, Any] = {
        "success": category == "success",
        "submitted": True,
        "http_status": response.status_code,
        "category": category,
        "call_sequence": list(prepared.call_sequence),
        "captcha_providers": captcha_provider_names(prepared.captcha_options),
        "payload_fields": list(payload),
        "captcha_fields": list(captcha),
        "request": {
            "method": "POST",
            "url": prepared.sign_in_url,
            "content_type": CONTENT_TYPE,
            "x_xsrf_token": "<redacted>",
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        },
    }
    if category == "success":
        _warm_up_publish_session(client)
        _verify_authenticated_session(client)
        cookies, cookie_header = _cookie_result(client)
        result["cookies"] = cookies
        result["cookie_header"] = cookie_header
    return result


def submit_signin_with_retries(
    *,
    username: str,
    password: str,
    return_url: str,
    remember: bool,
    captcha_timeout_seconds: float,
    captcha_attempts: int,
    node_binary: str,
    timeout_seconds: float,
    body_output: Path | None,
    proxy: str | None = None,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> dict[str, Any]:
    """Retry a rejected CAPTCHA with a fresh CNBlogs session and token."""

    if not 1 <= captcha_attempts <= MAX_CAPTCHA_ATTEMPTS:
        raise ValueError(
            f"captcha attempts must be between 1 and {MAX_CAPTCHA_ATTEMPTS}"
        )
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": USER_AGENT,
        **CLIENT_HINT_HEADERS,
    }
    result: dict[str, Any] | None = None
    for attempt in range(1, captcha_attempts + 1):
        # A CAPTCHA token is tied to its sign-in session; do not reuse either on retry.
        try:
            with client_factory(
                headers=headers,
                timeout=timeout_seconds,
                follow_redirects=False,
                **({"proxy": proxy} if proxy else {}),
            ) as client:
                result = _submit_signin_attempt(
                    client,
                    username=username,
                    password=password,
                    return_url=return_url,
                    remember=remember,
                    captcha_timeout_seconds=captcha_timeout_seconds,
                    node_binary=node_binary,
                    body_output=body_output,
                )
        except CaptchaChallengeError as error:
            result = {
                "success": False,
                "submitted": False,
                "category": "captcha_required",
                "error": str(error),
            }
        result["captcha_attempts"] = attempt
        if result["category"] != "captcha_required":
            return result
    assert result is not None
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("CNBLOGS_USERNAME"))
    parser.add_argument("--credentials-stdin", action="store_true")
    parser.add_argument(
        "--node-binary", default=os.environ.get("CNBLOGS_NODE_BINARY", "node")
    )
    parser.add_argument("--captcha-timeout", type=float, default=30.0)
    parser.add_argument(
        "--captcha-attempts",
        type=_captcha_attempts,
        default=DEFAULT_CAPTCHA_ATTEMPTS,
        help=f"maximum fresh-session CAPTCHA attempts (1-{MAX_CAPTCHA_ATTEMPTS})",
    )
    parser.add_argument("--return-url", default=DEFAULT_RETURN_URL)
    parser.add_argument(
        "--remember", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--body-output", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def _credentials_from_stdin() -> tuple[str, str]:
    try:
        value = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("credentials stdin is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("credentials stdin must be an object")
    username = value.get("username")
    password = value.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("credentials stdin is missing username or password")
    return username, password


def _cookie_result(client: httpx.Client) -> tuple[dict[str, str], str]:
    cookies = {
        cookie.name: cookie.value
        for cookie in client.cookies.jar
        if _cookie_applies_to_publish_host(cookie)
    }
    if not cookies:
        raise ValueError("sign-in did not return publish cookies")
    return cookies, "; ".join(f"{name}={value}" for name, value in cookies.items())


def _cookie_applies_to_publish_host(cookie: Any) -> bool:
    domain = getattr(cookie, "domain", "").lstrip(".").lower()
    return domain == PUBLISH_HOST or PUBLISH_HOST.endswith("." + domain)


def _warm_up_publish_session(client: httpx.Client) -> None:
    response = _REQUESTER.request("GET", PUBLISH_WARMUP_URL, _client=client)
    if response.status_code in {401, 403} or not response.is_success:
        raise ValueError("CNBlogs publish session warm-up failed")


def _verify_authenticated_session(client: httpx.Client) -> None:
    cookies, _ = _cookie_result(client)
    missing = {".CNBlogsCookie", "XSRF-TOKEN"}.difference(cookies)
    if missing:
        raise ValueError(
            "sign-in cookies are missing " + ", ".join(sorted(missing))
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    username = args.username
    password: str | None = None

    if args.credentials_stdin:
        try:
            username, password = _credentials_from_stdin()
        except ValueError as error:
            print(json.dumps({"success": False, "category": "protocol_error", "error": str(error)}))
            return 2

    if args.submit and not args.credentials_stdin:
        print(json.dumps({"success": False, "category": "protocol_error", "error": "--credentials-stdin is required for --submit"}))
        return 2

    if args.submit:
        if not username or not username.strip():
            print(json.dumps({"success": False, "category": "protocol_error", "error": "username is required"}))
            return 2
        if password is None or not password.strip():
            print(json.dumps({"success": False, "category": "protocol_error", "error": "password is required"}))
            return 2
    try:
        if args.submit:
            result = submit_signin_with_retries(
                username=username or "",
                password=password or "",
                return_url=args.return_url,
                remember=args.remember,
                captcha_timeout_seconds=args.captcha_timeout,
                captcha_attempts=args.captcha_attempts,
                node_binary=args.node_binary,
                timeout_seconds=args.timeout,
                body_output=args.body_output,
                proxy=os.environ.get("CNBLOGS_PROXY"),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["success"] else 1

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "User-Agent": USER_AGENT,
            **CLIENT_HINT_HEADERS,
        }
        with httpx.Client(
            headers=headers,
            timeout=args.timeout,
            follow_redirects=False,
            **(
                {"proxy": os.environ["CNBLOGS_PROXY"]}
                if os.environ.get("CNBLOGS_PROXY")
                else {}
            ),
        ) as client:
            prepared = prepare_signin(client, return_url=args.return_url)
            captcha = placeholder_captcha(prepared.captcha_options)
            payload = build_login_payload(
                username=username or "USERNAME",
                password=password or "PASSWORD",
                captcha=captcha,
                remember=args.remember,
            )
            body = encrypt_login_payload(payload, prepared.encryption)

            if args.body_output:
                args.body_output.parent.mkdir(parents=True, exist_ok=True)
                args.body_output.write_bytes(body)

            result: dict[str, Any] = {
                "success": True,
                "submitted": False,
                "call_sequence": list(prepared.call_sequence),
                "captcha_providers": captcha_provider_names(
                    prepared.captcha_options
                ),
                "payload_fields": list(payload),
                "captcha_fields": list(captcha),
                "request": {
                    "method": "POST",
                    "url": prepared.sign_in_url,
                    "content_type": CONTENT_TYPE,
                    "x_xsrf_token": "<redacted>",
                    "body_length": len(body),
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                },
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except (httpx.HTTPError, OSError) as error:
        print(
            json.dumps(
                {"success": False, "category": "network_error", "error": str(error)}, ensure_ascii=False
            )
        )
        return 1
    except ValueError as error:
        message = str(error).lower()
        category = "captcha_required" if "captcha" in message or "verification" in message else "protocol_error"
        print(
            json.dumps(
                {"success": False, "category": category, "error": str(error)}, ensure_ascii=False
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
