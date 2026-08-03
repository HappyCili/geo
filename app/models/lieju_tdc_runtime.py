"""Extract the session-specific Tencent TDC XTEA key without a browser."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
from collections.abc import Mapping
from struct import pack, unpack
from urllib.parse import unquote


_UINT32_MASK = 0xFFFFFFFF
_XTEA_DELTA = 0x9E3779B9
_PROFILE_CACHE: dict[str, bytes] = {}

_DISPATCH_PATTERN = re.compile(
    r"switch\((_[A-Za-z0-9]+)\[\+\+(_[A-Za-z0-9]+)\]\)\{"
)

_RUNTIME_PRELUDE = r"""
const __b64chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";
function __btoa(input) {
  let str = String(input), output = "";
  for (let block = 0, charCode, idx = 0, map = __b64chars;
       str.charAt(idx | 0) || (map = "=", idx % 1);
       output += map.charAt(63 & block >> 8 - idx % 1 * 8)) {
    charCode = str.charCodeAt(idx += 3 / 4);
    if (charCode > 255) throw new TypeError("btoa");
    block = block << 8 | charCode;
  }
  return output;
}
function __atob(input) {
  let str = String(input).replace(/=+$/, ""), output = "";
  if (str.length % 4 === 1) throw new TypeError("atob");
  for (let bc = 0, bs, buffer, idx = 0; buffer = str.charAt(idx++);) {
    buffer = __b64chars.indexOf(buffer);
    if (~buffer && (bs = bc % 4 ? bs * 64 + buffer : buffer, bc++ % 4)) {
      output += String.fromCharCode(255 & bs >> (-2 * bc & 6));
    }
  }
  return output;
}
const console = {log() {}, warn() {}, error() {}, info() {}};
function storage() {
  const values = {};
  return {
    get length() { return Object.keys(values).length; },
    clear() { for (const key of Object.keys(values)) delete values[key]; },
    getItem(key) {
      key = String(key);
      return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null;
    },
    key(index) { return Object.keys(values)[index] || null; },
    removeItem(key) { delete values[String(key)]; },
    setItem(key, value) { values[String(key)] = String(value); },
  };
}
function element(tagName) {
  const name = String(tagName).toUpperCase();
  const value = {
    nodeName: name,
    style: {},
    children: [],
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {},
    addEventListener() {},
    removeEventListener() {},
    getAttribute() { return null; },
    setAttribute() {},
    getBoundingClientRect() { return {left: 0, top: 0, width: 340, height: 195}; },
    matches() { return false; },
  };
  if (name === "CANVAS") {
    value.width = 300;
    value.height = 150;
    value.getContext = () => ({
      canvas: value,
      beginPath() {}, closePath() {}, fill() {}, fillRect() {}, fillText() {},
      arc() {}, rect() {}, stroke() {},
      measureText() { return {width: 10}; },
      getImageData() { return {data: new Uint8ClampedArray(4)}; },
      createLinearGradient() { return {addColorStop() {}}; },
      createRadialGradient() { return {addColorStop() {}}; },
    });
    value.toDataURL = () => "data:image/png;base64,";
  }
  if (name === "AUDIO" || name === "VIDEO") value.canPlayType = () => "";
  return value;
}
const document = {
  body: element("body"),
  documentElement: element("html"),
  cookie: "",
  currentScript: null,
  hidden: false,
  visibilityState: "visible",
  referrer: "",
  createElement: element,
  createRange() { return {}; },
  addEventListener() {},
  removeEventListener() {},
  getElementById() { return null; },
  getElementsByTagName() { return []; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
const navigator = {
  appCodeName: "Mozilla",
  appName: "Netscape",
  appVersion: "5.0",
  cookieEnabled: true,
  deviceMemory: 8,
  doNotTrack: null,
  hardwareConcurrency: 8,
  language: "zh-CN",
  languages: ["zh-CN", "zh"],
  maxTouchPoints: 0,
  onLine: true,
  platform: "MacIntel",
  product: "Gecko",
  productSub: "20030107",
  userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
    "AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36",
  vendor: "Google Inc.",
  vendorSub: "",
  plugins: [],
  mimeTypes: [],
  webdriver: false,
  storage: {},
};
const fixedNow = 1785380644460;
const timerQueue = [];
class FixedDate extends Date {
  constructor(...args) { super(...(args.length ? args : [fixedNow])); }
  static now() { return fixedNow; }
}
const sandbox = {
  console,
  Date: FixedDate,
  Math, JSON, Array, Object, String, Number, Boolean, RegExp,
  Error, TypeError, RangeError, Promise, Symbol, Map, Set, WeakMap,
  Uint8Array, Uint8ClampedArray, Int8Array, ArrayBuffer, DataView,
  parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
  escape, unescape, atob: __atob, btoa: __btoa,
  document, navigator,
  screen: {
    width: 1470, height: 956, availWidth: 1470, availHeight: 846,
    colorDepth: 30, pixelDepth: 30,
    orientation: {type: "landscape-primary", angle: 0},
  },
  location: {
    href: "https://captcha.gtimg.com/static/template/drag_ele.a48cc4fb.html",
    protocol: "https:", host: "captcha.gtimg.com", hostname: "captcha.gtimg.com",
    pathname: "/static/template/drag_ele.a48cc4fb.html",
    search: "", hash: "", origin: "https://captcha.gtimg.com",
  },
  history: {length: 2, pushState() {}, replaceState() {}},
  localStorage: storage(),
  sessionStorage: storage(),
  performance: {
    now: () => 14,
    timeOrigin: fixedNow,
    timing: {navigationStart: fixedNow - 100},
    getEntries: () => [],
    getEntriesByType: () => [],
  },
  crypto: {
    getRandomValues(values) {
      for (let index = 0; index < values.length; index += 1) {
        values[index] = (index * 31 + 7) & 255;
      }
      return values;
    },
  },
  addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
  getComputedStyle: () => ({}),
  matchMedia: () => ({matches: false, addListener() {}, removeListener() {}}),
  requestAnimationFrame: () => 1, cancelAnimationFrame() {},
  setTimeout: (callback) => {
    if (typeof callback === "function") timerQueue.push(callback);
    return timerQueue.length;
  },
  clearTimeout() {}, setInterval: () => 1, clearInterval() {},
  innerWidth: 360, innerHeight: 360, outerWidth: 1470, outerHeight: 956,
  devicePixelRatio: 2, isSecureContext: true,
  TCaptchaReferrer: "https://post.lieju.com/95/111",
};
Object.assign(globalThis, sandbox);
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.top = globalThis;
globalThis.parent = globalThis;
globalThis.__tdcActiveStart = null;
globalThis.__tdcCandidateWords = {};
globalThis.__tdcVmStepCount = 0;
function __tdcFindPair(value, depth, seen) {
  if (depth > 4 || value == null ||
      (typeof value !== "object" && typeof value !== "function")) return false;
  if (seen.indexOf(value) >= 0) return false;
  seen.push(value);
  if (Array.isArray(value) && value.length >= 2 &&
      typeof value[0] === "number" && typeof value[1] === "number") return true;
  for (const key of Object.keys(value).slice(0, 30)) {
    if (__tdcFindPair(value[key], depth + 1, seen)) return true;
  }
  return false;
}
function __tdcNumericLeaves(value, depth, seen) {
  if (depth > 6 || value == null) return 0;
  if (typeof value === "number") return 1;
  if (typeof value !== "object" && typeof value !== "function") return 0;
  if (seen.indexOf(value) >= 0) return 0;
  seen.push(value);
  let count = 0;
  for (const key of Object.keys(value).slice(0, 40)) {
    count += __tdcNumericLeaves(value[key], depth + 1, seen);
  }
  return count;
}
globalThis.__tdcVmEnter = (start, registers) => {
  if (__tdcActiveStart === null && __tdcFindPair(registers[4], 0, []) &&
      __tdcNumericLeaves(registers[2], 0, []) === 4) {
    __tdcActiveStart = start;
  }
};
globalThis.__tdcVmStep = (start, pc, opcode, registers) => {
  if (start !== __tdcActiveStart || __tdcVmStepCount >= 4000) return;
  __tdcVmStepCount += 1;
  for (let index = 7; index < registers.length; index += 1) {
    const value = registers[index];
    if (Number.isInteger(value) && value >= 0 && value <= 4294967295) {
      const word = String.fromCharCode(
        value & 255, (value >>> 8) & 255,
        (value >>> 16) & 255, (value >>> 24) & 255
      );
      if (/^[A-Za-z0-9_-]{4}$/.test(word)) __tdcCandidateWords[word] = value;
    }
  }
};
"""


class TdcRuntimeError(ValueError):
    pass


def _instrument_source(source: str) -> str:
    dispatch = _DISPATCH_PATTERN.search(source)
    if dispatch is None:
        raise TdcRuntimeError("TDC VM dispatch loop was not found")
    code_name, pc_name = dispatch.groups()
    before_dispatch = source[: dispatch.start()]
    pc_index = before_dispatch.rfind(f"{pc_name}=")
    if pc_index < 0:
        raise TdcRuntimeError("TDC VM program counter assignment was not found")
    pc_assignment = re.match(
        rf"{re.escape(pc_name)}=(_[A-Za-z0-9]+),",
        before_dispatch[pc_index:],
    )
    if pc_assignment is None:
        raise TdcRuntimeError("TDC VM start offset was not found")
    start_name = pc_assignment.group(1)
    declaration_start = before_dispatch.rfind("var ", 0, pc_index)
    if declaration_start < 0:
        raise TdcRuntimeError("TDC VM register declaration was not found")
    declaration = before_dispatch[declaration_start : dispatch.start()]
    register_match = re.search(
        rf"(_[A-Za-z0-9]+)=\[[^;]*?{re.escape(code_name)},0x0\]",
        declaration,
    )
    if register_match is None:
        raise TdcRuntimeError("TDC VM register array was not found")
    registers_name = register_match.group(1)

    switch = f"switch({code_name}[++{pc_name}]){{"
    loop = f"while(!![]){{try{{while(!![]){{{switch}"
    if loop not in source:
        raise TdcRuntimeError("TDC VM execution loop was not found")
    replacement = (
        "globalThis.__tdcVmEnter&&globalThis.__tdcVmEnter("
        f"{start_name},{registers_name});"
        "while(!![]){try{while(!![]){"
        f"var __tdcOp={code_name}[++{pc_name}];"
        "if(globalThis.__tdcVmStep)globalThis.__tdcVmStep("
        f"{start_name},{pc_name},__tdcOp,{registers_name});"
        "switch(__tdcOp){"
    )
    return source.replace(loop, replacement, 1)


def _xtea_decrypt_block(block: bytes, key: bytes) -> bytes:
    value_0, value_1 = unpack("<2I", block)
    key_words = unpack("<4I", key)
    total = (_XTEA_DELTA * 32) & _UINT32_MASK
    for _ in range(32):
        mix = (
            (((value_0 << 4) & _UINT32_MASK) ^ (value_0 >> 5)) + value_0
        ) & _UINT32_MASK
        value_1 = (
            value_1
            - (mix ^ ((total + key_words[(total >> 11) & 3]) & _UINT32_MASK))
        ) & _UINT32_MASK
        total = (total - _XTEA_DELTA) & _UINT32_MASK
        mix = (
            (((value_1 << 4) & _UINT32_MASK) ^ (value_1 >> 5)) + value_1
        ) & _UINT32_MASK
        value_0 = (
            value_0 - (mix ^ ((total + key_words[total & 3]) & _UINT32_MASK))
        ) & _UINT32_MASK
    return pack("<2I", value_0, value_1)


def _candidate_key(ciphertext: bytes, words: Mapping[str, int]) -> bytes:
    candidates = tuple(word.encode("ascii") for word in words)
    if len(candidates) < 4:
        raise TdcRuntimeError("TDC VM did not expose enough key candidates")
    matches: list[bytes] = []
    for parts in itertools.product(candidates, repeat=4):
        key = b"".join(parts)
        if _xtea_decrypt_block(ciphertext[:8], key).startswith(b'{"cd":[{'):
            matches.append(key)
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise TdcRuntimeError(
            f"TDC XTEA key selection returned {len(matches)} candidates"
        )
    return matches[0]


def _pack_tdc_text(value: str) -> bytes:
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


def _new_context(source: str, *, timeout_seconds: float) -> object:
    try:
        import quickjs
    except ImportError as error:
        raise TdcRuntimeError("quickjs dependency is not installed") from error
    context = quickjs.Context()
    context.set_memory_limit(128 * 1024 * 1024)
    context.set_time_limit(timeout_seconds)
    try:
        context.eval(_RUNTIME_PRELUDE)
        context.eval(_instrument_source(source))
        context.eval(
            "for(let round=0;round<20&&timerQueue.length;round++){"
            "const pending=timerQueue.splice(0);pending.forEach(cb=>cb())}"
        )
    except Exception as error:
        raise TdcRuntimeError("TDC script execution failed in QuickJS") from error
    return context


def _measure_padded_collect(
    source: str,
    padding_length: int,
    *,
    timeout_seconds: float,
) -> bytes:
    context = _new_context(source, timeout_seconds=timeout_seconds)
    try:
        context.eval(
            "TDC.setData({__liejuPadding: \"x\".repeat("
            f"{padding_length})}})"
        )
        encoded = context.eval("TDC.getData(true)")
        if not isinstance(encoded, str):
            raise TdcRuntimeError("TDC padding probe returned a non-string result")
        return base64.b64decode(unquote(encoded), validate=True)
    except TdcRuntimeError:
        raise
    except Exception as error:
        raise TdcRuntimeError("TDC padding probe failed") from error


def encode_tdc_payload(
    source: str,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float = 20,
) -> str:
    """Encrypt a caller-provided payload with the exact TDC VM currently loaded.

    Tencent's obfuscated runtime does not expose its encryptor as a public
    function.  The VM hook below replaces the plaintext blocks at the first
    collect-encryption invocation and lets the runtime perform its own keying
    and XTEA implementation.  No browser or external JavaScript process is
    started; QuickJS is embedded in this Python process.
    """

    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    packed = _pack_tdc_text(plaintext)
    target_blocks = [
        list(unpack("<2I", packed[offset : offset + 8]))
        for offset in range(0, len(packed), 8)
    ]
    target_length = len(packed)

    baseline = _measure_padded_collect(
        source, 0, timeout_seconds=timeout_seconds
    )
    padding_length = max(0, target_length - len(baseline))
    for _ in range(4):
        measured = _measure_padded_collect(
            source, padding_length, timeout_seconds=timeout_seconds
        )
        delta = target_length - len(measured)
        if delta == 0:
            break
        padding_length = max(0, padding_length + delta)
    else:
        raise TdcRuntimeError("TDC padding could not match the target payload size")

    target_json = json.dumps(target_blocks, separators=(",", ":"))
    first_block = target_blocks[0]
    last_error: Exception | None = None
    for prefix_offset in range(0, 16):
        context = _new_context(source, timeout_seconds=timeout_seconds)
        try:
            context.eval(
                "globalThis.__liejuTargetBlocks="
                f"{target_json};"
                "globalThis.__liejuTargetIndex=0;"
                "globalThis.__liejuTargetStart=null;"
                "globalThis.__liejuFindPair=function(value,depth,seen){"
                "if(depth>4||value==null||(typeof value!==\"object\"&&"
                "typeof value!==\"function\"))return null;"
                "seen=seen||[];if(seen.indexOf(value)>=0)return null;"
                "seen.push(value);if(typeof value[0]===\"number\"&&"
                "typeof value[1]===\"number\")"
                "return value;for(const key of Object.keys(value).slice(0,30)){"
                "const found=__liejuFindPair(value[key],depth+1,seen);"
                "if(found)return found;}return null};"
                "globalThis.__tdcVmEnter=function(start,registers){"
                "const pair=__liejuFindPair(registers[4],0,[]);"
                "if(!pair)return;"
                f"if(__liejuTargetStart===null&&(pair[0]>>>0)==={first_block[0]}){{"
                "__liejuTargetStart=start;"
                f"__liejuTargetIndex={prefix_offset};}}"
                "if(__liejuTargetStart!==null&&"
                "__liejuTargetIndex<__liejuTargetBlocks.length){"
                "pair[0]=__liejuTargetBlocks[__liejuTargetIndex][0];"
                "pair[1]=__liejuTargetBlocks[__liejuTargetIndex][1];"
                "__liejuTargetIndex++;}};"
                "globalThis.__tdcVmStep=function(){};"
            )
            context.eval(
                "TDC.setData({__liejuPadding: \"x\".repeat("
                f"{padding_length})}})"
            )
            encoded = context.eval("TDC.getData(true)")
            index = context.eval("__liejuTargetIndex")
            if not isinstance(encoded, str) or index != len(target_blocks):
                raise TdcRuntimeError(
                    "TDC VM did not visit all target encryption blocks"
                )
            ciphertext = base64.b64decode(unquote(encoded), validate=True)
            if len(ciphertext) != target_length:
                raise TdcRuntimeError(
                    "TDC VM output length does not match the target payload"
                )
            return unquote(encoded)
        except TdcRuntimeError as error:
            last_error = error
        except Exception as error:
            last_error = TdcRuntimeError("TDC payload encryption failed")
            last_error.__cause__ = error
    raise last_error or TdcRuntimeError("TDC payload encryption failed")


def extract_tdc_key(source: str, *, timeout_seconds: float = 20) -> bytes:
    """Return the XTEA key used by this exact, session-specific TDC source."""

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    cached = _PROFILE_CACHE.get(digest)
    if cached is not None:
        return cached
    try:
        import quickjs
    except ImportError as error:
        raise TdcRuntimeError("quickjs dependency is not installed") from error

    context = quickjs.Context()
    context.set_memory_limit(128 * 1024 * 1024)
    context.set_time_limit(timeout_seconds)
    try:
        context.eval(_RUNTIME_PRELUDE)
        context.eval(_instrument_source(source))
        context.eval(
            "for(let round=0;round<20&&timerQueue.length;round++){"
            "const pending=timerQueue.splice(0);pending.forEach(cb=>cb())}"
        )
        encoded_collect = context.eval("TDC.getData(true)")
        raw_words = context.eval("JSON.stringify(__tdcCandidateWords)")
    except Exception as error:
        raise TdcRuntimeError("TDC script execution failed in QuickJS") from error
    if not isinstance(encoded_collect, str) or not isinstance(raw_words, str):
        raise TdcRuntimeError("TDC QuickJS result is incomplete")
    try:
        ciphertext = base64.b64decode(unquote(encoded_collect), validate=True)
        words = json.loads(raw_words)
    except (ValueError, json.JSONDecodeError) as error:
        raise TdcRuntimeError("TDC QuickJS output is invalid") from error
    if len(ciphertext) < 8 or not isinstance(words, Mapping):
        raise TdcRuntimeError("TDC QuickJS output is incomplete")
    key = _candidate_key(ciphertext, words)
    _PROFILE_CACHE[digest] = key
    return key
