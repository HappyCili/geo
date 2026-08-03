"""Tencent captcha session acquisition for the Lieju publisher.

The primary flow uses HTTP plus an embedded QuickJS runtime.  The optional
browser implementation and explicit static TDC profile remain available for
compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urljoin

import httpx

from app.config import Settings, get_settings
from app.errors import PublisherConfigurationError, UpstreamPublishError
from app.api.platform.lieju.tdc_runtime import encode_tdc_payload
from app.utils.request import BaseRequest


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"
)


class CaptchaForm(Protocol):
    captcha_appid: str | None
    captcha_callback: str | None


@dataclass(frozen=True)
class LiejuCaptchaTicket:
    ticket: str
    randstr: str


class LiejuCaptchaSolver(Protocol):
    async def solve(
        self,
        *,
        category_url: str,
        form: CaptchaForm,
    ) -> LiejuCaptchaTicket: ...


@dataclass(frozen=True)
class LiejuTdcProfile:
    script_sha256: str
    key: bytes

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.script_sha256):
            raise ValueError("script_sha256 must be a lowercase SHA-256 digest")
        if len(self.key) != 16:
            raise ValueError("TDC XTEA key must contain exactly 16 bytes")


@dataclass(frozen=True)
class _Prehandle:
    sess: str
    tdc_path: str
    pow_prefix: str | None
    pow_md5: str | None
    sid: str | None


@dataclass(frozen=True)
class _CaptchaChallenge:
    """One current slider challenge returned by ``cap_union_new_getsig``."""

    sess: str
    background_url: str
    element_id: int
    answer_type: str
    element_width: int
    init_y: int
    min_x: int | None
    max_x: int | None
    init_x: int = 50
    logical_background_width: int = 672


@dataclass(frozen=True)
class _BrowserSliderChallenge:
    """Geometry exposed to the official browser captcha implementation."""

    background_url: str
    logical_background_width: int
    element_width: int
    init_x: int
    init_y: int
    min_x: int | None
    max_x: int | None


@dataclass(frozen=True)
class _ChaojiyingResult:
    pic_id: str | None
    points: tuple[tuple[int, int], ...]


_TDC_NAME_PATTERN = re.compile(
    r"window\.TDC_NAME\s*=\s*([\"'])(?P<name>[A-Za-z_$][\w$]*)\1"
)
_UINT32_MASK = 0xFFFFFFFF
_XTEA_DELTA = 0x9E3779B9
_BROWSER_CAPTCHA_CALLBACK = "liejuCaptchaCallback"
_BROWSER_CAPTCHA_SCRIPT = "https://ssl.captcha.qq.com/TCaptcha.js"
_BROWSER_CAPTCHA_API_BASE = "https://t.captcha.qq.com"
_TDC_IFRAME_REFERER = (
    "https://captcha.gtimg.com/static/template/drag_ele.a48cc4fb.html"
)
_TDC_RENDERED_BACKGROUND_WIDTH = 340
_BROWSER_MOTION_RATIOS = (0.12, 0.28, 0.47, 0.65, 0.81, 0.93, 1.0)
_BROWSER_MOTION_Y_OFFSETS = (1, -1, 2, -1, 1, 0, 0)


def _load_payload(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(payload.get("cd"), list) or not isinstance(
        payload.get("sd"), Mapping
    ):
        raise ValueError("TDC payload must contain cd and sd")
    return {"cd": list(payload["cd"]), "sd": dict(payload["sd"])}


def _parse_jsonp_object(text: str, name: str) -> Mapping[str, Any]:
    open_paren = text.find("(")
    close_paren = text.rfind(")")
    if open_paren <= 0 or close_paren <= open_paren:
        raise ValueError(f"invalid {name} JSONP")
    payload = json.loads(text[open_paren + 1 : close_paren])
    if not isinstance(payload, Mapping):
        raise ValueError(f"{name} payload must be an object")
    return payload


def _parse_prehandle(text: str) -> _Prehandle:
    payload = _parse_jsonp_object(text, "cap_union_prehandle")
    sess = payload.get("sess")
    data = payload.get("data")
    if not isinstance(sess, str) or not isinstance(data, Mapping):
        raise ValueError("cap_union_prehandle is missing sess or data")
    common = data.get("comm_captcha_cfg")
    if not isinstance(common, Mapping) or not isinstance(common.get("tdc_path"), str):
        raise ValueError("cap_union_prehandle is missing tdc_path")
    raw_pow = common.get("pow_cfg")
    prefix = target = None
    if isinstance(raw_pow, Mapping):
        if isinstance(raw_pow.get("prefix"), str):
            prefix = raw_pow["prefix"]
        if isinstance(raw_pow.get("md5"), str):
            target = raw_pow["md5"]
    return _Prehandle(
        sess=sess,
        tdc_path=common["tdc_path"],
        pow_prefix=prefix,
        pow_md5=target,
        sid=str(payload["sid"]) if payload.get("sid") is not None else None,
    )


def _bootstrap_info(source: str) -> str:
    name_match = _TDC_NAME_PATTERN.search(source)
    if name_match is None:
        raise ValueError("TDC script does not define window.TDC_NAME")
    name = name_match.group("name")
    value_pattern = re.compile(
        rf"window(?:\.{re.escape(name)}|\[['\"]{re.escape(name)}['\"]\])"
        r"\s*=\s*([\"'])(?P<info>[A-Za-z0-9+/=]+)\1"
    )
    value_match = value_pattern.search(source, name_match.end())
    if value_match is None:
        raise ValueError("TDC script does not define its bootstrap info value")
    return value_match.group("info")


def _pack_tdc_string(value: str) -> bytes:
    encoded = value.encode("utf-16-le", "surrogatepass")
    units = [
        int.from_bytes(encoded[offset : offset + 2], "little")
        for offset in range(0, len(encoded), 2)
    ]
    padded_length = (len(units) + 7) // 8 * 8
    units.extend([0] * (padded_length - len(units)))
    packed = bytearray()
    for offset in range(0, len(units), 4):
        word = 0
        for shift, code_unit in enumerate(units[offset : offset + 4]):
            word |= code_unit << (shift * 8)
        packed.extend((word & _UINT32_MASK).to_bytes(4, "little"))
    return bytes(packed)


def _xtea_encrypt(block: bytes, profile: LiejuTdcProfile) -> bytes:
    if len(block) != 8:
        raise ValueError("TDC XTEA block must contain exactly 8 bytes")
    value_0 = int.from_bytes(block[:4], "little")
    value_1 = int.from_bytes(block[4:], "little")
    key = [
        int.from_bytes(profile.key[offset : offset + 4], "little")
        for offset in range(0, 16, 4)
    ]
    total = 0
    for _ in range(32):
        mix = (
            (((value_1 << 4) & _UINT32_MASK) ^ (value_1 >> 5)) + value_1
        ) & _UINT32_MASK
        value_0 = (
            value_0 + (mix ^ ((total + key[total & 3]) & _UINT32_MASK))
        ) & _UINT32_MASK
        total = (total + _XTEA_DELTA) & _UINT32_MASK
        mix = (
            (((value_0 << 4) & _UINT32_MASK) ^ (value_0 >> 5)) + value_0
        ) & _UINT32_MASK
        value_1 = (
            value_1 + (mix ^ ((total + key[(total >> 11) & 3]) & _UINT32_MASK))
        ) & _UINT32_MASK
    return value_0.to_bytes(4, "little") + value_1.to_bytes(4, "little")


def _encrypt_tdc_text(value: str, profile: LiejuTdcProfile) -> str:
    packed = _pack_tdc_string(value)
    ciphertext = b"".join(
        _xtea_encrypt(packed[offset : offset + 8], profile)
        for offset in range(0, len(packed), 8)
    )
    return base64.b64encode(ciphertext).decode("ascii")


def _js_utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _jquery_quote(value: str) -> str:
    return quote(value, safe="-_.!~*'()").replace("%20", "+")


def _form_body(params: Mapping[str, str | int]) -> str:
    return "&".join(
        f"{_jquery_quote(str(key))}={_jquery_quote(str(value))}"
        for key, value in params.items()
    )


def _solve_pow(prefix: str, target_md5: str, timeout_ms: int) -> tuple[str, int]:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", target_md5):
        raise ValueError("PoW target must be an MD5 digest")
    started = time.perf_counter()
    nonce = 0
    while True:
        answer = f"{prefix}{nonce}"
        if hashlib.md5(answer.encode("utf-8")).hexdigest() == target_md5.lower():
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return answer, elapsed_ms
        nonce += 1
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms > timeout_ms:
            raise TimeoutError(f"PoW search exceeded {timeout_ms} ms")


def _request_json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        value = response.json()
    except ValueError as error:
        raise UpstreamPublishError(
            "Lieju 验证接口返回了非 JSON 数据", response.status_code
        ) from error
    if not isinstance(value, Mapping):
        raise UpstreamPublishError("Lieju 验证接口返回格式无效", response.status_code)
    return value


def _parse_chaojiying_points(value: str) -> tuple[tuple[int, int], ...]:
    points: list[tuple[int, int]] = []
    for raw_point in value.split("|"):
        match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", raw_point)
        if match is None:
            raise ValueError(f"invalid point: {raw_point!r}")
        points.append((int(match.group(1)), int(match.group(2))))
    if not points:
        raise ValueError("no points returned")
    return tuple(points)


class ChaojiyingGapSolver(BaseRequest):
    """Submit a Tencent slider background to Chaojiying type 9602."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        soft_id: str,
        captcha_type: int = 9602,
        api_base: str = "https://upload.chaojiying.net",
        timeout_seconds: float = 30,
    ) -> None:
        if not all((username, password, soft_id)):
            raise ValueError("Chaojiying credentials must not be blank")
        if captcha_type <= 0:
            raise ValueError("Chaojiying captcha_type must be positive")
        if timeout_seconds <= 0:
            raise ValueError("Chaojiying timeout_seconds must be positive")
        super().__init__()
        self._username = username
        self._password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        self._soft_id = soft_id
        self._captcha_type = captcha_type
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @property
    def _base_params(self) -> dict[str, str]:
        return {
            "user": self._username,
            "pass2": self._password_hash,
            "softid": self._soft_id,
        }

    async def recognize(
        self,
        client: httpx.AsyncClient,
        image: bytes,
    ) -> _ChaojiyingResult:
        response = await self.request(
            "POST",
            f"{self._api_base}/Upload/Processing.php",
            data={**self._base_params, "codetype": str(self._captcha_type)},
            files={"userfile": ("lieju-slider.jpg", image, "image/jpeg")},
            headers={"User-Agent": USER_AGENT},
            timeout=self._timeout_seconds,
            _client=client,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamPublishError(
                "超级鹰识别接口返回了非 JSON 数据", response.status_code
            ) from error
        if not isinstance(payload, Mapping):
            raise UpstreamPublishError("超级鹰识别接口返回格式无效", response.status_code)

        error_number = payload.get("err_no")
        if error_number not in (0, "0"):
            message = payload.get("err_str")
            detail = str(message) if message else "未知错误"
            raise UpstreamPublishError(
                f"超级鹰滑块识别失败（err_no={error_number}）：{detail}",
                response.status_code,
            )
        raw_points = payload.get("pic_str")
        if not isinstance(raw_points, str):
            raise UpstreamPublishError(
                "超级鹰滑块识别结果缺少 pic_str", response.status_code
            )
        try:
            points = _parse_chaojiying_points(raw_points)
        except ValueError as error:
            raise UpstreamPublishError(
                f"超级鹰滑块识别结果格式无效：{error}", response.status_code
            ) from error
        raw_pic_id = payload.get("pic_id")
        pic_id = str(raw_pic_id) if raw_pic_id is not None else None
        return _ChaojiyingResult(pic_id=pic_id, points=points)

    async def report_error(self, client: httpx.AsyncClient, pic_id: str | None) -> None:
        if not pic_id:
            return
        try:
            response = await self.request(
                "POST",
                f"{self._api_base}/Upload/ReportError.php",
                data={**self._base_params, "id": pic_id},
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_seconds,
                _client=client,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # The original Tencent verification result is more useful than a
            # secondary reporting failure, so it remains the surfaced error.
            return


def _integer_pair(value: Any, field_name: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field_name} must be a two-item list")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError(f"{field_name} must contain numbers")
    return int(value[0]), int(value[1])


def _track_limits(value: Any) -> tuple[int | None, int | None]:
    if not isinstance(value, str):
        return None, None
    lower = re.search(r"\bx\s*>=\s*(-?\d+)", value)
    upper = re.search(r"\bx\s*<=\s*(-?\d+)", value)
    return (
        int(lower.group(1)) if lower is not None else None,
        int(upper.group(1)) if upper is not None else None,
    )


def _parse_challenge(payload: Mapping[str, Any], api_base: str) -> _CaptchaChallenge:
    if payload.get("ret") not in (0, "0"):
        raise ValueError(f"getsig returned ret={payload.get('ret')!r}")
    sess = payload.get("sess")
    data = payload.get("data")
    if not isinstance(sess, str) or not isinstance(data, Mapping):
        raise ValueError("getsig is missing sess or data")
    background = data.get("bg_elem_cfg")
    elements = data.get("fg_elem_list")
    if not isinstance(background, Mapping) or not isinstance(elements, list):
        raise ValueError("getsig is missing slider element configuration")
    image_path = background.get("img_url")
    if not isinstance(image_path, str) or not image_path:
        raise ValueError("getsig is missing the slider background image URL")
    logical_width = 672
    try:
        parsed_width, _ = _integer_pair(
            background.get("size_2d"), "background size_2d"
        )
        if parsed_width > 0:
            logical_width = parsed_width
    except ValueError:
        pass

    for element in elements:
        if not isinstance(element, Mapping):
            continue
        move_config = element.get("move_cfg")
        if not isinstance(move_config, Mapping):
            continue
        answer_types = move_config.get("data_type")
        if (
            not isinstance(answer_types, list)
            or "DynAnswerType_POS" not in answer_types
        ):
            continue
        element_id = element.get("id")
        if isinstance(element_id, bool) or not isinstance(element_id, int):
            continue
        try:
            width, _ = _integer_pair(element.get("size_2d"), "piece size_2d")
            init_x, init_y = _integer_pair(
                element.get("init_pos"), "piece init_pos"
            )
        except ValueError:
            continue
        if width <= 0:
            continue
        min_x, max_x = _track_limits(move_config.get("track_limit"))
        return _CaptchaChallenge(
            sess=sess,
            background_url=urljoin(f"{api_base.rstrip('/')}/", image_path),
            element_id=element_id,
            answer_type="DynAnswerType_POS",
            element_width=width,
            init_y=init_y,
            min_x=min_x,
            max_x=max_x,
            init_x=init_x,
            logical_background_width=logical_width,
        )
    raise ValueError("getsig did not contain a horizontal DynAnswerType_POS piece")


def _answer_from_gap(
    challenge: _CaptchaChallenge,
    gap: tuple[int, int],
) -> tuple[int, int]:
    """Convert Chaojiying's gap-center point to Tencent logical coordinates."""

    answer_x = math.floor(gap[0] - challenge.element_width / 2)
    if challenge.min_x is not None and answer_x < challenge.min_x:
        raise ValueError(f"x={answer_x} is before the slider track")
    if challenge.max_x is not None and answer_x > challenge.max_x:
        raise ValueError(f"x={answer_x} is after the slider track")
    return answer_x, challenge.init_y


def _parse_browser_challenge(
    prehandle_text: str,
    api_base: str = _BROWSER_CAPTCHA_API_BASE,
) -> _BrowserSliderChallenge:
    payload = _parse_jsonp_object(prehandle_text, "cap_union_prehandle")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("cap_union_prehandle is missing data")
    show_info = data.get("dyn_show_info")
    if not isinstance(show_info, Mapping):
        raise ValueError("cap_union_prehandle is missing dyn_show_info")
    background = show_info.get("bg_elem_cfg")
    elements = show_info.get("fg_elem_list")
    if not isinstance(background, Mapping) or not isinstance(elements, list):
        raise ValueError("dyn_show_info is missing slider element configuration")

    logical_width, _ = _integer_pair(
        background.get("size_2d"), "background size_2d"
    )
    image_path = background.get("img_url")
    if logical_width <= 0:
        raise ValueError("background width must be positive")
    if not isinstance(image_path, str) or not image_path:
        raise ValueError("dyn_show_info is missing the background image URL")

    for element in elements:
        if not isinstance(element, Mapping):
            continue
        move_config = element.get("move_cfg")
        if not isinstance(move_config, Mapping):
            continue
        answer_types = move_config.get("data_type")
        if not isinstance(answer_types, list) or "DynAnswerType_POS" not in answer_types:
            continue
        try:
            element_width, _ = _integer_pair(
                element.get("size_2d"), "piece size_2d"
            )
            init_x, init_y = _integer_pair(element.get("init_pos"), "piece init_pos")
        except ValueError:
            continue
        if element_width <= 0:
            continue
        min_x, max_x = _track_limits(move_config.get("track_limit"))
        return _BrowserSliderChallenge(
            background_url=urljoin(f"{api_base.rstrip('/')}/", image_path),
            logical_background_width=logical_width,
            element_width=element_width,
            init_x=init_x,
            init_y=init_y,
            min_x=min_x,
            max_x=max_x,
        )
    raise ValueError("dyn_show_info did not contain a horizontal slider piece")


def _browser_drag_distance(
    challenge: _BrowserSliderChallenge,
    gap: tuple[int, int],
    rendered_background_width: float,
) -> tuple[int, float]:
    if rendered_background_width <= 0:
        raise ValueError("rendered background width must be positive")
    answer_x = math.floor(gap[0] - challenge.element_width / 2)
    if challenge.min_x is not None and answer_x < challenge.min_x:
        raise ValueError(f"x={answer_x} is before the slider track")
    if challenge.max_x is not None and answer_x > challenge.max_x:
        raise ValueError(f"x={answer_x} is after the slider track")
    distance = (
        (answer_x - challenge.init_x)
        * rendered_background_width
        / challenge.logical_background_width
    )
    if distance <= 0:
        raise ValueError("drag distance must be positive")
    return answer_x, distance


def _drag_track(
    challenge: _CaptchaChallenge,
    answer: tuple[int, int],
    now_ms: int,
) -> list[list[int]]:
    distance = round(
        (answer[0] - challenge.init_x)
        * _TDC_RENDERED_BACKGROUND_WIDTH
        / challenge.logical_background_width
    )
    if distance <= 0:
        raise ValueError("calculated slider drag distance must be positive")

    track: list[list[int]] = [
        [4, 520 + secrets.randbelow(80), 110 + secrets.randbelow(50), now_ms - 3200, 0, 0, 0, 0],
        [
            1,
            65 + secrets.randbelow(40),
            260 + secrets.randbelow(70),
            700 + secrets.randbelow(350),
            0,
            0,
            0,
            0,
        ],
        [1, -30 - secrets.randbelow(30), -25 - secrets.randbelow(30), 34, 0, 0, 0, 0],
        [1, -15 - secrets.randbelow(20), -10 - secrets.randbelow(20), 33, 0, 0, 0, 0],
        [1, -5 - secrets.randbelow(10), -3 - secrets.randbelow(8), 35, 0, 0, 0, 0],
        [2, 0, 0, 90 + secrets.randbelow(35), 0, 0, 0, 0],
    ]

    previous = 0
    steps = 28
    y_pattern = (0, -1, 0, 1, 0, 0, -1, 1)
    for index in range(1, steps + 1):
        progress = index / steps
        current = round(distance * (1 - (1 - progress) ** 3))
        delta_x = current - previous
        previous = current
        track.append(
            [
                1,
                delta_x,
                y_pattern[index % len(y_pattern)],
                28 + secrets.randbelow(13),
                0,
                0,
                0,
                0,
            ]
        )
    track.extend(
        (
            [1, 0, 0, 80 + secrets.randbelow(60), 0, 0, 0, 0],
            [3, 0, 0, 85 + secrets.randbelow(35), 0, 0, 0, 0],
        )
    )
    return track


def _dynamic_payload(
    template: Mapping[str, Any],
    *,
    category_url: str,
    session_marker: str,
    challenge: _CaptchaChallenge,
    answer: tuple[int, int],
    feature_flags: str | None,
    is_new_entry: int,
) -> dict[str, Any]:
    payload = copy.deepcopy(dict(template))
    cd = payload.get("cd")
    sd = payload.get("sd")
    if not isinstance(cd, list) or len(cd) < 73 or not isinstance(sd, dict):
        raise ValueError("dynamic TDC template must contain the 73-field browser profile")

    now_ms = int(time.time() * 1000)
    now_seconds = now_ms // 1000
    track = _drag_track(challenge, answer, now_ms)
    cd[5] = category_url
    cd[15] = now_seconds
    cd[21] = now_seconds - 5
    cd[25] = f"{_TDC_IFRAME_REFERER}?rand={now_ms}"
    cd[41] = now_seconds - 80
    cd[45] = {"workerTime": now_ms - 5_000, "diff": 10 + secrets.randbelow(12)}
    cd[46] = {"kc": 0, "mc": len(track) + 35, "tc": 0, "pc": len(track) + 34}
    cd[51] = track
    cd[55] = USER_AGENT
    marker = int.from_bytes(
        hashlib.sha256(session_marker.encode("utf-8")).digest()[:8], "big"
    )
    cd[56] = f"1;{marker}"

    raw_slide_value = sd.get("slideValue")
    if isinstance(raw_slide_value, list) and raw_slide_value:
        first = raw_slide_value[0]
        if isinstance(first, list) and len(first) >= 4:
            first[3] = now_ms - 35_000
    sd["isNewEntry"] = is_new_entry
    if feature_flags is not None:
        sd["ft"] = feature_flags
    sd["tdc_perf"] = {
        "fi": 70 + secrets.randbelow(25),
        "pi": 45 + secrets.randbelow(25),
        "gd": 65 + secrets.randbelow(25),
    }
    return {"cd": cd, "sd": sd}


def _css_background_url(value: str) -> str | None:
    match = re.search(
        r"url\(\s*(?P<quote>['\"]?)(?P<url>.+?)(?P=quote)\s*\)", value
    )
    if match is None:
        return None
    result = match.group("url").strip()
    return result or None


def _browser_bridge_html(appid: str) -> str:
    safe_appid = escape(appid, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <script>
    window.__captchaResult = null;
    window.{_BROWSER_CAPTCHA_CALLBACK} = function (result) {{
      window.__captchaResult = result;
    }};
  </script>
  <script src="{_BROWSER_CAPTCHA_SCRIPT}"></script>
</head>
<body>
  <button id="TencentCaptcha" data-appid="{safe_appid}"
          data-cbfn="{_BROWSER_CAPTCHA_CALLBACK}">验证</button>
</body>
</html>"""


def _captcha_result_error(result: Mapping[str, Any]) -> str:
    code = result.get("errorCode", result.get("ret", "unknown"))
    raw_message = result.get("errMessage") or result.get("message")
    message = f"，message={raw_message}" if raw_message else ""
    return f"code={code}{message}"


class LiejuBrowserCaptchaFlow(BaseRequest):
    """Run Tencent's current TDC implementation in Chrome and drag its slider."""

    def __init__(
        self,
        *,
        chaojiying_solver: ChaojiyingGapSolver,
        chaojiying_point_index: int = 0,
        captcha_max_attempts: int = 1,
        executable_path: str | Path | None = None,
        headless: bool = True,
        timeout_seconds: float = 30,
        user_agent: str = USER_AGENT,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if chaojiying_point_index < 0:
            raise ValueError("chaojiying_point_index must not be negative")
        if captcha_max_attempts < 1:
            raise ValueError("captcha_max_attempts must be at least one")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        super().__init__(client_factory=client_factory)
        self._chaojiying_solver = chaojiying_solver
        self._chaojiying_point_index = chaojiying_point_index
        self._captcha_max_attempts = captcha_max_attempts
        self._executable_path = (
            Path(executable_path).expanduser() if executable_path else None
        )
        self._headless = headless
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent

    @classmethod
    def from_environment(cls) -> "LiejuBrowserCaptchaFlow | None":
        return cls.from_settings(get_settings())

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiejuBrowserCaptchaFlow | None":
        if not getattr(settings, "lieju_captcha_browser_enabled", False):
            return None
        chaojiying_values = (
            settings.lieju_chaojiying_username,
            settings.lieju_chaojiying_password,
            settings.lieju_chaojiying_soft_id,
        )
        if not all(chaojiying_values):
            raise PublisherConfigurationError(
                "浏览器验证码模式需要完整的超级鹰用户名、密码和软件 ID"
            )
        executable_path = getattr(
            settings, "lieju_captcha_browser_executable_path", None
        )
        if (
            executable_path is not None
            and not Path(executable_path).expanduser().is_file()
        ):
            raise PublisherConfigurationError(
                f"浏览器验证码可执行文件不存在：{executable_path}"
            )
        try:
            return cls(
                chaojiying_solver=ChaojiyingGapSolver(
                    username=settings.lieju_chaojiying_username or "",
                    password=settings.lieju_chaojiying_password or "",
                    soft_id=settings.lieju_chaojiying_soft_id or "",
                    captcha_type=settings.lieju_chaojiying_type,
                    api_base=settings.lieju_chaojiying_api_base,
                    timeout_seconds=settings.lieju_chaojiying_timeout_seconds,
                ),
                chaojiying_point_index=settings.lieju_chaojiying_point_index,
                captcha_max_attempts=settings.lieju_captcha_max_attempts,
                executable_path=executable_path,
                headless=settings.lieju_captcha_browser_headless,
                timeout_seconds=settings.lieju_captcha_browser_timeout_seconds,
            )
        except (TypeError, ValueError) as error:
            raise PublisherConfigurationError(
                "Lieju 浏览器验证码环境变量格式无效"
            ) from error

    async def _recognize_background(
        self,
        client: httpx.AsyncClient,
        image: bytes,
    ) -> _ChaojiyingResult:
        result = await self._chaojiying_solver.recognize(client, image)
        if self._chaojiying_point_index >= len(result.points):
            await self._chaojiying_solver.report_error(client, result.pic_id)
            raise UpstreamPublishError(
                "超级鹰滑块识别结果没有配置索引对应的缺口点"
            )
        return result

    async def _run_attempt(
        self,
        *,
        browser: Any,
        recognition_client: httpx.AsyncClient,
        category_url: str,
        appid: str,
    ) -> LiejuCaptchaTicket:
        timeout_ms = self._timeout_seconds * 1000
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=self._user_agent,
        )
        recognized: _ChaojiyingResult | None = None
        try:
            await context.add_init_script(
                "Object.defineProperty(Navigator.prototype, 'webdriver', "
                "{get: () => undefined});"
            )
            page = await context.new_page()

            async def serve_bridge(route: Any) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_browser_bridge_html(appid),
                )

            await page.route(category_url, serve_bridge)
            await page.goto(
                category_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            await page.wait_for_function(
                "typeof window.TencentCaptcha === 'function'", timeout=timeout_ms
            )
            async with page.expect_response(
                lambda response: "/cap_union_prehandle" in response.url,
                timeout=timeout_ms,
            ) as response_info:
                await page.locator("#TencentCaptcha").click(timeout=timeout_ms)
            prehandle_response = await response_info.value
            challenge = _parse_browser_challenge(await prehandle_response.text())

            iframe = page.locator("iframe[src*='drag_ele']")
            await iframe.wait_for(state="attached", timeout=timeout_ms)
            iframe_src = await iframe.get_attribute("src")
            frame = page.frame_locator("iframe[src*='drag_ele']")
            background = frame.locator("#slideBg")
            await background.wait_for(state="visible", timeout=timeout_ms)
            background_box = await background.bounding_box()
            if background_box is None:
                raise ValueError("captcha background does not have a bounding box")
            background_style = await background.evaluate(
                "element => getComputedStyle(element).backgroundImage"
            )
            style_url = (
                _css_background_url(background_style)
                if isinstance(background_style, str)
                else None
            )
            image_url = urljoin(
                iframe_src or challenge.background_url,
                style_url or challenge.background_url,
            )
            image_response = await self.request(
                "GET",
                image_url,
                headers={"Referer": iframe_src or _BROWSER_CAPTCHA_API_BASE},
                timeout=timeout_ms,
                _client=context.request,
            )
            try:
                if not image_response.ok:
                    raise UpstreamPublishError(
                        "Lieju 浏览器验证码背景图请求失败", image_response.status
                    )
                image = await image_response.body()
            finally:
                await image_response.dispose()
            if not image:
                raise UpstreamPublishError("Lieju 浏览器验证码背景图为空")

            recognized = await self._recognize_background(
                recognition_client, image
            )
            gap = recognized.points[self._chaojiying_point_index]
            _, distance = _browser_drag_distance(
                challenge, gap, background_box["width"]
            )

            slider = frame.locator(".tc-fg-item.tc-slider-normal")
            await slider.wait_for(state="visible", timeout=timeout_ms)
            slider_box = await slider.bounding_box()
            if slider_box is None:
                raise ValueError("captcha slider does not have a bounding box")
            start_x = slider_box["x"] + slider_box["width"] / 2
            start_y = slider_box["y"] + slider_box["height"] / 2
            await page.mouse.move(start_x - 24, start_y + 4)
            await page.mouse.move(start_x, start_y, steps=5)
            await page.wait_for_timeout(90 + secrets.randbelow(70))
            await page.mouse.down()
            for ratio, y_offset in zip(
                _BROWSER_MOTION_RATIOS, _BROWSER_MOTION_Y_OFFSETS
            ):
                await page.mouse.move(
                    start_x + distance * ratio,
                    start_y + y_offset,
                    steps=2,
                )
                await page.wait_for_timeout(45 + secrets.randbelow(45))
            await page.mouse.up()
            await page.wait_for_function(
                "window.__captchaResult !== null", timeout=timeout_ms
            )
            result = await page.evaluate("window.__captchaResult")
            if not isinstance(result, Mapping):
                raise UpstreamPublishError("腾讯验证码回调结果格式无效")
            success = result.get("ret") in (0, "0") or result.get(
                "errorCode"
            ) in (0, "0")
            ticket = result.get("ticket")
            randstr = result.get("randstr")
            if success and isinstance(ticket, str) and isinstance(randstr, str):
                if ticket and randstr:
                    return LiejuCaptchaTicket(ticket=ticket, randstr=randstr)
            raise UpstreamPublishError(
                f"腾讯浏览器验证码返回失败：{_captcha_result_error(result)}"
            )
        except Exception:
            if recognized is not None:
                await self._chaojiying_solver.report_error(
                    recognition_client, recognized.pic_id
                )
            raise
        finally:
            await context.close()

    async def solve(
        self,
        *,
        category_url: str,
        form: CaptchaForm,
    ) -> LiejuCaptchaTicket:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise PublisherConfigurationError(
                "Lieju 浏览器验证码需要安装 playwright 依赖"
            ) from error

        launch_options: dict[str, Any] = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._executable_path is not None:
            launch_options["executable_path"] = str(self._executable_path)
        appid = form.captcha_appid or "2060609993"

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**launch_options)
            except PlaywrightError as error:
                raise UpstreamPublishError(
                    "Lieju 浏览器验证码启动 Chrome 失败"
                ) from error
            try:
                async with self._client_factory(
                    timeout=self._timeout_seconds,
                    follow_redirects=True,
                ) as recognition_client:
                    for attempt in range(self._captcha_max_attempts):
                        cause: BaseException | None = None
                        try:
                            return await self._run_attempt(
                                browser=browser,
                                recognition_client=recognition_client,
                                category_url=category_url,
                                appid=appid,
                            )
                        except UpstreamPublishError as error:
                            failure = error
                        except httpx.HTTPError as error:
                            cause = error
                            status = (
                                error.response.status_code
                                if isinstance(error, httpx.HTTPStatusError)
                                else None
                            )
                            failure = UpstreamPublishError(
                                "Lieju 浏览器验证码请求失败", status
                            )
                        except (PlaywrightError, TypeError, ValueError) as error:
                            cause = error
                            failure = UpstreamPublishError(
                                "Lieju 浏览器验证码交互失败"
                            )
                        if attempt + 1 == self._captcha_max_attempts:
                            if cause is not None:
                                raise failure from cause
                            raise failure
                        await asyncio.sleep(0.2)
            finally:
                await browser.close()
        raise UpstreamPublishError("Lieju 浏览器验证码未返回结果")


class LiejuPurePythonCaptchaFlow(BaseRequest):
    """Acquire a Lieju captcha session and exchange it for ticket/randstr."""

    def __init__(
        self,
        *,
        tdc_payload: Mapping[str, Any] | str | Path,
        tdc_profile: LiejuTdcProfile | None,
        answer: tuple[int, int] | None,
        chaojiying_solver: ChaojiyingGapSolver | None = None,
        chaojiying_point_index: int = 0,
        captcha_max_attempts: int = 1,
        api_base: str = "https://t.captcha.qq.com",
        user_agent: str = USER_AGENT,
        feature_flags: str | None = None,
        is_new_entry: int = 0,
        capture_request_path: str | Path | None = None,
        callback: str | None = None,
        pow_timeout_ms: int = 30_000,
        request_timeout_seconds: float = 15,
        dynamic_tdc: bool = False,
        runtime_timeout_seconds: float = 20,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if pow_timeout_ms < 0:
            raise ValueError("pow_timeout_ms must not be negative")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if runtime_timeout_seconds <= 0:
            raise ValueError("runtime_timeout_seconds must be positive")
        if not dynamic_tdc and tdc_profile is None:
            raise ValueError("a static TDC profile is required")
        if answer is None and chaojiying_solver is None:
            raise ValueError("a static answer or Chaojiying solver is required")
        if chaojiying_point_index < 0:
            raise ValueError("chaojiying_point_index must not be negative")
        if captcha_max_attempts < 1:
            raise ValueError("captcha_max_attempts must be at least one")
        super().__init__(client_factory=client_factory)
        self._payload = _load_payload(tdc_payload)
        self._profile = tdc_profile
        self._answer = answer
        self._chaojiying_solver = chaojiying_solver
        self._chaojiying_point_index = chaojiying_point_index
        self._captcha_max_attempts = captcha_max_attempts
        self._api_base = api_base.rstrip("/")
        self._user_agent = user_agent
        self._feature_flags = feature_flags
        self._is_new_entry = is_new_entry
        self._capture_request_path = Path(capture_request_path) if capture_request_path else None
        self._callback = callback
        self._pow_timeout_ms = pow_timeout_ms
        self._request_timeout_seconds = request_timeout_seconds
        self._dynamic_tdc = dynamic_tdc
        self._runtime_timeout_seconds = runtime_timeout_seconds

    @classmethod
    def from_environment(cls) -> "LiejuPurePythonCaptchaFlow | None":
        return cls.from_settings(get_settings())

    @classmethod
    def from_settings(cls, settings: Settings) -> "LiejuPurePythonCaptchaFlow | None":
        payload_path = settings.lieju_tdc_payload_path
        profile_sha = settings.lieju_tdc_sha256
        raw_key = settings.lieju_tdc_key
        dynamic_tdc = getattr(settings, "lieju_tdc_dynamic", False)
        answer_text = settings.lieju_captcha_answer
        chaojiying_values = (
            settings.lieju_chaojiying_username,
            settings.lieju_chaojiying_password,
            settings.lieju_chaojiying_soft_id,
        )
        if any(chaojiying_values) and not all(chaojiying_values):
            raise PublisherConfigurationError(
                "超级鹰配置不完整：需要用户名、密码和软件 ID"
            )
        if not payload_path:
            return None
        if not dynamic_tdc and not all((profile_sha, raw_key)):
            return None
        if not answer_text and not all(chaojiying_values):
            return None
        try:
            answer = None
            if answer_text:
                x_text, y_text = answer_text.split(",", 1)
                answer = (int(x_text.strip()), int(y_text.strip()))
            profile = None
            if not dynamic_tdc:
                assert profile_sha is not None and raw_key is not None
                if len(raw_key) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", raw_key):
                    key = bytes.fromhex(raw_key)
                else:
                    key = raw_key.encode("utf-8")
                profile = LiejuTdcProfile(
                    script_sha256=profile_sha.lower(), key=key
                )
            chaojiying_solver = None
            if all(chaojiying_values):
                chaojiying_solver = ChaojiyingGapSolver(
                    username=settings.lieju_chaojiying_username or "",
                    password=settings.lieju_chaojiying_password or "",
                    soft_id=settings.lieju_chaojiying_soft_id or "",
                    captcha_type=settings.lieju_chaojiying_type,
                    api_base=settings.lieju_chaojiying_api_base,
                    timeout_seconds=settings.lieju_chaojiying_timeout_seconds,
                )
            return cls(
                tdc_payload=payload_path,
                tdc_profile=profile,
                answer=answer,
                chaojiying_solver=chaojiying_solver,
                chaojiying_point_index=settings.lieju_chaojiying_point_index,
                captcha_max_attempts=settings.lieju_captcha_max_attempts,
                feature_flags=settings.lieju_tdc_feature_flags,
                is_new_entry=settings.lieju_tdc_is_new_entry,
                capture_request_path=settings.lieju_capture_request_path,
                callback=settings.lieju_tdc_callback,
                pow_timeout_ms=settings.lieju_tdc_pow_timeout_ms,
                request_timeout_seconds=settings.lieju_captcha_timeout_seconds,
                dynamic_tdc=dynamic_tdc,
                runtime_timeout_seconds=getattr(
                    settings, "lieju_tdc_runtime_timeout_seconds", 20
                ),
            )
        except (OSError, TypeError, ValueError) as error:
            raise PublisherConfigurationError(
                "Lieju 验证码环境变量格式无效"
            ) from error

    def _prehandle_url(self, category_url: str, form: CaptchaForm) -> str:
        appid = form.captcha_appid or "2060609993"
        callback = (
            form.captcha_callback
            or self._callback
            or f"_aq_{secrets.randbelow(900000) + 100000}"
        )
        params = {
            "aid": appid,
            "protocol": "https",
            "accver": "1",
            "showtype": "popup",
            "ua": base64.b64encode(self._user_agent.encode("utf-8")).decode("ascii"),
            "noheader": "1",
            "fb": "1",
            "aged": "0",
            "enableAged": "0",
            "enableDarkMode": "0",
            "grayscale": "1",
            "dyeid": "0",
            "clientype": "2",
            "cap_cd": "",
            "uid": "",
            "lang": "en",
            "entry_url": category_url,
            "elder_captcha": "0",
            "js": "/tcaptcha-frame.a456d6f9.js",
            "login_appid": "",
            "support_media": "jpeg,png,gif,webp,mp4,webm",
            "wb": "1",
            "version": "1.1.0",
            "subsid": "1",
            "callback": callback,
            "sess": "",
            "agent_id": "",
            "agent_auth_sign": "",
        }
        return f"{self._api_base}/cap_union_prehandle?{urlencode(params)}"

    def _captcha_headers(self, category_url: str) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Referer": category_url,
            "User-Agent": self._user_agent,
        }

    def _build_params(
        self,
        source: str,
        sess: str,
        answer: tuple[int, int],
        *,
        element_id: int = 1,
        answer_type: str = "DynAnswerType_POS",
        category_url: str = "",
        session_marker: str = "",
        challenge: _CaptchaChallenge | None = None,
    ) -> dict[str, str | int]:
        if self._dynamic_tdc:
            if challenge is None or not category_url:
                raise ValueError("dynamic TDC mode requires current challenge geometry")
            payload = _dynamic_payload(
                self._payload,
                category_url=category_url,
                session_marker=session_marker or sess,
                challenge=challenge,
                answer=answer,
                feature_flags=self._feature_flags,
                is_new_entry=self._is_new_entry,
            )
            collect = encode_tdc_payload(
                source,
                payload,
                timeout_seconds=self._runtime_timeout_seconds,
            )
        else:
            assert self._profile is not None
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest != self._profile.script_sha256:
                raise PublisherConfigurationError(
                    "Lieju TDC 脚本 SHA 与 profile 不匹配："
                    f"expected {self._profile.script_sha256}, got {digest}"
                )
            sd = dict(self._payload["sd"])
            sd["isNewEntry"] = self._is_new_entry
            if self._feature_flags is not None:
                sd["ft"] = self._feature_flags
            plaintext = json.dumps(
                {"cd": self._payload["cd"], "sd": sd},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            collect = _encrypt_tdc_text(plaintext, self._profile)
        ans = json.dumps(
            [
                {
                    "elem_id": element_id,
                    "type": answer_type,
                    "data": f"{answer[0]},{answer[1]}",
                }
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        params: dict[str, str | int] = {
            "collect": collect,
            "tlg": _js_utf16_length(collect),
            "eks": _bootstrap_info(source),
            "sess": sess,
            "ans": ans,
        }
        return params

    async def _get_challenge(
        self,
        client: httpx.AsyncClient,
        *,
        sess: str,
        category_url: str,
    ) -> _CaptchaChallenge:
        response = await self.request(
            "POST",
            f"{self._api_base}/cap_union_new_getsig",
            content=_form_body(
                {
                    "sess": sess,
                    "extra_info": json.dumps(
                        {"refresh_trigger": 1}, separators=(",", ":")
                    ),
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": category_url,
                "User-Agent": self._user_agent,
            },
            _client=client,
        )
        response.raise_for_status()
        try:
            return _parse_challenge(_request_json(response), self._api_base)
        except ValueError as error:
            raise UpstreamPublishError(
                f"Lieju 验证码 getsig 返回格式无效：{error}", response.status_code
            ) from error

    async def _download_background(
        self,
        client: httpx.AsyncClient,
        challenge: _CaptchaChallenge,
        *,
        category_url: str,
    ) -> bytes:
        response = await self.request(
            "GET",
            challenge.background_url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": _TDC_IFRAME_REFERER,
                "User-Agent": self._user_agent,
            },
            _client=client,
        )
        response.raise_for_status()
        if not response.content:
            raise UpstreamPublishError(
                "Lieju 验证码背景图为空", response.status_code
            )
        return response.content

    def _chaojiying_answer(
        self,
        challenge: _CaptchaChallenge,
        result: _ChaojiyingResult,
    ) -> tuple[int, int]:
        if self._chaojiying_point_index >= len(result.points):
            raise UpstreamPublishError(
                "超级鹰滑块识别结果没有配置索引对应的缺口点"
            )
        try:
            return _answer_from_gap(
                challenge, result.points[self._chaojiying_point_index]
            )
        except ValueError as error:
            raise UpstreamPublishError(
                f"超级鹰滑块识别坐标不在当前腾讯滑轨范围内：{error}"
            ) from error

    def _save_capture_request(
        self,
        *,
        verify_url: str,
        verify_headers: Mapping[str, str],
        body: str,
        category_url: str,
    ) -> None:
        if self._capture_request_path is None:
            return
        record = {
            "method": "POST",
            "url": verify_url,
            "headers": dict(verify_headers),
            "body": body,
            "documentUrl": category_url,
            "documentTitle": "",
            "documentIsTopFrame": True,
        }
        self._capture_request_path.parent.mkdir(parents=True, exist_ok=True)
        self._capture_request_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    async def solve(
        self,
        *,
        category_url: str,
        form: CaptchaForm,
    ) -> LiejuCaptchaTicket:
        try:
            async with self._client_factory(
                timeout=self._request_timeout_seconds,
                follow_redirects=True,
            ) as client:
                headers = self._captcha_headers(category_url)
                prehandle_url = self._prehandle_url(category_url, form)
                attempts = (
                    self._captcha_max_attempts
                    if self._chaojiying_solver is not None
                    else 1
                )
                for attempt in range(attempts):
                    prehandle_response = await self.request(
                        "GET", prehandle_url, headers=headers, _client=client
                    )
                    prehandle_response.raise_for_status()
                    try:
                        prehandle = _parse_prehandle(prehandle_response.text)
                    except (TypeError, ValueError) as error:
                        raise UpstreamPublishError(
                            "Lieju 验证码 prehandle 返回格式无效",
                            prehandle_response.status_code,
                        ) from error

                    tdc_url = urljoin(prehandle_url, prehandle.tdc_path)
                    tdc_response = await self.request(
                        "GET", tdc_url, headers=headers, _client=client
                    )
                    tdc_response.raise_for_status()
                    source = tdc_response.text

                    answer = self._answer
                    element_id = 1
                    answer_type = "DynAnswerType_POS"
                    chaojiying_result = None
                    sess = prehandle.sess
                    challenge = None
                    try:
                        geometry = _parse_browser_challenge(
                            prehandle_response.text, self._api_base
                        )
                        challenge = _CaptchaChallenge(
                            sess=prehandle.sess,
                            background_url=geometry.background_url,
                            element_id=1,
                            answer_type="DynAnswerType_POS",
                            element_width=geometry.element_width,
                            init_y=geometry.init_y,
                            min_x=geometry.min_x,
                            max_x=geometry.max_x,
                            init_x=geometry.init_x,
                            logical_background_width=(
                                geometry.logical_background_width
                            ),
                        )
                    except ValueError:
                        pass
                    if self._chaojiying_solver is not None:
                        if challenge is None:
                            challenge = await self._get_challenge(
                                client,
                                sess=prehandle.sess,
                                category_url=category_url,
                            )
                        background = await self._download_background(
                            client,
                            challenge,
                            category_url=category_url,
                        )
                        chaojiying_result = await self._chaojiying_solver.recognize(
                            client, background
                        )
                        answer = self._chaojiying_answer(
                            challenge, chaojiying_result
                        )
                        element_id = challenge.element_id
                        answer_type = challenge.answer_type
                        sess = challenge.sess
                    assert answer is not None
                    try:
                        params = await asyncio.to_thread(
                            self._build_params,
                            source,
                            sess,
                            answer,
                            element_id=element_id,
                            answer_type=answer_type,
                            category_url=category_url,
                            session_marker=prehandle.sid or prehandle.sess,
                            challenge=challenge,
                        )
                    except PublisherConfigurationError:
                        raise
                    except (TypeError, ValueError) as error:
                        raise UpstreamPublishError(
                            f"Lieju TDC 实时画像生成失败：{error}"
                        ) from error

                    if bool(prehandle.pow_prefix) != bool(prehandle.pow_md5):
                        raise UpstreamPublishError(
                            "Lieju 验证码 prehandle 返回了不完整的 PoW 配置",
                            prehandle_response.status_code,
                        )
                    if prehandle.pow_prefix and prehandle.pow_md5:
                        try:
                            pow_answer, duration_ms = await asyncio.to_thread(
                                _solve_pow,
                                prehandle.pow_prefix,
                                prehandle.pow_md5,
                                self._pow_timeout_ms,
                            )
                        except (TimeoutError, ValueError) as error:
                            raise UpstreamPublishError(
                                f"Lieju 验证码 PoW 计算失败：{error}"
                            ) from error
                        params["pow_answer"] = pow_answer
                        params["pow_calc_time"] = duration_ms

                    body = _form_body(params)
                    verify_url = f"{self._api_base}/cap_union_new_verify"
                    verify_headers = {
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Origin": "https://captcha.gtimg.com",
                        "Referer": _TDC_IFRAME_REFERER,
                        "User-Agent": self._user_agent,
                    }
                    self._save_capture_request(
                        verify_url=verify_url,
                        verify_headers=verify_headers,
                        body=body,
                        category_url=category_url,
                    )
                    verify_response = await self.request(
                        "POST",
                        verify_url,
                        content=body.encode("utf-8"),
                        headers=verify_headers,
                        _client=client,
                    )
                    verify_response.raise_for_status()
                    result = _request_json(verify_response)
                    success = result.get("errorCode") in (0, "0") or result.get("ret") in (0, "0")
                    ticket = result.get("ticket")
                    randstr = result.get("randstr")
                    if success and isinstance(ticket, str) and isinstance(randstr, str):
                        return LiejuCaptchaTicket(ticket=ticket, randstr=randstr)

                    if (
                        self._chaojiying_solver is not None
                        and chaojiying_result is not None
                    ):
                        await self._chaojiying_solver.report_error(
                            client, chaojiying_result.pic_id
                        )
                    failure = UpstreamPublishError(
                        "Lieju 验证接口返回失败："
                        f"{json.dumps(dict(result), ensure_ascii=False)}",
                        verify_response.status_code,
                    )
                    if attempt + 1 == attempts:
                        raise failure
        except httpx.HTTPError as error:
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            raise UpstreamPublishError("Lieju 验证码请求失败", status) from error
