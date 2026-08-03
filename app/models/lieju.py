from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import unescape
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import markdown
from lxml import html as lxml_html
from PIL import Image, ImageSequence

from app.config import get_settings
from app.domain import (
    Credentials,
    FieldControlType,
    FieldOption,
    PlatformFieldValue,
    PublishFieldRequirement,
    PublishRequirements,
    PublishResult,
)
from app.errors import (
    CaptchaRequiredError,
    LoginExpiredError,
    PlatformFieldsValidationError,
    PublisherConfigurationError,
    UpstreamPublishError,
)
from app.models.base import ArticlePublisher
from app.models.lieju_captcha import (
    LiejuCaptchaSolver,
    LiejuPurePythonCaptchaFlow,
)
from app.schemas import ContentType


BASE_URL = "https://post.lieju.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36"
)
_CATEGORY_VALUE_PATTERN = re.compile(r"^(\d+)/(\d+)$")
_ARTICLE_PATH_PATTERN = re.compile(r"^/[^/]+/\d+\.html$")
_LENGTH_DATATYPE_PATTERN = re.compile(r"^\*(\d+)-(\d+)$")
_IMAGE_INPUT_PATTERN = re.compile(r"^local_file(\d+)$")
_IMAGE_LIMIT_PATTERN = re.compile(r"\blimitnum\s*=\s*(\d+)\s*;")
_BARE_URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
_TOP_LEVEL_FIELDS = {"title", "content"}
_INTERNAL_FIELDS = {"autofill"}
_CAPTCHA_FIELDS = {"atc_yzm", "postdb[ticket]", "postdb[randstr]"}
_PROMOTION_FIELDS = {"dtop", "topday", "bank_code"}
_IMAGE_CONTENT_TYPES = {
    "image/gif": ("gif", "image/gif"),
    "image/jpeg": ("jpg", "image/jpeg"),
    "image/png": ("png", "image/png"),
}
_IMAGE_PATH_SUFFIXES = (".gif", ".jpeg", ".jpg", ".png")
_MAX_IMAGE_BYTES = 1024 * 1024
_MAX_SOURCE_IMAGE_BYTES = 20 * _MAX_IMAGE_BYTES
_MAX_IMAGE_PIXELS = 40_000_000
_IMAGE_SCALE_STEPS = (1.0, 0.85, 0.7, 0.55, 0.4, 0.3)
_JPEG_QUALITY_STEPS = (85, 75, 65, 55, 45)
_PALETTE_COLOR_STEPS = (256, 128, 64, 32)
_BLOCK_TAGS = (
    "address",
    "article",
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "section",
)


@dataclass(frozen=True)
class _ParsedControl:
    raw_name: str
    requirement: PublishFieldRequirement


@dataclass(frozen=True)
class _ParsedForm:
    hidden_fields: tuple[tuple[str, str], ...]
    controls: tuple[_ParsedControl, ...]
    submit_fields: tuple[tuple[str, str], ...]
    image_slots: tuple[int, ...]
    captcha_required: bool
    captcha_fields: tuple[tuple[str, str], ...]
    captcha_appid: str | None
    captcha_callback: str | None

    @property
    def captcha_satisfied(self) -> bool:
        values = dict(self.captcha_fields)
        return (
            values.get("atc_yzm") == "1"
            and bool(values.get("postdb[ticket]"))
            and bool(values.get("postdb[randstr]"))
        )

    @property
    def public_controls(self) -> tuple[_ParsedControl, ...]:
        return tuple(
            control
            for control in self.controls
            if control.raw_name.startswith("postdb[")
            and control.requirement.key not in _TOP_LEVEL_FIELDS | _INTERNAL_FIELDS
            and control.raw_name not in _CAPTCHA_FIELDS
    )


@dataclass(frozen=True)
class _DownloadedImage:
    slot: int
    filename: str
    content: bytes
    content_type: str


def _has_class(class_name: str) -> str:
    return (
        "contains(concat(' ', normalize-space(@class), ' '), "
        f"' {class_name} ')"
    )


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _field_key(raw_name: str) -> str:
    match = re.fullmatch(r"postdb\[([^]]+)](?:\[\])?", raw_name)
    return match.group(1) if match else raw_name


def _label_text(raw_label: str, fallback: str) -> str:
    label = raw_label.strip()
    if label.startswith("*"):
        label = label[1:].strip()
    label = label.rstrip(":：").strip()
    return label or fallback


def _control_type(element: Any) -> FieldControlType | None:
    if element.tag == "select":
        return "select"
    if element.tag == "textarea":
        return "textarea"
    if element.tag != "input":
        return None
    input_type = (element.get("type") or "text").lower()
    if input_type in ("text", "password"):
        return "text"
    if input_type in ("radio", "checkbox"):
        return input_type
    return None


def _option_label(element: Any) -> str:
    if element.getparent() is not None and element.getparent().tag == "label":
        label = _normalized_text(element.getparent().text_content())
        if label:
            return label
    label = _normalized_text(element.tail or "")
    return label or (element.get("value") or "")


def _options(elements: list[Any], control_type: FieldControlType) -> tuple[FieldOption, ...]:
    if control_type == "select":
        return tuple(
            FieldOption(
                value=option.get("value") or "",
                label=_normalized_text(option.text_content()),
            )
            for option in elements[0].xpath("./option")
            if option.get("value")
        )
    if control_type in ("radio", "checkbox"):
        return tuple(
            FieldOption(value=element.get("value") or "", label=_option_label(element))
            for element in elements
        )
    return ()


def _default_value(
    elements: list[Any], control_type: FieldControlType
) -> PlatformFieldValue | None:
    if control_type == "select":
        options = elements[0].xpath("./option")
        selected = elements[0].xpath("./option[@selected]")
        option = selected[0] if selected else (options[0] if options else None)
        value = option.get("value") if option is not None else None
        return value or None
    if control_type == "textarea":
        value = elements[0].text or ""
        return value or None
    if control_type == "text":
        value = elements[0].get("value") or ""
        return value or None
    checked = [
        element.get("value") or ""
        for element in elements
        if element.get("checked") is not None
    ]
    if control_type == "checkbox":
        return checked or None
    return checked[0] if checked else None


def _image_slots(form: Any, source: str) -> tuple[int, ...]:
    slots = {
        int(match.group(1))
        for element in form.xpath(".//input[@type='file'][@name]")
        if (match := _IMAGE_INPUT_PATTERN.fullmatch(element.get("name") or ""))
    }
    if not slots:
        return ()

    limit_match = _IMAGE_LIMIT_PATTERN.search(source)
    if limit_match:
        slots.update(range(1, int(limit_match.group(1)) + 1))
    return tuple(sorted(slots))


def _is_login_page(document: Any) -> bool:
    page_text = _normalized_text(document.text_content())
    return "此操作需要先登录" in page_text or bool(
        document.xpath("//form[contains(@action, 'login')]")
    )


def _parse_form(raw_html: bytes) -> _ParsedForm:
    decoded = raw_html.decode("gb18030", errors="replace")
    document = lxml_html.fromstring(decoded)
    if _is_login_page(document):
        raise LoginExpiredError("Lieju 登录态已过期")

    forms = document.xpath("//form[contains(@action, 'action=postnew')]")
    if not forms:
        raise UpstreamPublishError("Lieju 未返回发布表单")
    form = forms[0]
    captcha_buttons = form.xpath(".//*[@id='TencentCaptcha']")
    captcha_inputs = form.xpath(".//input[@name]")
    captcha_fields = tuple(
        (element.get("name"), element.get("value") or "")
        for element in captcha_inputs
        if element.get("name") in _CAPTCHA_FIELDS
    )
    captcha_required = bool(captcha_buttons or captcha_fields)
    captcha_button = captcha_buttons[0] if captcha_buttons else None

    hidden_fields = tuple(
        (element.get("name"), element.get("value") or "")
        for element in form.xpath(".//input[@type='hidden'][@name]")
        if element.get("name") not in _CAPTCHA_FIELDS
    )
    submit_fields = tuple(
        (element.get("name"), element.get("value") or "")
        for element in form.xpath(".//input[@type='submit'][@name]")
    )

    controls: list[_ParsedControl] = []
    rows = form.xpath(f".//div[{_has_class('in')}]")
    for row in rows:
        label_nodes = row.xpath(f"./div[{_has_class('m1')}]")
        raw_label = _normalized_text(label_nodes[0].text_content()) if label_nodes else ""
        row_required = raw_label.startswith("*")

        grouped: OrderedDict[str, list[Any]] = OrderedDict()
        for element in row.xpath(".//*[@name]"):
            if _control_type(element) is None:
                continue
            grouped.setdefault(element.get("name"), []).append(element)

        for raw_name, elements in grouped.items():
            first = elements[0]
            control_type = _control_type(first)
            if control_type is None:
                continue
            datatype = first.get("datatype")
            required = row_required and (bool(datatype) or len(grouped) == 1)
            key = _field_key(raw_name)
            requirement = PublishFieldRequirement(
                key=key,
                label=_label_text(raw_label, key),
                control_type=control_type,
                required=required,
                multiple=control_type == "checkbox",
                default=_default_value(elements, control_type),
                options=_options(elements, control_type),
                validation=datatype,
                validation_message=first.get("errormsg") or first.get("nullmsg"),
            )
            controls.append(_ParsedControl(raw_name=raw_name, requirement=requirement))

    return _ParsedForm(
        hidden_fields=hidden_fields,
        controls=tuple(controls),
        submit_fields=submit_fields,
        image_slots=_image_slots(form, decoded),
        captcha_required=captcha_required,
        captcha_fields=captcha_fields,
        captcha_appid=(captcha_button.get("data-appid") if captcha_button is not None else None),
        captcha_callback=(
            captcha_button.get("data-cbfn") if captcha_button is not None else None
        ),
    )


def _content_html(content: str, content_type: ContentType) -> str:
    return (
        markdown.markdown(content, extensions=["extra", "sane_lists"], output_format="html5")
        if content_type is ContentType.MARKDOWN
        else content
    )


def _image_urls(content: str, content_type: ContentType) -> tuple[str, ...]:
    document = lxml_html.fragment_fromstring(
        _content_html(content, content_type), create_parent="div"
    )
    urls = [str(url).strip() for url in document.xpath(".//img[@src]/@src")]
    for match in _BARE_URL_PATTERN.finditer(content):
        url = unescape(match.group(0).rstrip(".,;:!?)]}"))
        if urlparse(url).path.lower().endswith(_IMAGE_PATH_SUFFIXES):
            urls.append(url)
    return tuple(dict.fromkeys(url for url in urls if url))


def _plain_text(content: str, content_type: ContentType) -> str:
    source = _content_html(content, content_type)
    fragment = lxml_html.fragment_fromstring(source, create_parent="div")
    for line_break in fragment.xpath(".//br"):
        line_break.tail = "\n" + (line_break.tail or "")
    for block in fragment.xpath(".//" + " | .//".join(_BLOCK_TAGS)):
        block.tail = "\n" + (block.tail or "")
    lines = [line.strip() for line in fragment.text_content().splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    return "\n".join(normalized).strip()


def _is_blank(value: PlatformFieldValue | None) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return not value or any(not item.strip() for item in value)
    return not value.strip()


def _validate_control_value(
    requirement: PublishFieldRequirement, value: PlatformFieldValue
) -> str | None:
    if requirement.multiple:
        if not isinstance(value, list):
            return "必须是字符串数组"
        values = value
    else:
        if not isinstance(value, str):
            return "必须是字符串"
        values = [value]

    allowed = {option.value for option in requirement.options}
    if allowed:
        invalid = [item for item in values if item not in allowed]
        if invalid:
            return f"选项无效：{', '.join(invalid)}"

    datatype = requirement.validation or ""
    length_match = _LENGTH_DATATYPE_PATTERN.fullmatch(datatype)
    if length_match:
        minimum, maximum = map(int, length_match.groups())
        if any(not minimum <= len(item) <= maximum for item in values):
            return (
                requirement.validation_message
                or f"长度必须为 {minimum}~{maximum} 个字符"
            )
    return None


def _payload_fields(
    form: _ParsedForm,
    *,
    title: str,
    content: str,
    platform_fields: Mapping[str, PlatformFieldValue],
    captcha_fields: tuple[tuple[str, str], ...] = (),
) -> list[tuple[str, str]]:
    public_by_key = {control.requirement.key: control for control in form.public_controls}
    unknown_fields = sorted(set(platform_fields) - set(public_by_key))
    missing_fields: list[str] = []
    invalid_fields: dict[str, str] = {}
    values: list[tuple[str, str]] = list(form.hidden_fields)

    for control in form.controls:
        requirement = control.requirement
        key = requirement.key
        value: PlatformFieldValue | None
        if key == "title":
            value = title
        elif key == "content":
            value = content
        elif key in platform_fields:
            value = platform_fields[key]
        elif key in _INTERNAL_FIELDS:
            value = requirement.default
        elif control.raw_name in _PROMOTION_FIELDS:
            continue
        else:
            value = requirement.default

        if _is_blank(value):
            if requirement.required:
                missing_fields.append(key)
            continue
        assert value is not None
        error = _validate_control_value(requirement, value)
        if error:
            invalid_fields[key] = error
            continue
        if isinstance(value, list):
            values.extend((control.raw_name, item) for item in value)
        else:
            values.append((control.raw_name, value))

    if missing_fields or unknown_fields or invalid_fields:
        raise PlatformFieldsValidationError(
            "Lieju 发布参数与分类表单不匹配",
            missing_fields=sorted(set(missing_fields)),
            unknown_fields=unknown_fields,
            invalid_fields=invalid_fields,
        )
    values.extend(captcha_fields)
    values.extend(form.submit_fields)
    return values


def _image_validation_error(message: str) -> PlatformFieldsValidationError:
    return PlatformFieldsValidationError(
        "Lieju 图片参数无效", invalid_fields={"content": message}
    )


def _validate_image_url(url: str, position: int) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _image_validation_error(f"第 {position} 张图片地址必须使用 HTTP 或 HTTPS")
    if parsed.username or parsed.password:
        raise _image_validation_error(f"第 {position} 张图片地址不能包含账号信息")


def _resized_image(image: Image.Image, scale: float) -> Image.Image:
    if scale == 1:
        return image.copy()
    return image.resize(
        (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        ),
        resample=Image.Resampling.LANCZOS,
    )


def _encode_jpeg(image: Image.Image, *, quality: int, scale: float) -> bytes:
    resized = _resized_image(image, scale)
    if "A" in resized.getbands():
        background = Image.new("RGB", resized.size, "white")
        background.paste(resized, mask=resized.getchannel("A"))
        resized = background
    elif resized.mode != "RGB":
        resized = resized.convert("RGB")

    buffer = BytesIO()
    resized.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


def _encode_png(image: Image.Image, *, colors: int, scale: float) -> bytes:
    source = image.convert("RGBA") if "A" in image.getbands() else image.convert("RGB")
    resized = _resized_image(source, scale)
    if resized.mode == "RGBA":
        palette = resized.quantize(
            colors=colors,
            method=Image.Quantize.FASTOCTREE,
        )
    else:
        palette = resized.quantize(
            colors=colors,
            method=Image.Quantize.MEDIANCUT,
        )

    buffer = BytesIO()
    palette.save(buffer, format="PNG", optimize=True, compress_level=9)
    return buffer.getvalue()


def _gif_frame(frame: Image.Image, *, colors: int, scale: float) -> Image.Image:
    resized = _resized_image(frame.convert("RGBA"), scale)
    return resized.convert(
        "P",
        palette=Image.Palette.ADAPTIVE,
        colors=colors,
    )


def _encode_gif(image: Image.Image, *, colors: int, scale: float) -> bytes:
    frames = [
        _gif_frame(frame, colors=colors, scale=scale)
        for frame in ImageSequence.Iterator(image)
    ]
    if not frames:
        raise ValueError("GIF does not contain a frame")

    buffer = BytesIO()
    save_options: dict[str, Any] = {"format": "GIF", "optimize": True}
    if len(frames) > 1:
        save_options.update(
            {
                "save_all": True,
                "append_images": frames[1:],
                "duration": [
                    frame.info.get("duration", image.info.get("duration", 0))
                    for frame in ImageSequence.Iterator(image)
                ],
                "loop": image.info.get("loop", 0),
            }
        )
    frames[0].save(buffer, **save_options)
    return buffer.getvalue()


def _compressed_image(
    content: bytes,
    *,
    content_type: str,
    position: int,
) -> bytes:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > _MAX_IMAGE_PIXELS:
                raise _image_validation_error(
                    f"第 {position} 张图片分辨率不能超过 {_MAX_IMAGE_PIXELS:,} 像素"
                )
            image.load()

            if content_type == "image/jpeg":
                for scale in _IMAGE_SCALE_STEPS:
                    for quality in _JPEG_QUALITY_STEPS:
                        encoded = _encode_jpeg(image, quality=quality, scale=scale)
                        if len(encoded) < _MAX_IMAGE_BYTES:
                            return encoded
            elif content_type == "image/png":
                for scale in _IMAGE_SCALE_STEPS:
                    for colors in _PALETTE_COLOR_STEPS:
                        encoded = _encode_png(image, colors=colors, scale=scale)
                        if len(encoded) < _MAX_IMAGE_BYTES:
                            return encoded
            else:
                for scale in _IMAGE_SCALE_STEPS:
                    for colors in _PALETTE_COLOR_STEPS:
                        encoded = _encode_gif(image, colors=colors, scale=scale)
                        if len(encoded) < _MAX_IMAGE_BYTES:
                            return encoded
    except PlatformFieldsValidationError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise _image_validation_error(
            f"第 {position} 张图片无法读取或压缩"
        ) from error

    raise _image_validation_error(f"第 {position} 张图片压缩后仍超过 1MB")


async def _download_image(
    requester: ArticlePublisher,
    client: httpx.AsyncClient,
    *,
    url: str,
    slot: int,
    position: int,
) -> _DownloadedImage:
    _validate_image_url(url, position)
    response = await requester.request("GET", url, _client=client)
    response.raise_for_status()
    _validate_image_url(str(response.url), position)
    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    image_type = _IMAGE_CONTENT_TYPES.get(media_type)
    if image_type is None:
        raise _image_validation_error(f"第 {position} 张图片必须是 JPG、PNG 或 GIF 格式")

    content_length = response.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > _MAX_SOURCE_IMAGE_BYTES:
        raise _image_validation_error(f"第 {position} 张图片原始文件不能超过 20MB")

    content = response.content
    if len(content) > _MAX_SOURCE_IMAGE_BYTES:
        raise _image_validation_error(f"第 {position} 张图片原始文件不能超过 20MB")
    if not content:
        raise _image_validation_error(f"第 {position} 张图片为空")
    extension, content_type = image_type
    if len(content) >= _MAX_IMAGE_BYTES:
        content = _compressed_image(
            content,
            content_type=content_type,
            position=position,
        )
    return _DownloadedImage(
        slot=slot,
        filename=f"image-{slot}.{extension}",
        content=content,
        content_type=content_type,
    )


def _add_image_fields(
    fields: list[tuple[str, str]], images: tuple[_DownloadedImage, ...]
) -> list[tuple[str, str]]:
    values = list(fields)
    names = {name for name, _ in values}
    for image in images:
        for name, value in (
            (f"photodb[{image.slot}]", ""),
            (f"ftype[{image.slot}]", "in"),
        ):
            if name not in names:
                values.append((name, value))
                names.add(name)
    return values


def _multipart_parts(
    fields: list[tuple[str, str]], images: tuple[_DownloadedImage, ...] = ()
) -> list[tuple[str, tuple[Any, ...]]]:
    try:
        parts: list[tuple[str, tuple[Any, ...]]] = [
            (name, (None, value.encode("gb18030"))) for name, value in fields
        ]
    except UnicodeEncodeError as error:
        raise PlatformFieldsValidationError(
            "Lieju 发布内容包含 GB18030 无法编码的字符",
            invalid_fields={"content": "包含平台不支持的字符"},
        ) from error
    parts.extend(
        (
            f"local_file{image.slot}",
            (image.filename, image.content, image.content_type),
        )
        for image in images
    )
    return parts


def _article_url(raw_html: bytes, response_url: str) -> str | None:
    document = lxml_html.fromstring(raw_html.decode("gb18030", errors="replace"))
    for href in document.xpath("//a[@href]/@href"):
        candidate = urljoin(response_url, str(href))
        parsed = urlparse(candidate)
        if (
            parsed.hostname
            and parsed.hostname.endswith(".lieju.com")
            and parsed.hostname not in {"post.lieju.com", "www.lieju.com", "image.lieju.com"}
            and _ARTICLE_PATH_PATTERN.fullmatch(parsed.path)
        ):
            return candidate
    parsed_response = urlparse(response_url)
    if (
        parsed_response.hostname
        and parsed_response.hostname.endswith(".lieju.com")
        and parsed_response.hostname not in {"post.lieju.com", "www.lieju.com"}
        and _ARTICLE_PATH_PATTERN.fullmatch(parsed_response.path)
    ):
        return response_url
    return None


def _confirmed_result(raw_html: bytes, response_url: str) -> str | None:
    document = lxml_html.fromstring(raw_html.decode("gb18030", errors="replace"))
    text = _normalized_text(document.text_content())
    article_url = _article_url(raw_html, response_url)
    if "发布成功" in text or article_url:
        return article_url
    excerpt = text[:240] or "空响应"
    raise UpstreamPublishError(f"Lieju 未确认发布成功：{excerpt}")


class LiejuPublisher(ArticlePublisher):
    platform = "lieju"

    def __init__(
        self,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        captcha_solver: LiejuCaptchaSolver | None = None,
        image_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(client_factory=client_factory)
        self._image_client_factory = image_client_factory or httpx.AsyncClient
        settings = get_settings()
        if captcha_solver is not None:
            self._captcha_solver = captcha_solver
        else:
            self._captcha_solver = LiejuPurePythonCaptchaFlow.from_settings(settings)
        self._timeout = settings.request_timeout_seconds

    @staticmethod
    def _category_url(category: str, category_value: str) -> str:
        match = _CATEGORY_VALUE_PATTERN.fullmatch(category_value.strip())
        if not match:
            raise PublisherConfigurationError(
                f"Lieju 分类 {category} 必须配置为 city_id/fid"
            )
        city_id, fid = match.groups()
        return f"{BASE_URL}/{city_id}/{fid}"

    @staticmethod
    def _headers(credentials: Credentials, category_url: str) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": credentials.cookie_header,
            "Origin": BASE_URL,
            "Referer": category_url,
            "User-Agent": USER_AGENT,
        }

    async def _load_form(
        self,
        client: httpx.AsyncClient,
        category_url: str,
    ) -> _ParsedForm:
        response = await self.request("GET", category_url, _client=client)
        response.raise_for_status()
        return _parse_form(response.content)

    async def _download_images(
        self,
        image_urls: tuple[str, ...],
        image_slots: tuple[int, ...],
    ) -> tuple[_DownloadedImage, ...]:
        if not image_urls:
            return ()
        if not image_slots:
            raise _image_validation_error("当前分类不支持上传图片")
        if len(image_urls) > len(image_slots):
            raise _image_validation_error(
                f"当前账号最多可上传 {len(image_slots)} 张图片"
            )

        try:
            async with self._image_client_factory(
                headers={
                    "Accept": "image/jpeg,image/png,image/gif,*/*;q=0.8",
                    "User-Agent": USER_AGENT,
                },
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                return tuple(
                    [
                        await _download_image(
                            self,
                            client,
                            url=url,
                            slot=slot,
                            position=position,
                        )
                        for position, (url, slot) in enumerate(
                            zip(image_urls, image_slots), start=1
                        )
                    ]
                )
        except httpx.HTTPError as error:
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            raise UpstreamPublishError("Lieju 图片下载失败", status) from error

    async def get_requirements(
        self,
        *,
        category: str,
        category_value: str,
        credentials: Credentials,
    ) -> PublishRequirements:
        category_url = self._category_url(category, category_value)
        try:
            async with self._client_factory(
                headers=self._headers(credentials, category_url),
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                form = await self._load_form(client, category_url)
        except httpx.HTTPError as error:
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            raise UpstreamPublishError("Lieju 发布表单请求失败", status) from error
        return PublishRequirements(
            publishable=(
                not form.captcha_required
                or form.captcha_satisfied
                or self._captcha_solver is not None
            ),
            captcha_required=form.captcha_required and not form.captcha_satisfied,
            fields=tuple(control.requirement for control in form.public_controls),
        )

    async def publish_article(
        self,
        *,
        title: str,
        category: str,
        category_value: str,
        content: str,
        content_type: ContentType,
        credentials: Credentials,
        platform_fields: Mapping[str, PlatformFieldValue],
    ) -> PublishResult:
        category_url = self._category_url(category, category_value)
        try:
            async with self._client_factory(
                headers=self._headers(credentials, category_url),
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                form = await self._load_form(client, category_url)
                image_urls = _image_urls(content, content_type)
                if image_urls and len(image_urls) > len(form.image_slots):
                    if form.image_slots:
                        raise _image_validation_error(
                            f"当前账号最多可上传 {len(form.image_slots)} 张图片"
                        )
                    raise _image_validation_error("当前分类不支持上传图片")
                captcha_fields = form.captcha_fields if form.captcha_satisfied else ()
                if form.captcha_required and not form.captcha_satisfied:
                    if self._captcha_solver is None:
                        raise CaptchaRequiredError(
                            "Lieju 当前发布表单要求腾讯验证码，"
                            "请配置 LIEJU_TDC_DYNAMIC、TDC 画像模板和完整的 "
                            "LIEJU_CHAOJIYING_*"
                        )
                    captcha = await self._captcha_solver.solve(
                        category_url=category_url,
                        form=form,
                    )
                    captcha_fields = (
                        ("atc_yzm", "1"),
                        ("postdb[ticket]", captcha.ticket),
                        ("postdb[randstr]", captcha.randstr),
                    )
                images = await self._download_images(image_urls, form.image_slots)
                fields = _payload_fields(
                    form,
                    title=title,
                    content=_plain_text(content, content_type),
                    platform_fields=platform_fields,
                    captcha_fields=captcha_fields,
                )
                fields = _add_image_fields(fields, images)
                response = await self.request(
                    "POST",
                    f"{category_url}?action=postnew",
                    files=_multipart_parts(fields, images),
                    _client=client,
                )
                response.raise_for_status()
                document = lxml_html.fromstring(
                    response.content.decode("gb18030", errors="replace")
                )
                if _is_login_page(document):
                    raise LoginExpiredError("Lieju 登录态已过期")
        except httpx.HTTPError as error:
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            raise UpstreamPublishError("Lieju 发布请求失败", status) from error

        return PublishResult(
            success=True,
            http_status=response.status_code,
            article_url=_confirmed_result(response.content, str(response.url)),
            message="Lieju 发布成功",
        )
