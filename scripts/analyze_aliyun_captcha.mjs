#!/usr/bin/env node

import fs from "node:fs";
import vm from "node:vm";
import asyncHooks from "node:async_hooks";
import readline from "node:readline";

const sourcePath = process.argv[2];
const dynamicSourcePath = process.argv[3];
const certifyId = process.env.ALIYUN_CERTIFY_ID || "CERTIFY_ID";
const encryptedDeviceConfig = process.env.ALIYUN_DEVICE_CONFIG;
const deviceSourcePath = process.env.ALIYUN_DEVICE_SOURCE;
const cnblogsCaptchaSourcePath = process.env.CNBLOGS_CAPTCHA_SOURCE;
const useRealHttp = process.env.ALIYUN_REAL_HTTP === "1";
const runInitProbe = process.env.ALIYUN_RUN_INIT === "1";
const diagnoseDynamicCalls = process.env.ALIYUN_DIAGNOSE_DYNAMIC_CALLS === "1";
const runDynamicSample = process.env.ALIYUN_RUN_DYNAMIC_SAMPLE === "1" || !runInitProbe;
const runChallengeProbe = process.env.ALIYUN_RUN_CHALLENGE === "1";
const simulateActivity = process.env.ALIYUN_SIMULATE_ACTIVITY === "1";
const challengeTimeoutMs = Number.parseInt(
  process.env.ALIYUN_CHALLENGE_TIMEOUT_MS || "8000",
  10,
);
const useHttpBridge = process.env.ALIYUN_HTTP_BRIDGE === "1";
const useDeviceFixture = process.env.ALIYUN_DEVICE_FIXTURE === "1";
const assertFingerprintShims = process.env.ALIYUN_ASSERT_FINGERPRINT_SHIMS === "1";
const outputMode = process.env.ALIYUN_OUTPUT_MODE || "analysis";
const captchaPrefix = process.env.ALIYUN_PREFIX || "1p4ezn";
const sceneId = process.env.ALIYUN_SCENE_ID || "ivn46p64";
const captchaRegion = process.env.ALIYUN_REGION || "cn";
const captchaPageUrl = process.env.ALIYUN_PAGE_URL
  || "https://account.cnblogs.com/signin";
const captchaPageLocation = new URL(captchaPageUrl);
const captchaOrigin = captchaPageLocation.origin;
const browserMajorVersion = process.env.ALIYUN_BROWSER_MAJOR_VERSION || "152";
const userAgent = process.env.ALIYUN_USER_AGENT || (
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
  + "AppleWebKit/537.36 (KHTML, like Gecko) "
  + `Chrome/${browserMajorVersion}.0.0.0 Safari/537.36`
);
if (!sourcePath) {
  throw new Error("usage: analyze_aliyun_captcha.mjs ALIYUN_CAPTCHA_JS");
}
if (captchaPageLocation.protocol !== "https:") {
  throw new Error("ALIYUN_PAGE_URL must use HTTPS");
}
if (!/^[A-Za-z0-9_-]{1,128}$/.test(captchaPrefix)) {
  throw new Error("ALIYUN_PREFIX has an invalid format");
}
if (!/^[A-Za-z0-9_-]{1,128}$/.test(sceneId)) {
  throw new Error("ALIYUN_SCENE_ID has an invalid format");
}
if (captchaRegion !== "cn") {
  throw new Error("only the Aliyun cn region is supported");
}
if (!/^\d{2,3}$/.test(browserMajorVersion)) {
  throw new Error("ALIYUN_BROWSER_MAJOR_VERSION has an invalid format");
}
if (!new Set(["analysis", "captcha"]).has(outputMode)) {
  throw new Error("ALIYUN_OUTPUT_MODE must be analysis or captcha");
}
if (!Number.isSafeInteger(challengeTimeoutMs) || challengeTimeoutMs < 1_000 || challengeTimeoutMs > 15_000) {
  throw new Error("ALIYUN_CHALLENGE_TIMEOUT_MS must be between 1000 and 15000");
}
if (useDeviceFixture && !deviceSourcePath) {
  throw new Error("ALIYUN_DEVICE_FIXTURE requires ALIYUN_DEVICE_SOURCE");
}

function assertAllowedAliyunUrl(input) {
  const parsed = new URL(input);
  const allowedHost = parsed.hostname === "g.alicdn.com"
    || parsed.hostname === "aliyuncs.com"
    || parsed.hostname.endsWith(".aliyuncs.com");
  if (parsed.protocol !== "https:" || !allowedHost) {
    throw new Error(`unexpected Aliyun URL: ${parsed.href}`);
  }
  return parsed;
}

let bridgeRequestId = 0;
const bridgePending = new Map();
const bridgeInterface = useHttpBridge
  ? readline.createInterface({ input: process.stdin })
  : null;
bridgeInterface?.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }
  const pending = bridgePending.get(message.id);
  if (!pending) return;
  bridgePending.delete(message.id);
  if (message.error) pending.reject(new Error(message.error));
  else pending.resolve(message);
});
bridgeInterface?.on("close", () => {
  for (const pending of bridgePending.values()) {
    pending.reject(new Error("Aliyun HTTP bridge closed"));
  }
  bridgePending.clear();
});

async function bridgeFetch(input, options = {}) {
  const parsed = assertAllowedAliyunUrl(input);
  const id = ++bridgeRequestId;
  const request = {
    id,
    method: String(options.method || "GET").toUpperCase(),
    url: parsed.href,
    headers: Object.fromEntries(new Headers(options.headers || {}).entries()),
    body: options.body == null ? "" : String(options.body),
  };
  const responsePromise = new Promise((resolve, reject) => {
    bridgePending.set(id, { reject, resolve });
  });
  process.stderr.write(`ALIYUN_HTTP_REQUEST ${JSON.stringify(request)}\n`);
  const response = await responsePromise;
  const body = Buffer.from(response.bodyBase64 || "", "base64");
  return {
    headers: new Headers(response.headers || {}),
    ok: response.status >= 200 && response.status < 300,
    status: response.status,
    async text() { return body.toString("utf8"); },
  };
}

const runtimeFetch = useHttpBridge ? bridgeFetch : fetch;
const useNetwork = useRealHttp || useHttpBridge;

const exportMarker = "if(window.AliyunCaptchaConfig&&";
const source = fs.readFileSync(sourcePath, "utf8");
if (!source.includes(exportMarker)) {
  throw new Error("AliyunCaptcha.js export marker was not found");
}

const exportCode = `window.__ALIYUN_INTERNALS={
  captchaConfig:er,
  deviceConfig:nr,
  captchaKeys:ke,
  deviceKeys:Ee,
  frontConstants:Ft,
  captchaEndpoints:et,
  deviceEndpoints:br,
  apiVersion:lt,
  signParams:Ce,
  requestAction:Ne,
  buildDeviceData:Re,
  normalizeInitResult:be,
  decryptConfig:me,
  deviceConfigSecret:de,
  aesEncrypt:we,
  aesSecret:ye,
  decryptSecret:ge,
  encodeUrl:Qe,
  timestamp:hr,
  nonce:dr,
  makeUrl:lr,
  resolveVerifyType:Sr,
  deviceState:Ie,
  captchaRequestConfig:Be
};`;

const instrumented = source
  .replace(exportMarker, exportCode + exportMarker)
  .replace(
    "if(function(){Dn.apply(this,arguments)}(),void 0===t)",
    "if(void 0===t)",
  );

const elements = new Map();
const capturedRequests = [];
const externalScripts = [];
let externalScriptLoader;
const runtimeConsoleMessages = [];
const runtimeEvents = [];
const runtimeStats = {
  canvas2d: 0,
  canvasBlobExports: 0,
  canvasDataUrls: 0,
  canvasImageReads: 0,
  canvasWebgl: 0,
  createdElements: {},
  iframeAppends: 0,
  iframeContentDocumentReads: 0,
  iframeContentWindowReads: 0,
  indexedDbDeletes: 0,
  indexedDbOpens: 0,
  rafFired: 0,
  rafScheduled: 0,
  storageClears: 0,
  storageGets: 0,
  storageKeys: 0,
  storageRemoves: 0,
  storageSets: 0,
  timersFired: 0,
  timersScheduled: 0,
};
const pendingDevicePromises = new Map();
let traceDevicePromises = false;
const asyncHook = asyncHooks.createHook({
  init(asyncId, type) {
    if (!traceDevicePromises || type !== "PROMISE") return;
    const stack = new Error().stack || "";
    if (deviceSourcePath && stack.includes(deviceSourcePath)) {
      pendingDevicePromises.set(asyncId, stack.split("\n").slice(2, 9));
    }
  },
  promiseResolve(asyncId) {
    pendingDevicePromises.delete(asyncId);
  },
});
asyncHook.enable();
const sandboxTimerHandles = new Map();
let sandboxTimerId = 1;
const sandboxSetTimeout = (callback, delay, ...args) => {
  runtimeStats.timersScheduled += 1;
  const timerId = sandboxTimerId++;
  const handle = setTimeout(() => {
    sandboxTimerHandles.delete(timerId);
    runtimeStats.timersFired += 1;
    try {
      callback(...args);
    } catch (error) {
      runtimeConsoleMessages.push(formatConsoleValue(error));
      window?.onerror?.(
        error?.message || String(error),
        "",
        0,
        0,
        error,
      );
    }
  }, delay);
  sandboxTimerHandles.set(timerId, handle);
  return timerId;
};
const sandboxSetInterval = (callback, delay, ...args) => {
  const timerId = sandboxTimerId++;
  const handle = setInterval(() => {
    try {
      callback(...args);
    } catch (error) {
      runtimeConsoleMessages.push(formatConsoleValue(error));
    }
  }, delay);
  sandboxTimerHandles.set(timerId, handle);
  return timerId;
};
const sandboxClearTimer = (timerId) => {
  const handle = sandboxTimerHandles.get(timerId);
  if (!handle) return;
  clearTimeout(handle);
  clearInterval(handle);
  sandboxTimerHandles.delete(timerId);
};
const formatConsoleValue = (value) => String(value?.stack || value)
  .split("\n")
  .filter((line) => line.length < 2_000)
  .slice(0, 20)
  .join("\n");
const formatErrorValue = (value) => {
  if (value && typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return formatConsoleValue(value);
    }
  }
  return formatConsoleValue(value);
};
const runtimeConsole = {
  debug() {},
  info() {},
  log() {},
  warn(...values) {
    runtimeConsoleMessages.push(values.map(formatConsoleValue).join(" "));
  },
  error(...values) {
    runtimeConsoleMessages.push(values.map(formatConsoleValue).join(" "));
  },
};

function makeSyntheticEvent(type, target, properties = {}) {
  return {
    bubbles: true,
    cancelable: true,
    currentTarget: target,
    defaultPrevented: false,
    isTrusted: true,
    preventDefault() { this.defaultPrevented = true; },
    stopImmediatePropagation() { this._immediatePropagationStopped = true; },
    stopPropagation() { this._propagationStopped = true; },
    target,
    timeStamp: globalThis.performance.now(),
    type,
    view: window,
    ...properties,
  };
}
function makeMouseEvent(type, target, buttons, x = 14, y = 14) {
  return makeSyntheticEvent(type, target, {
    button: 0,
    buttons,
    clientX: x,
    clientY: y,
    pageX: x,
    pageY: y,
    screenX: x + 200,
    screenY: y + 200,
    which: 1,
  });
}

const wait = (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs));
const randomInteger = (minimum, maximum) => (
  Math.floor(Math.random() * (maximum - minimum + 1)) + minimum
);

function instrumentDynamicCalls(dynamicSource) {
  if (!diagnoseDynamicCalls) return dynamicSource;
  return dynamicSource.replace(
    "p=null===h?l.apply(c,o):h[l].apply(h,o)",
    'p=null===h?l.apply(c,o):typeof h[l]==="function"?h[l].apply(h,o):window.__ALIYUN_RECORD_MISSING(h,l,o)',
  );
}

function StyleDeclaration() {
  this.cssText = "";
}
StyleDeclaration.prototype.getPropertyValue = function getPropertyValue(name) {
  return this[String(name)] || "";
};
StyleDeclaration.prototype.removeProperty = function removeProperty(name) {
  const key = String(name);
  const previous = this[key] || "";
  delete this[key];
  return previous;
};
StyleDeclaration.prototype.setProperty = function setProperty(name, value) {
  this[String(name)] = String(value);
};

function DOMTokenList(element) {
  this.element = element;
}
DOMTokenList.prototype._tokens = function tokens() {
  return String(this.element.className || "").split(/\s+/).filter(Boolean);
};
DOMTokenList.prototype._write = function write(tokens) {
  this.element.className = [...new Set(tokens)].join(" ");
};
DOMTokenList.prototype.add = function add(...tokens) {
  this._write([...this._tokens(), ...tokens.map(String)]);
};
DOMTokenList.prototype.contains = function contains(token) {
  return this._tokens().includes(String(token));
};
DOMTokenList.prototype.forEach = function forEach(callback, thisArg) {
  this._tokens().forEach(callback, thisArg);
};
DOMTokenList.prototype.item = function item(index) {
  return this._tokens()[index] ?? null;
};
DOMTokenList.prototype.remove = function remove(...tokens) {
  const removed = new Set(tokens.map(String));
  this._write(this._tokens().filter((token) => !removed.has(token)));
};
DOMTokenList.prototype.replace = function replace(oldToken, newToken) {
  const tokens = this._tokens();
  const index = tokens.indexOf(String(oldToken));
  if (index < 0) return false;
  tokens[index] = String(newToken);
  this._write(tokens);
  return true;
};
DOMTokenList.prototype.toggle = function toggle(token, force) {
  const value = String(token);
  const present = this.contains(value);
  const enabled = force === undefined ? !present : Boolean(force);
  if (enabled && !present) this.add(value);
  if (!enabled && present) this.remove(value);
  return enabled;
};
DOMTokenList.prototype[Symbol.iterator] = function iterator() {
  return this._tokens()[Symbol.iterator]();
};
Object.defineProperties(DOMTokenList.prototype, {
  length: { get() { return this._tokens().length; } },
  value: {
    get() { return String(this.element.className || ""); },
    set(value) { this.element.className = String(value); },
  },
});

function CanvasGradient() {}
CanvasGradient.prototype.addColorStop = function addColorStop() {};
function CanvasPattern() {}
CanvasPattern.prototype.setTransform = function setTransform() {};
function ImageData(dataOrWidth = 1, widthOrHeight = 1, heightValue) {
  if (dataOrWidth instanceof Uint8ClampedArray) {
    this.data = dataOrWidth;
    this.width = widthOrHeight;
    this.height = heightValue ?? Math.floor(dataOrWidth.length / 4 / widthOrHeight);
  } else {
    this.width = dataOrWidth;
    this.height = widthOrHeight;
    this.data = new Uint8ClampedArray(Math.max(0, this.width * this.height * 4));
  }
}
function TextMetrics(width = 0) {
  this.actualBoundingBoxAscent = 8;
  this.actualBoundingBoxDescent = 2;
  this.width = width;
}
function CanvasRenderingContext2D() {}

function createCanvas2DContext(canvas) {
  const pixels = new Uint8ClampedArray([17, 34, 51, 255]);
  const context = {
    canvas,
    arc() {},
    beginPath() {},
    bezierCurveTo() {},
    clearRect() {},
    closePath() {},
    createImageData(width = 1, height = 1) {
      return new ImageData(width, height);
    },
    createLinearGradient() { return new CanvasGradient(); },
    createPattern() { return new CanvasPattern(); },
    createRadialGradient() { return new CanvasGradient(); },
    drawImage() {},
    fill() {},
    fillRect() {},
    fillText() {},
    getImageData(_x = 0, _y = 0, width = 1, height = 1) {
      runtimeStats.canvasImageReads += 1;
      const data = new Uint8ClampedArray(Math.max(4, width * height * 4));
      for (let index = 0; index < data.length; index += 1) {
        data[index] = pixels[index % pixels.length];
      }
      return new ImageData(data, width, height);
    },
    isPointInPath() { return false; },
    lineTo() {},
    measureText(text = "") {
      return new TextMetrics(String(text).length * 7.25);
    },
    moveTo() {},
    putImageData() {},
    quadraticCurveTo() {},
    rect() {},
    restore() {},
    rotate() {},
    save() {},
    scale() {},
    stroke() {},
    strokeRect() {},
    strokeText() {},
    transform() {},
    translate() {},
  };
  Object.setPrototypeOf(context, CanvasRenderingContext2D.prototype);
  return new Proxy(context, {
    get(target, key) {
      if (!(key in target) && typeof key === "string") return 0;
      return Reflect.get(target, key);
    },
  });
}

function createWebGLContext(canvas) {
  const parameters = new Map([
    [0x1F00, "WebKit"],
    [0x1F01, "WebKit WebGL"],
    [0x1F02, "WebGL 1.0 (OpenGL ES 2.0 Chromium)"],
    [0x8B8C, "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)"],
    [0x0D33, 16384],
    [0x8872, 16],
  ]);
  return {
    canvas,
    ALIASED_LINE_WIDTH_RANGE: 0x846E,
    ALIASED_POINT_SIZE_RANGE: 0x846D,
    MAX_TEXTURE_SIZE: 0x0D33,
    RENDERER: 0x1F01,
    SHADING_LANGUAGE_VERSION: 0x8B8C,
    VENDOR: 0x1F00,
    VERSION: 0x1F02,
    getContextAttributes() {
      return { alpha: true, antialias: true, depth: true, stencil: false };
    },
    getExtension(name) {
      if (name === "WEBGL_debug_renderer_info") {
        return {
          UNMASKED_RENDERER_WEBGL: 0x9246,
          UNMASKED_VENDOR_WEBGL: 0x9245,
        };
      }
      return null;
    },
    getParameter(parameter) {
      if (parameter === 0x9245) return "Google Inc. (Apple)";
      if (parameter === 0x9246) return "ANGLE (Apple, ANGLE Metal Renderer)";
      if (parameter === 0x846D) return new Float32Array([1, 511]);
      if (parameter === 0x846E) return new Float32Array([1, 1]);
      return parameters.get(parameter) ?? 0;
    },
    getSupportedExtensions() {
      return ["EXT_blend_minmax", "EXT_texture_filter_anisotropic", "WEBGL_debug_renderer_info"];
    },
  };
}

function Element(tagName = "div", ownerDocument = null) {
  this.tagName = String(tagName).toUpperCase();
  this.nodeName = this.tagName;
  this.nodeType = 1;
  this.ownerDocument = ownerDocument;
  this.children = [];
  this.childNodes = this.children;
  this.className = "";
  this.attributes = [];
  this.style = new StyleDeclaration();
  this.scrollLeft = 0;
  this.scrollTop = 0;
  this.width = this.tagName === "CANVAS" ? 300 : 0;
  this.height = this.tagName === "CANVAS" ? 150 : 0;
  runtimeStats.createdElements[this.tagName] = (
    runtimeStats.createdElements[this.tagName] || 0
  ) + 1;
}
Element.prototype = {
  constructor: Element,
  addEventListener(type, listener) {
    if (listener == null) return;
    this._listeners ||= new Map();
    const key = String(type);
    this._listeners.set(key, [...(this._listeners.get(key) || []), listener]);
  },
  appendChild(child) {
    child.parentNode = this;
    child.parentElement = this;
    child.ownerDocument ||= this.ownerDocument;
    this.children.push(child);
    if (child.tagName === "IFRAME") runtimeStats.iframeAppends += 1;
    if (child.id) elements.set(`#${child.id}`, child);
    if (child.tagName === "SCRIPT" && child.src && externalScriptLoader) {
      externalScriptLoader(child.src).then(() => {
        child.onload?.({ type: "load", target: child });
      }).catch((error) => {
        try {
          child.onerror?.({ error, type: "error", target: child });
        } catch (handlerError) {
          runtimeConsoleMessages.push(formatConsoleValue(handlerError));
        }
      });
      return child;
    }
    sandboxSetTimeout(() => child.onload?.({ type: "load", target: child }), 0);
    return child;
  },
  click() { return this.dispatchEvent(makeMouseEvent("click", this, 0)); },
  cloneNode(deep = false) {
    const clone = new Element(this.tagName, this.ownerDocument);
    clone.className = this.className;
    clone.id = this.id;
    if (deep) this.children.forEach((child) => clone.appendChild(child.cloneNode(true)));
    return clone;
  },
  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches?.(selector)) return current;
      current = current.parentElement;
    }
    return null;
  },
  dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    for (const listener of [...(this._listeners?.get(event.type) || [])]) {
      if (typeof listener === "function") listener.call(this, event);
      else listener.handleEvent?.(event);
      if (event._immediatePropagationStopped) break;
    }
    const handler = this[`on${event.type}`];
    if (!event._immediatePropagationStopped && typeof handler === "function") {
      handler.call(this, event);
    }
    if (event.bubbles && !event._propagationStopped) {
      this.parentNode?.dispatchEvent?.(event);
    }
    return !event.defaultPrevented;
  },
  getBoundingClientRect() {
    const width = this.width || 360;
    const height = this.height || 40;
    return { bottom: height, height, left: 0, right: width, top: 0, width, x: 0, y: 0 };
  },
  getContext(type) {
    if (this.tagName !== "CANVAS") return null;
    if (type === "2d") {
      runtimeStats.canvas2d += 1;
      return (this._context2d ||= createCanvas2DContext(this));
    }
    if (type === "webgl" || type === "experimental-webgl" || type === "webgl2") {
      runtimeStats.canvasWebgl += 1;
      return (this._webglContext ||= createWebGLContext(this));
    }
    return null;
  },
  getElementsByTagName(tagName) {
    const upper = String(tagName).toUpperCase();
    return this.children.flatMap((child) => [
      ...(upper === "*" || child.tagName === upper ? [child] : []),
      ...(typeof child.getElementsByTagName === "function"
        ? child.getElementsByTagName(upper)
        : []),
    ]);
  },
  getAttribute(name) {
    return this[String(name)] ?? null;
  },
  hasAttribute(name) {
    return this[String(name)] !== undefined;
  },
  insertAdjacentHTML(_position, html) {
    for (const match of String(html).matchAll(/id=["']([^"']+)["']/g)) {
      const child = new Element();
      child.id = match[1];
      this.appendChild(child);
    }
  },
  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return String(this.className).split(/\s+/).includes(selector.slice(1));
    return this.tagName === selector.toUpperCase();
  },
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  querySelectorAll(selector) {
    return this.getElementsByTagName("*").filter(
      (child) => typeof child.matches === "function" && child.matches(selector),
    );
  },
  remove() { this.parentNode?.removeChild(this); },
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    for (const descendant of [child, ...child.getElementsByTagName("*")]) {
      if (descendant.id && elements.get(`#${descendant.id}`) === descendant) {
        elements.delete(`#${descendant.id}`);
      }
    }
    child.parentNode = null;
    child.parentElement = null;
    return child;
  },
  removeEventListener(type, listener) {
    const listeners = this._listeners?.get(String(type)) || [];
    this._listeners?.set(String(type), listeners.filter((item) => item !== listener));
  },
  removeAttribute(name) { delete this[String(name)]; },
  setAttribute(name, value) {
    this[name] = value;
    this.attributes.push({ name: String(name), value: String(value) });
    if (name === "id") elements.set(`#${value}`, this);
  },
  toBlob(callback, type = "image/png") {
    runtimeStats.canvasBlobExports += 1;
    callback(new Blob(["instrumented-canvas"], { type }));
  },
  toDataURL(type = "image/png") {
    runtimeStats.canvasDataUrls += 1;
    return `data:${type};base64,aW5zdHJ1bWVudGVkLWNhbnZhcw==`;
  },
};
Object.defineProperties(Element.prototype, {
  classList: { get() { return (this._classList ||= new DOMTokenList(this)); } },
  clientHeight: { get() { return this.height || 40; } },
  clientWidth: { get() { return this.width || 360; } },
  firstChild: { get() { return this.children[0] || null; } },
  innerHTML: {
    get() { return this._innerHTML || ""; },
    set(value) {
      this._innerHTML = String(value);
      for (const child of [...this.children]) this.removeChild(child);
      this.insertAdjacentHTML("beforeend", value);
    },
  },
  lastChild: { get() { return this.children[this.children.length - 1] || null; } },
  offsetHeight: { get() { return this.height || 40; } },
  offsetWidth: { get() { return this.width || 360; } },
  outerHTML: {
    get() {
      const tag = this.tagName.toLowerCase();
      const id = this.id ? ` id="${this.id}"` : "";
      return `<${tag}${id}>${this.innerHTML}</${tag}>`;
    },
  },
});
function HTMLElement() {}
HTMLElement.prototype = Object.create(Element.prototype);
HTMLElement.prototype.constructor = HTMLElement;
function HTMLCanvasElement() {}
HTMLCanvasElement.prototype = Object.create(HTMLElement.prototype);
HTMLCanvasElement.prototype.constructor = HTMLCanvasElement;
function HTMLIFrameElement() {}
HTMLIFrameElement.prototype = Object.create(HTMLElement.prototype);
HTMLIFrameElement.prototype.constructor = HTMLIFrameElement;
function NodeList() {}
NodeList.prototype = Object.create(Array.prototype);
function HTMLCollection() {}
HTMLCollection.prototype = Object.create(Array.prototype);
function Window() {}
function History() {}
function Range() {}
function Node() {}
function Text(value = "") {
  this.nodeName = "#text";
  this.nodeType = 3;
  this._textContent = String(value);
}
Text.prototype.cloneNode = function cloneNode() { return new Text(this.textContent); };
Object.defineProperties(Text.prototype, {
  data: {
    get() { return this._textContent; },
    set(value) {
      this._textContent = String(value);
      for (const observer of this._mutationObservers || []) observer._notify(this);
    },
  },
  textContent: {
    get() { return this._textContent; },
    set(value) {
      this._textContent = String(value);
      for (const observer of this._mutationObservers || []) observer._notify(this);
    },
  },
});
function File() {}
function Document() {}
function Navigator() {}
function Screen() {}
function Location() {}
function Attr() {}
function HTMLOptionElement() {}
HTMLOptionElement.prototype = Object.create(HTMLElement.prototype);
function Option(text = "", value = "", defaultSelected = false, selected = false) {
  const option = new Element("option", document);
  Object.setPrototypeOf(option, HTMLOptionElement.prototype);
  option.defaultSelected = Boolean(defaultSelected);
  option.selected = Boolean(selected);
  option.text = String(text);
  option.textContent = String(text);
  option.value = String(value);
  return option;
}
function Storage() {
  Object.defineProperty(this, "_values", { value: new Map() });
}
Storage.prototype.clear = function clear() {
  runtimeStats.storageClears += 1;
  this._values.clear();
};
Storage.prototype.getItem = function getItem(key) {
  runtimeStats.storageGets += 1;
  return this._values.has(String(key)) ? this._values.get(String(key)) : null;
};
Storage.prototype.key = function key(index) {
  runtimeStats.storageKeys += 1;
  return [...this._values.keys()][index] ?? null;
};
Storage.prototype.removeItem = function removeItem(key) {
  runtimeStats.storageRemoves += 1;
  this._values.delete(String(key));
};
Storage.prototype.setItem = function setItem(key, value) {
  runtimeStats.storageSets += 1;
  this._values.set(String(key), String(value));
};
Object.defineProperty(Storage.prototype, "length", {
  get() { return this._values.size; },
});
Object.defineProperty(Storage.prototype, Symbol.toStringTag, { value: "Storage" });
function createStorage() {
  return new Proxy(new Storage(), {
    defineProperty(target, key, descriptor) {
      if (typeof key === "string" && !(key in target) && "value" in descriptor) {
        target.setItem(key, descriptor.value);
        return true;
      }
      return Reflect.defineProperty(target, key, descriptor);
    },
    deleteProperty(target, key) {
      if (typeof key === "string" && target._values.has(key)) {
        target.removeItem(key);
        return true;
      }
      return Reflect.deleteProperty(target, key);
    },
    get(target, key, receiver) {
      if (typeof key === "string" && !(key in target) && target._values.has(key)) {
        return target.getItem(key);
      }
      return Reflect.get(target, key, receiver);
    },
    getOwnPropertyDescriptor(target, key) {
      if (typeof key === "string" && target._values.has(key)) {
        return { configurable: true, enumerable: true, value: target.getItem(key), writable: true };
      }
      return Reflect.getOwnPropertyDescriptor(target, key);
    },
    has(target, key) {
      return Reflect.has(target, key) || (typeof key === "string" && target._values.has(key));
    },
    ownKeys(target) {
      return [...Reflect.ownKeys(target), ...target._values.keys()];
    },
    set(target, key, value, receiver) {
      if (typeof key === "string" && !(key in target)) {
        target.setItem(key, value);
        return true;
      }
      return Reflect.set(target, key, value, receiver);
    },
  });
}
function Worker() {
  this.addEventListener = () => {};
  this.postMessage = () => {};
  this.terminate = () => {};
}
function Image() { Element.call(this, "img"); }
Image.prototype = Object.create(HTMLElement.prototype);
Object.defineProperty(Image.prototype, "src", {
  get() { return this._src || ""; },
  set(value) {
    this._src = value;
    sandboxSetTimeout(() => this.onload && this.onload(), 0);
  },
});
function HTMLMediaElement() { Element.call(this, "media"); }
HTMLMediaElement.prototype = Object.create(HTMLElement.prototype);
HTMLMediaElement.prototype.canPlayType = function canPlayType(type = "") {
  return /^(audio|video)\//.test(String(type)) ? "probably" : "";
};
HTMLMediaElement.prototype.load = function load() {};
HTMLMediaElement.prototype.pause = function pause() { this.paused = true; };
HTMLMediaElement.prototype.play = function play() {
  this.paused = false;
  return Promise.resolve();
};
function HTMLAudioElement() { HTMLMediaElement.call(this); this.tagName = "AUDIO"; }
HTMLAudioElement.prototype = Object.create(HTMLMediaElement.prototype);
function Audio(source = "") {
  const audio = new Element("audio", document);
  Object.setPrototypeOf(audio, HTMLAudioElement.prototype);
  audio.autoplay = false;
  audio.currentTime = 0;
  audio.duration = Number.NaN;
  audio.loop = false;
  audio.muted = false;
  audio.paused = true;
  audio.preload = "auto";
  audio.src = String(source);
  audio.volume = 1;
  return audio;
}
URL.createObjectURL ||= () => "blob:instrumented";
URL.revokeObjectURL ||= () => {};
const head = new Element("head");
const captchaElement = new Element("form");
captchaElement.id = "captcha-element";
elements.set("#captcha-element", captchaElement);
const captchaButton = new Element("button");
captchaButton.id = "captcha-button";
elements.set("#captcha-button", captchaButton);
function makeDocument() {
  const html = new Element("html");
  const body = new Element("body");
  const frameHead = new Element("head");
  html.appendChild(frameHead);
  html.appendChild(body);
  const value = {
    URL: "about:blank",
    characterSet: "UTF-8",
    charset: "UTF-8",
    compatMode: "CSS1Compat",
    cookie: "",
    documentElement: html,
    hidden: false,
    readyState: "complete",
    referrer: document?.URL || "",
    title: "",
    visibilityState: "visible",
    body,
    head: frameHead,
    addEventListener(type, listener) { body.addEventListener(type, listener); },
    close() { this.readyState = "complete"; },
    createElement(tagName) { return createElementForDocument(this, tagName); },
    createTextNode(text) { return new Text(text); },
    getElementById(id) { return this.documentElement.querySelector(`#${id}`); },
    getElementsByTagName(name) {
      if (String(name).toLowerCase() === "html") return [this.documentElement];
      return this.documentElement.getElementsByTagName(name);
    },
    open() { this.readyState = "loading"; return this; },
    querySelector(selector) { return this.documentElement.querySelector(selector); },
    querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); },
    removeEventListener(type, listener) { body.removeEventListener(type, listener); },
    dispatchEvent(event) { return body.dispatchEvent(event); },
    write(htmlSource) { this.body.innerHTML = htmlSource; },
  };
  Object.setPrototypeOf(value, Document.prototype);
  html.ownerDocument = value;
  frameHead.ownerDocument = value;
  body.ownerDocument = value;
  return value;
}

function createElementForDocument(ownerDocument, tagName) {
  const element = new Element(tagName, ownerDocument);
  const normalizedTagName = String(tagName).toLowerCase();
  Object.setPrototypeOf(element, HTMLElement.prototype);
  if (normalizedTagName === "canvas") {
    Object.setPrototypeOf(element, HTMLCanvasElement.prototype);
  }
  if (normalizedTagName === "audio") {
    Object.setPrototypeOf(element, HTMLAudioElement.prototype);
    element.paused = true;
  }
  if (normalizedTagName === "iframe") {
    Object.setPrototypeOf(element, HTMLIFrameElement.prototype);
    const frameDocument = makeDocument();
    const frameWindow = Object.create(window);
    frameWindow.document = frameDocument;
    frameWindow.window = frameWindow;
    frameWindow.self = frameWindow;
    frameWindow.parent = window;
    frameWindow.top = window;
    frameWindow.frameElement = element;
    frameWindow.frames = frameWindow;
    frameWindow.length = 0;
    frameWindow.localStorage = window.localStorage;
    frameWindow.sessionStorage = window.sessionStorage;
    frameWindow.location = {
      hash: "",
      host: "",
      hostname: "",
      href: "about:blank",
      pathname: "blank",
      protocol: "about:",
      search: "",
    };
    frameDocument.defaultView = frameWindow;
    frameDocument.location = frameWindow.location;
    element.src = "about:blank";
    Object.defineProperties(element, {
      contentDocument: {
        configurable: true,
        get() {
          runtimeStats.iframeContentDocumentReads += 1;
          return frameDocument;
        },
      },
      contentWindow: {
        configurable: true,
        get() {
          runtimeStats.iframeContentWindowReads += 1;
          return frameWindow;
        },
      },
    });
  }
  return element;
}

const document = {
  URL: captchaPageUrl,
  characterSet: "UTF-8",
  charset: "UTF-8",
  compatMode: "CSS1Compat",
  cookie: "",
  documentElement: new Element("html"),
  hidden: false,
  readyState: "complete",
  referrer: "https://www.cnblogs.com/",
  title: "CNBlogs Sign In",
  visibilityState: "visible",
  body: new Element("body"),
  head,
  addEventListener(type, listener) { this.documentElement.addEventListener(type, listener); },
  createElement(tagName) {
    return createElementForDocument(this, tagName);
  },
  createEvent() {
    return { initCustomEvent() {} };
  },
  createTextNode(text) {
    return new Text(text);
  },
  getElementById(id) {
    return elements.get(`#${id}`) || null;
  },
  getElementsByTagName(name) {
    const normalized = String(name).toLowerCase();
    if (normalized === "head") return [head];
    if (normalized === "body") return [this.body];
    if (normalized === "html") return [this.documentElement];
    return this.documentElement.getElementsByTagName(name);
  },
  querySelector(selector) {
    if (selector === "head") return head;
    if (selector === "body") return this.body;
    return elements.get(selector) || null;
  },
  querySelectorAll(selector) {
    const known = elements.get(selector);
    if (known) return [known];
    return this.documentElement.querySelectorAll(selector);
  },
  removeEventListener(type, listener) {
    this.documentElement.removeEventListener(type, listener);
  },
  dispatchEvent(event) { return this.documentElement.dispatchEvent(event); },
};
document.documentElement.ownerDocument = document;
document.body.ownerDocument = document;
document.head.ownerDocument = document;
document.documentElement.appendChild(document.head);
document.documentElement.appendChild(document.body);
document.body.appendChild(captchaElement);
document.body.appendChild(captchaButton);

async function runLoginActivity() {
  const inputProfiles = [
    { id: "signin-username", length: randomInteger(7, 10), type: "text" },
    { id: "signin-password", length: randomInteger(10, 14), type: "password" },
  ];
  await wait(randomInteger(240, 420));
  for (const profile of inputProfiles) {
    const input = document.createElement("input");
    input.id = profile.id;
    input.type = profile.type;
    input.value = "";
    captchaElement.appendChild(input);
    input.dispatchEvent(makeSyntheticEvent("focus", input, { bubbles: false }));
    input.dispatchEvent(makeSyntheticEvent("focusin", input));
    for (let index = 0; index < profile.length; index += 1) {
      const keyCode = randomInteger(65, 90);
      const key = String.fromCharCode(keyCode).toLowerCase();
      const keyboardProperties = {
        code: `Key${key.toUpperCase()}`,
        key,
        keyCode,
        which: keyCode,
      };
      input.dispatchEvent(makeSyntheticEvent("keydown", input, keyboardProperties));
      input.value += key;
      input.dispatchEvent(makeSyntheticEvent("input", input, {
        data: key,
        inputType: "insertText",
      }));
      input.dispatchEvent(makeSyntheticEvent("keyup", input, keyboardProperties));
      await wait(randomInteger(55, 135));
    }
    input.dispatchEvent(makeSyntheticEvent("change", input));
    input.dispatchEvent(makeSyntheticEvent("focusout", input));
    input.dispatchEvent(makeSyntheticEvent("blur", input, { bubbles: false }));
    await wait(randomInteger(160, 360));
  }

  const pointCount = randomInteger(10, 15);
  for (let index = 0; index < pointCount; index += 1) {
    const progress = (index + 1) / pointCount;
    const x = Math.round(45 + (progress * 520) + randomInteger(-8, 8));
    const y = Math.round(95 + (progress * 230) + randomInteger(-12, 12));
    document.body.dispatchEvent(makeMouseEvent("mousemove", document.body, 0, x, y));
    await wait(randomInteger(18, 48));
  }
  const scrollTop = randomInteger(8, 24);
  document.documentElement.scrollTop = scrollTop;
  document.documentElement.dispatchEvent(
    makeSyntheticEvent("scroll", document.documentElement, {
      scrollX: 0,
      scrollY: scrollTop,
    }),
  );
  await wait(randomInteger(220, 420));
}

async function waitForAttachedElement(selector, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const element = elements.get(selector);
    if (element?.parentNode) return element;
    await wait(100);
  }
  return null;
}

async function clickCheckboxNaturally(checkbox) {
  const bounds = checkbox.getBoundingClientRect();
  const targetX = Math.round(bounds.left + Math.min(20, bounds.width / 2));
  const targetY = Math.round(bounds.top + Math.min(20, bounds.height / 2));
  const steps = randomInteger(8, 12);
  for (let index = 0; index < steps; index += 1) {
    const progress = (index + 1) / steps;
    const x = Math.round(80 + ((targetX - 80) * progress) + randomInteger(-3, 3));
    const y = Math.round(120 + ((targetY - 120) * progress) + randomInteger(-3, 3));
    document.body.dispatchEvent(makeMouseEvent("mousemove", document.body, 0, x, y));
    await wait(randomInteger(20, 48));
  }
  checkbox.dispatchEvent(makeMouseEvent("mouseover", checkbox, 0, targetX, targetY));
  checkbox.dispatchEvent(makeMouseEvent("mouseenter", checkbox, 0, targetX, targetY));
  checkbox.dispatchEvent(makeMouseEvent("mousemove", checkbox, 0, targetX, targetY));
  await wait(randomInteger(90, 180));
  checkbox.dispatchEvent(makeMouseEvent("mousedown", checkbox, 1, targetX, targetY));
  await wait(randomInteger(85, 160));
  checkbox.dispatchEvent(makeMouseEvent("mouseup", checkbox, 0, targetX, targetY));
  checkbox.dispatchEvent(makeMouseEvent("click", checkbox, 0, targetX, targetY));
}

class FakeXMLHttpRequest {
  constructor() {
    this.headers = {};
    this.status = 200;
    this.responseText = "";
  }
  getResponseHeader(name) {
    return this.responseHeaders?.[String(name).toLowerCase()] || null;
  }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(name, value) { this.headers[name] = value; }
  send(body) {
    const requestRecord = {
      method: this.method,
      url: this.url,
      headers: this.headers,
      body,
    };
    capturedRequests.push(requestRecord);
    if (useNetwork) {
      assertAllowedAliyunUrl(this.url);
      runtimeFetch(this.url, {
        method: this.method,
        headers: {
          Origin: captchaOrigin,
          Referer: captchaPageUrl,
          "User-Agent": window.navigator.userAgent,
          ...this.headers,
        },
        body,
      }).then(async (response) => {
        this.status = response.status;
        this.responseHeaders = Object.fromEntries(response.headers.entries());
        this.response = this.responseText = await response.text();
        requestRecord.status = response.status;
        requestRecord.responseHeaders = this.responseHeaders;
        try {
          requestRecord.response = JSON.parse(this.responseText);
        } catch {
          requestRecord.response = this.responseText;
        }
        this.onload && this.onload();
      }).catch((error) => {
        requestRecord.error = String(error && error.stack ? error.stack : error);
        this.onerror && this.onerror(error);
      });
      return;
    }
    this.response = this.responseText = JSON.stringify({
      Code: "Fail",
      Message: "instrumented response",
      Success: false,
    });
    requestRecord.response = JSON.parse(this.responseText);
    sandboxSetTimeout(() => this.onload && this.onload(), 0);
  }
}
class MutationObserver {
  constructor(callback) {
    this.callback = callback;
    this.targets = new Set();
    this.pending = false;
  }
  disconnect() {
    for (const target of this.targets) target._mutationObservers?.delete(this);
    this.targets.clear();
  }
  observe(target) {
    target._mutationObservers ||= new Set();
    target._mutationObservers.add(this);
    this.targets.add(target);
  }
  _notify(target) {
    if (this.pending) return;
    this.pending = true;
    sandboxSetTimeout(() => {
      this.pending = false;
      this.callback([{ target, type: "characterData" }], this);
    }, 0);
  }
  takeRecords() { return []; }
}
class IntersectionObserver extends MutationObserver {}
class ResizeObserver extends MutationObserver {}
class WebGLRenderingContext {}
class WebGL2RenderingContext extends WebGLRenderingContext {}
class AudioContext {
  constructor() {
    this.destination = {};
    this.sampleRate = 44100;
    this.state = "running";
  }
  close() { this.state = "closed"; return Promise.resolve(); }
  createAnalyser() {
    return {
      channelCount: 2,
      connect() {},
      disconnect() {},
      frequencyBinCount: 1024,
      getFloatFrequencyData(array) { array.fill(-100); },
    };
  }
  createDynamicsCompressor() { return { connect() {}, disconnect() {} }; }
  createGain() { return { connect() {}, disconnect() {}, gain: { value: 1 } }; }
  createOscillator() {
    return { connect() {}, disconnect() {}, frequency: { value: 440 }, start() {}, stop() {}, type: "sine" };
  }
  createScriptProcessor() { return { connect() {}, disconnect() {} }; }
  resume() { this.state = "running"; return Promise.resolve(); }
  suspend() { this.state = "suspended"; return Promise.resolve(); }
}
const requestAnimationFrame = (callback) => {
  runtimeStats.rafScheduled += 1;
  return sandboxSetTimeout(() => {
    runtimeStats.rafFired += 1;
    callback(globalThis.performance.now());
  }, 16);
};
const cancelAnimationFrame = (handle) => sandboxClearTimer(handle);
function makeIndexedDbRequest(result = {}) {
  const request = { error: null, result };
  sandboxSetTimeout(() => request.onsuccess?.({ target: request }), 0);
  return request;
}
const window = {
  addEventListener(type, listener) {
    if (listener == null) return;
    this._listeners ||= new Map();
    const key = String(type);
    this._listeners.set(key, [...(this._listeners.get(key) || []), listener]);
  },
  atob(value) { return Buffer.from(value, "base64").toString("binary"); },
  alert() {},
  btoa(value) { return Buffer.from(value, "binary").toString("base64"); },
  confirm() { return false; },
  blur() {},
  close() {},
  focus() {},
  CustomEvent: function CustomEvent() {},
  Event: function Event() {},
  document,
  location: {
    hash: captchaPageLocation.hash,
    host: captchaPageLocation.host,
    hostname: captchaPageLocation.hostname,
    href: captchaPageLocation.href,
    origin: captchaPageLocation.origin,
    pathname: captchaPageLocation.pathname,
    port: captchaPageLocation.port,
    protocol: captchaPageLocation.protocol,
    search: captchaPageLocation.search,
  },
  navigator: {
    appCodeName: "Mozilla",
    appName: "Netscape",
    appVersion: "5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    cookieEnabled: true,
    deviceMemory: 16,
    doNotTrack: null,
    hardwareConcurrency: 10,
    javaEnabled() { return false; },
    language: "zh-CN",
    languages: ["zh-CN"],
    maxTouchPoints: 0,
    mimeTypes: [
      { description: "Portable Document Format", suffixes: "pdf", type: "application/pdf" },
      { description: "Portable Document Format", suffixes: "pdf", type: "text/pdf" },
    ],
    onLine: true,
    oscpu: "Intel Mac OS X 10_15_7",
    pdfViewerEnabled: true,
    platform: "MacIntel",
    plugins: [
      { filename: "internal-pdf-viewer", length: 2, name: "PDF Viewer" },
      { filename: "internal-pdf-viewer", length: 2, name: "Chrome PDF Viewer" },
      { filename: "internal-pdf-viewer", length: 2, name: "Chromium PDF Viewer" },
      { filename: "internal-pdf-viewer", length: 2, name: "Microsoft Edge PDF Viewer" },
      { filename: "internal-pdf-viewer", length: 2, name: "WebKit built-in PDF" },
    ],
    product: "Gecko",
    productSub: "20030107",
    vendor: "Google Inc.",
    vendorSub: "",
    webdriver: false,
    connection: {
      downlink: 10,
      effectiveType: "4g",
      rtt: 50,
      saveData: false,
      type: "wifi",
    },
    mediaDevices: { async enumerateDevices() { return []; } },
    permissions: { async query() { return { state: "prompt" }; } },
    storage: { async estimate() { return { quota: 0, usage: 0 }; } },
    userAgentData: {
      brands: [
        { brand: "Chromium", version: browserMajorVersion },
        { brand: "Not?A_Brand", version: "24" },
        { brand: "Google Chrome", version: browserMajorVersion },
      ],
      mobile: false,
      platform: "macOS",
      async getHighEntropyValues() {
        return {
          architecture: "arm",
          bitness: "64",
          brands: this.brands,
          fullVersionList: this.brands.map(({ brand, version }) => ({
            brand,
            version: brand === "Not?A_Brand" ? "24.0.0.0" : `${version}.0.0.0`,
          })),
          mobile: false,
          model: "",
          platform: this.platform,
          platformVersion: "26.2.0",
          uaFullVersion: `${browserMajorVersion}.0.0.0`,
          wow64: false,
        };
      },
    },
    webkitTemporaryStorage: {
      queryUsageAndQuota(success) { success(0, 10 * 1024 * 1024 * 1024); },
    },
    userAgent,
  },
  screen: {
    availHeight: 838,
    availWidth: 1470,
    colorDepth: 30,
    height: 956,
    pixelDepth: 30,
    width: 1470,
    orientation: { angle: 0, type: "landscape-primary" },
  },
  devicePixelRatio: 2,
  innerHeight: 720,
  innerWidth: 1280,
  outerHeight: 838,
  outerWidth: 1462,
  history: Object.assign(new History(), {
    length: 1,
    pushState() {},
    replaceState() {},
    state: null,
  }),
  getComputedStyle(element) {
    return new Proxy({
      display: element?.style?.display || "block",
      fontFamily: element?.style?.fontFamily || "Arial",
      fontSize: element?.style?.fontSize || "10px",
      getPropertyValue(name) { return this[String(name)] || ""; },
      height: `${element?.height || 40}px`,
      lineHeight: element?.style?.lineHeight || "normal",
      width: `${element?.width || 360}px`,
    }, {
      get(target, key) { return key in target ? target[key] : ""; },
    });
  },
  dispatchEvent(event) {
    runtimeEvents.push({
      message: event?.message || String(event?.reason || ""),
      type: event?.type || "unknown",
    });
    for (const listener of this._listeners?.get(event?.type) || []) {
      if (typeof listener === "function") listener.call(this, event);
      else listener.handleEvent?.(event);
    }
    return true;
  },
  indexedDB: {
    deleteDatabase() {
      runtimeStats.indexedDbDeletes += 1;
      return makeIndexedDbRequest();
    },
    open() {
      runtimeStats.indexedDbOpens += 1;
      return makeIndexedDbRequest({
        close() {},
        createObjectStore() { return {}; },
        objectStoreNames: { contains() { return false; }, length: 0 },
        transaction() { return { objectStore() { return {}; } }; },
      });
    },
  },
  localStorage: createStorage(),
  matchMedia() { return { matches: false, addListener() {}, removeListener() {} }; },
  moveBy() {},
  moveTo() {},
  sessionStorage: createStorage(),
  removeEventListener() {},
  prompt() { return null; },
  print() {},
  scroll() {},
  scrollBy() {},
  scrollTo() {},
  webkitRequestFileSystem(_type, _size, success) { success?.({}); },
};
document.defaultView = window;
document.fonts = { check() { return false; }, ready: Promise.resolve() };
document.styleSheets = [];
Object.setPrototypeOf(document, Document.prototype);
Object.setPrototypeOf(window, Window.prototype);
Object.setPrototypeOf(window.navigator, Navigator.prototype);
Object.setPrototypeOf(window.screen, Screen.prototype);
Object.setPrototypeOf(window.location, Location.prototype);
window.window = window;
window.self = window;
Object.assign(window, {
  Array,
  ArrayBuffer,
  Attr,
  AudioContext,
  Audio,
  Boolean,
  Blob,
  CanvasGradient,
  CanvasPattern,
  CanvasRenderingContext2D,
  Date,
  Document,
  DOMTokenList,
  Element,
  Error,
  Function,
  File,
  HTMLCollection,
  HTMLCanvasElement,
  HTMLElement,
  HTMLIFrameElement,
  History,
  HTMLAudioElement,
  HTMLOptionElement,
  HTMLMediaElement,
  Image,
  ImageData,
  IntersectionObserver,
  JSON,
  Map,
  Math,
  MutationObserver,
  Navigator,
  Node,
  NodeList,
  Number,
  Object,
  Option,
  Range,
  ResizeObserver,
  Screen,
  Location,
  RegExp,
  Set,
  Storage,
  String,
  Symbol,
  Text,
  TextMetrics,
  TextDecoder,
  TextEncoder,
  Uint8Array,
  Uint8ClampedArray,
  URL,
  WeakMap,
  Window,
  WebGL2RenderingContext,
  WebGLRenderingContext,
  Worker,
  cancelAnimationFrame,
  crypto: globalThis.crypto,
  decodeURIComponent,
  encodeURIComponent,
  escape,
  fetch: runtimeFetch,
  performance: globalThis.performance,
  requestAnimationFrame,
  unescape,
});
window.AudioContext = AudioContext;
window.webkitAudioContext = AudioContext;
window.cancelAnimationFrame = cancelAnimationFrame;
window.requestAnimationFrame = requestAnimationFrame;
window.clearInterval = sandboxClearTimer;
window.clearTimeout = sandboxClearTimer;
window.setInterval = sandboxSetInterval;
window.setTimeout = sandboxSetTimeout;
window.CSS = { supports() { return true; } };
window.openDatabase = () => ({});
window.parent = window;
window.top = window;

const context = {
  ArrayBuffer,
  AudioContext,
  Audio,
  atob: window.atob,
  Attr,
  btoa: window.btoa,
  alert: window.alert,
  blur: window.blur,
  close: window.close,
  confirm: window.confirm,
  focus: window.focus,
  Blob,
  Buffer,
  CanvasGradient,
  CanvasPattern,
  CanvasRenderingContext2D,
  Date,
  Document,
  DOMTokenList,
  Element,
  Error,
  File,
  fetch: runtimeFetch,
  JSON,
  HTMLCollection,
  HTMLCanvasElement,
  HTMLElement,
  HTMLIFrameElement,
  History,
  HTMLAudioElement,
  HTMLOptionElement,
  HTMLMediaElement,
  Image,
  ImageData,
  IntersectionObserver,
  Location,
  Math,
  MutationObserver,
  performance: globalThis.performance,
  prompt: window.prompt,
  print: window.print,
  NodeList,
  Navigator,
  Node,
  Option,
  Range,
  ResizeObserver,
  Screen,
  Storage,
  TextDecoder,
  TextEncoder,
  Text,
  TextMetrics,
  Uint8Array,
  Uint8ClampedArray,
  URL,
  Window,
  WebGL2RenderingContext,
  WebGLRenderingContext,
  Worker,
  cancelAnimationFrame,
  crypto: globalThis.crypto,
  XMLHttpRequest: FakeXMLHttpRequest,
  clearInterval: sandboxClearTimer,
  clearTimeout: sandboxClearTimer,
  console: runtimeConsole,
  CSSRule: function CSSRule() {},
  CSSStyleRule: function CSSStyleRule() {},
  CSSStyleSheet: function CSSStyleSheet() {},
  CustomEvent: window.CustomEvent,
  document,
  encodeURIComponent,
  decodeURIComponent,
  escape,
  Event: window.Event,
  location: window.location,
  history: window.history,
  navigator: window.navigator,
  screen: window.screen,
  open() { return null; },
  matchMedia: window.matchMedia,
  moveBy: window.moveBy,
  moveTo: window.moveTo,
  resizeBy() {},
  resizeTo() {},
  scroll() {},
  scrollBy() {},
  scrollTo() {},
  setInterval: sandboxSetInterval,
  setTimeout: sandboxSetTimeout,
  requestAnimationFrame,
  unescape,
  window,
};
context.globalThis = context;
context.self = window;
window.XMLHttpRequest = FakeXMLHttpRequest;
window.__ALIYUN_MISSING_CALLS = [];
window.__ALIYUN_RECORD_MISSING = (target, key, args) => {
  const targetKeys = Object.keys(target).slice(0, 24);
  window.__ALIYUN_MISSING_CALLS.push({
    argPreviews: Array.from(args || [], (value) => formatConsoleValue(value).slice(0, 300)),
    argTypes: Array.from(args || [], (value) => typeof value),
    key,
    prototypeKeys: Object.keys(Object.getPrototypeOf(target) || {}).slice(0, 24),
    stack: formatConsoleValue(new Error("missing dynamic call")),
    target: Object.prototype.toString.call(target),
    targetKeys,
    targetValueTypes: Object.fromEntries(
      targetKeys.map((name) => [name, typeof target[name]]),
    ),
    targetValuePreviews: Object.fromEntries(
      targetKeys.map((name) => [
        name,
        formatConsoleValue(target[name]).slice(0, 300),
      ]),
    ),
    valuePreview: String(target?.[key]).slice(0, 120),
    valueType: typeof target?.[key],
  });
  return "";
};
vm.runInNewContext("window.Promise = Promise", context, {
  filename: "aliyun-sandbox-bootstrap.js",
});

vm.runInNewContext(instrumented, context, {
  filename: sourcePath,
  timeout: 10_000,
});

let cnblogsFingerprintFunction;
let cnblogsFingerprintLoadError;
if (cnblogsCaptchaSourcePath) {
  try {
    const fingerprintMarker = "function ys(){return ws.apply(this,arguments)}";
    const cnblogsSource = fs.readFileSync(cnblogsCaptchaSourcePath, "utf8");
    if (!cnblogsSource.includes(fingerprintMarker)) {
      throw new Error("CNBlogs fingerprint entrypoint was not found");
    }
    const instrumentedCnblogsSource = cnblogsSource
      .replace(
        fingerprintMarker,
        `${fingerprintMarker};window.__CNBLOGS_FINGERPRINT=ys;`,
      )
      .replace("},SensePro})", "},e.SensePro})");
    vm.runInNewContext(
      instrumentedCnblogsSource,
      context,
      { filename: cnblogsCaptchaSourcePath, timeout: 10_000 },
    );
    const captchaChunk = window.webpackChunkcnblogs_account?.find(
      (chunk) => typeof chunk?.[1]?.[98711] === "function",
    );
    if (!captchaChunk) {
      throw new Error("CNBlogs CAPTCHA webpack module was not found");
    }
    const webpackRequire = (moduleId) => {
      if (moduleId !== 74788) {
        throw new Error(`unexpected CNBlogs webpack dependency: ${moduleId}`);
      }
      return {
        LFG() { return {}; },
        R0b: {},
        Yz7(definition) { return definition; },
      };
    };
    webpackRequire.d = (target, definitions) => {
      for (const [name, getter] of Object.entries(definitions)) {
        Object.defineProperty(target, name, { enumerable: true, get: getter });
      }
    };
    captchaChunk[1][98711]({ exports: {} }, {}, webpackRequire);
    if (typeof window.__CNBLOGS_FINGERPRINT !== "function") {
      throw new Error("CNBlogs fingerprint function was not exported");
    }
    cnblogsFingerprintFunction = window.__CNBLOGS_FINGERPRINT;
  } catch (error) {
    cnblogsFingerprintLoadError = formatConsoleValue(error);
  }
}
externalScriptLoader = async (sourceUrl) => {
  const parsed = new URL(sourceUrl, "https://g.alicdn.com/");
  assertAllowedAliyunUrl(parsed);
  if (parsed.hostname !== "g.alicdn.com") {
    throw new Error(`unexpected external script URL: ${parsed.href}`);
  }
  const record = { url: parsed.href };
  externalScripts.push(record);
  const response = await runtimeFetch(parsed, {
    headers: { Referer: captchaPageUrl, "User-Agent": userAgent },
  });
  record.status = response.status;
  if (!response.ok) {
    throw new Error(`external script returned HTTP ${response.status}`);
  }
  const downloadedSource = await response.text();
  record.bytes = Buffer.byteLength(downloadedSource);
  const diagnosticSource = instrumentDynamicCalls(downloadedSource);
  try {
    vm.runInNewContext(diagnosticSource, context, {
      filename: parsed.pathname,
      timeout: 10_000,
    });
  } catch (error) {
    record.error = formatConsoleValue(error);
    throw error;
  }
};

let dynamicError;
if (dynamicSourcePath) {
  try {
    const dynamicSource = instrumentDynamicCalls(
      fs.readFileSync(dynamicSourcePath, "utf8"),
    );
    vm.runInNewContext(dynamicSource, context, {
      filename: dynamicSourcePath,
      timeout: 10_000,
    });
  } catch (error) {
    dynamicError = String(error && error.stack ? error.stack : error);
  }
}

const internals = window.__ALIYUN_INTERNALS;
let deviceSourceLoadError;
if (deviceSourcePath) {
  try {
    window.um = {};
    window.z_um = {};
    const deviceSource = fs.readFileSync(deviceSourcePath, "utf8").replace(
      "C[L([97,L()][0],[46,L()][0])](tA)?n+=16:n=124",
      "(window.__FEILIN_SELENIUM_PROBE={documentProp:L(81,23),nestedProp:_,method:L(97,46),needle:tA,valueType:typeof C},C&&C[L(97,46)](tA))?n+=16:n=124",
    );
    vm.runInNewContext(deviceSource, context, {
      filename: deviceSourcePath,
      timeout: 10_000,
    });
  } catch (error) {
    deviceSourceLoadError = String(error && error.stack ? error.stack : error);
  }
}
let decodedDeviceConfig;
let decodedDeviceConfigError;
let runtimeDeviceConfig = encryptedDeviceConfig;
if (useDeviceFixture) {
  const encodeFixtureField = (value) => Buffer.from(value, "utf8").toString("base64");
  const fixtureTimestamp = String(Date.now());
  const fixturePlaintext = [
    encodeFixtureField("0123456789abcdef"),
    encodeFixtureField("1"),
    `fixture-session-${fixtureTimestamp}`,
    "1.5.1/feilin123.ed38caecf23e5ef6d0497886ba8d6235095caf67089a43a9a30484d225e71e3c",
    encodeFixtureField(""),
    encodeFixtureField(""),
    encodeFixtureField(""),
    fixtureTimestamp,
    "127.0.0.1",
  ].join("#");
  runtimeDeviceConfig = internals.aesSecret(
    internals.deviceConfigSecret,
    fixturePlaintext,
  );
}
if (runtimeDeviceConfig) {
  try {
    decodedDeviceConfig = internals.decryptConfig(runtimeDeviceConfig);
  } catch (error) {
    decodedDeviceConfigError = String(error);
  }
}
let deviceRuntimeError;
let deviceRuntimeResult;
let deviceInitReturn;
let deviceInitResolvedValue;
let deviceInitState = "not-run";
if (deviceSourcePath && decodedDeviceConfig) {
  try {
    internals.deviceConfig._extend({
      DeviceConfig: runtimeDeviceConfig,
      appKey: "ab034ec0643f91399eb33e062dc7fae1",
      appName: "saf-captcha",
      deviceConfig: decodedDeviceConfig,
      endpoints: [
        "https://cloudauth-device-dualstack.cn-shanghai.aliyuncs.com",
        "https://cn-shanghai.device.saf.aliyuncs.com",
      ],
      initTime: Date.now(),
      logs: [],
      prefix: captchaPrefix,
      region: captchaRegion,
      sceneId,
      timestamp: decodedDeviceConfig.timestamp,
      dev: false,
    });
    if (!window.FEILIN || typeof window.FEILIN.initFeiLin !== "function") {
      throw new Error("FEILIN.initFeiLin was not exported");
    }
    await Promise.race([
      new Promise((resolve) => {
        deviceInitState = "pending";
        traceDevicePromises = true;
        deviceInitReturn = window.FEILIN.initFeiLin(internals.deviceConfig, (state, result) => {
          deviceInitState = "callback";
          deviceRuntimeResult = { state, result };
          resolve();
        });
        if (deviceInitReturn && typeof deviceInitReturn.then === "function") {
          deviceInitReturn.then(
            (value) => {
              deviceInitResolvedValue = value;
              if (deviceInitState === "pending") deviceInitState = "resolved";
              resolve();
            },
            (error) => {
              deviceInitState = "rejected";
              deviceRuntimeError = String(error && error.stack ? error.stack : error);
              resolve();
            },
          );
        }
      }),
      new Promise((resolve) => setTimeout(resolve, 8_000)),
    ]);
    await new Promise((resolve) => setTimeout(resolve, 500));
    traceDevicePromises = false;
  } catch (error) {
    deviceRuntimeError = String(error && error.stack ? error.stack : error);
  }
}
const project = (value, keys) => Object.fromEntries(
  keys.map((key) => [key, value[key]]),
);
const listenerCounts = (target) => Object.fromEntries(
  [...(target?._listeners || new Map()).entries()].map(([type, listeners]) => [
    type,
    listeners.length,
  ]),
);
const sampleParams = {
  Action: "InitCaptcha",
  AaduaneId: internals.captchaKeys.KEY_ID,
  Format: "JSON",
  Language: "zh-CN",
  SceneId: sceneId,
  SignatureMethod: "HMAC-SHA1",
  SignatureNonce: "00000000-0000-4000-8000-000000000000",
  SignatureVersion: "1.0",
  Timestamp: "2026-08-01T00:00:00Z",
  Version: "2023-03-05",
};
const sampleDeviceFlag = internals.aesSecret(
  internals.deviceKeys.WEB_AES_FLAG_SECRET_KEY,
  `W.10001.c#saf-captcha##captcha-front#${captchaPrefix}#${captchaRegion}`,
);
const sampleDeviceData = internals.aesEncrypt([
  "ab034ec0643f91399eb33e062dc7fae1",
  "W",
  sampleDeviceFlag,
  "W20220202",
  "CLOUD",
  "",
]);
let initProbeError;
let initProbeInstance;
let initProbeResult;
let initProbeSuccessToken;
let initProbeVerificationError;
let initProbeVerificationState = "not-run";
let initProbeChallengeState = "not-run";
let initProbeActivityState = "not-run";

async function waitForInitProbeSuccess(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (!initProbeSuccessToken && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return Boolean(initProbeSuccessToken);
}

if (runInitProbe) {
  try {
    context.__ALIYUN_INIT_CALLBACKS = {
      fail(error) { initProbeError ||= formatErrorValue(error); },
      getInstance(instance) { initProbeInstance = instance; },
      onError(error) { initProbeError ||= formatErrorValue(error); },
      success(token) { initProbeSuccessToken = token; },
    };
    context.__ALIYUN_RUNTIME_CONFIG = {
      prefix: captchaPrefix,
      region: captchaRegion,
      sceneId,
    };
    const sandboxProbe = vm.runInNewContext(`
      window.AliyunCaptchaConfig = {
        prefix: __ALIYUN_RUNTIME_CONFIG.prefix,
        region: __ALIYUN_RUNTIME_CONFIG.region
      };
      window.initAliyunCaptcha({
        SceneId: __ALIYUN_RUNTIME_CONFIG.sceneId,
        button: "#captcha-button",
        element: "#captcha-element",
        fail: __ALIYUN_INIT_CALLBACKS.fail,
        getInstance: __ALIYUN_INIT_CALLBACKS.getInstance,
        mode: "popup",
        onError: __ALIYUN_INIT_CALLBACKS.onError,
        slideStyle: { height: 40, width: 360 },
        success: __ALIYUN_INIT_CALLBACKS.success
      });
    `, context, { filename: "aliyun-init-probe.js" });
    const probe = Promise.resolve(sandboxProbe).then((result) => {
      initProbeResult = result;
    });
    await Promise.race([
      probe,
      new Promise((resolve) => setTimeout(resolve, 15_000)),
    ]);
    await wait(250);
    if (simulateActivity) {
      initProbeActivityState = "running";
      await runLoginActivity();
      initProbeActivityState = "completed";
    }
    if (typeof captchaButton.onclick === "function") {
      initProbeVerificationState = "pending";
      initProbeInstance?.show?.();
      captchaButton.click();
      await wait(1_500);
      initProbeVerificationState = initProbeSuccessToken
        ? "succeeded"
        : elements.has("#aliyunCaptcha-checkbox-icon")
          ? "challenge"
          : "completed";
      if (runChallengeProbe) {
        const checkbox = await waitForAttachedElement(
          "#aliyunCaptcha-checkbox-icon",
          4_000,
        );
        if (!checkbox) {
          initProbeChallengeState = "checkbox-not-found";
        } else {
          initProbeChallengeState = "dispatched";
          await clickCheckboxNaturally(checkbox);
          const verified = await waitForInitProbeSuccess(challengeTimeoutMs);
          initProbeChallengeState = verified ? "succeeded" : "timeout";
          initProbeVerificationState = verified ? "succeeded" : "challenge";
        }
      }
    } else if (typeof initProbeInstance?.startTracelessVerification === "function") {
      initProbeVerificationState = "missing-button-handler";
    }
  } catch (error) {
    initProbeError = String(error && error.stack ? error.stack : error);
  }
}
const sampleInitResult = {
  CertifyId: certifyId,
  Message: "success",
  RequestId: "REQUEST_ID",
  Code: "Success",
  LimitFlow: false,
  Success: true,
  StaticPath: "3.29.0/cx.065.a8eec56eee00587a",
  CaptchaType: "TRACELESS",
};
let dynamicInstance;
let dynamicInstanceError;
let dynamicInstanceTimedOut = false;
let dynamicSuccessToken;
let dynamicVerificationPayload;
if (runDynamicSample && typeof window.AliyunCaptcha === "function") {
  try {
    internals.captchaConfig._extend({
      ...internals.normalizeInitResult(
        { ...sampleInitResult },
        internals.captchaConfig,
      ),
      SceneId: sceneId,
      DeviceToken: deviceRuntimeResult?.result?.DeviceToken || "",
      button: "#captcha-button",
      captchaVerifyCallback(payload) {
        dynamicVerificationPayload = payload;
        return { captchaResult: false, bizResult: false };
      },
      delayBeforeSuccess: false,
      element: "#captcha-element",
      immediate: true,
      mode: "popup",
      prefix: captchaPrefix,
      region: captchaRegion,
      success(token) { dynamicSuccessToken = token; },
      urls: [`https://${captchaPrefix}.captcha-open-dual.aliyuncs.com/`],
      verifyType: "2.0",
    });
    window.AliyunCaptcha.prototype.config = internals.captchaConfig;
    window.AliyunCaptcha.prototype.deviceConfig = internals.deviceConfig;
    dynamicInstance = new window.AliyunCaptcha();
    let verificationSettled = false;
    const verification = dynamicInstance.startTracelessVerification();
    if (verification && typeof verification.then === "function") {
      verification.then(
        () => { verificationSettled = true; },
        (error) => {
          verificationSettled = true;
          dynamicInstanceError = String(error && error.stack ? error.stack : error);
        },
      );
    } else {
      verificationSettled = true;
    }
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    dynamicInstanceTimedOut = !verificationSettled;
    await new Promise((resolve) => setTimeout(resolve, 50));
  } catch (error) {
    dynamicInstanceError = String(error && error.stack ? error.stack : error);
  }
}
let cnblogsFingerprint;
let cnblogsFingerprintError = cnblogsFingerprintLoadError;
if (initProbeSuccessToken && cnblogsFingerprintFunction && !cnblogsFingerprintError) {
  try {
    cnblogsFingerprint = await Promise.race([
      Promise.resolve(cnblogsFingerprintFunction()),
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("CNBlogs fingerprint timed out")),
        10_000,
      )),
    ]);
    const decodedFingerprint = Buffer.from(cnblogsFingerprint?.fp || "", "base64");
    const permutation = [...decodedFingerprint.subarray(16)];
    if (
      decodedFingerprint.length !== 32
      || new Set(permutation).size !== 16
      || permutation.some((value) => value > 15)
    ) {
      throw new Error("CNBlogs fingerprint has an invalid payload");
    }
  } catch (error) {
    cnblogsFingerprintError = formatConsoleValue(error);
    cnblogsFingerprint = undefined;
  }
}
const fingerprintShims = {
  canvas: runtimeStats.canvas2d > 0
    && runtimeStats.canvasDataUrls > 0
    && runtimeStats.canvasWebgl > 0,
  iframe: runtimeStats.iframeAppends > 0,
  storage: runtimeStats.storageSets > 0 && runtimeStats.storageRemoves > 0,
};
fingerprintShims.passed = Object.values(fingerprintShims).every(Boolean);
const output = {
  runtimeConsoleMessages,
  runtimeEvents,
  runtimeStats,
  fingerprintShims,
  cnblogsFingerprint,
  cnblogsFingerprintError,
  dom: {
    ids: [...elements.keys()].sort(),
    listeners: Object.fromEntries(
      [...elements.entries()]
        .filter(([, element]) => element._listeners?.size)
        .map(([selector, element]) => [
          selector,
          Object.fromEntries(
            [...element._listeners.entries()].map(([type, listeners]) => [
              type,
              listeners.length,
            ]),
          ),
        ]),
    ),
    rootListeners: {
      body: listenerCounts(document.body),
      button: listenerCounts(captchaButton),
      documentElement: listenerCounts(document.documentElement),
      head: listenerCounts(document.head),
    },
    windowListeners: listenerCounts(window),
  },
  externalScripts,
  pendingDevicePromises: [...pendingDevicePromises.entries()].map(
    ([asyncId, stack]) => ({ asyncId, stack }),
  ),
  decodedDeviceConfig,
  decodedDeviceConfigError,
  deviceSourceLoadError,
  deviceRuntimeError,
  deviceRuntimeResult,
  deviceInitReturnType: typeof deviceInitReturn,
  deviceInitResolvedValue,
  deviceInitState,
  feilinKeys: Object.keys(window.FEILIN || {}),
  feilinSeleniumProbe: window.__FEILIN_SELENIUM_PROBE,
  feilinInitSource: typeof window.FEILIN?.initFeiLin === "function"
    ? String(window.FEILIN.initFeiLin).slice(0, 500)
    : undefined,
  initProbe: {
    activityEnabled: simulateActivity,
    activityState: initProbeActivityState,
    challengeEnabled: runChallengeProbe,
    challengeState: initProbeChallengeState,
    enabled: runInitProbe,
    error: initProbeError,
    instanceKeys: initProbeInstance ? Object.keys(initProbeInstance) : [],
    result: initProbeResult,
    successToken: initProbeSuccessToken,
    verificationError: initProbeVerificationError,
    verificationState: initProbeVerificationState,
  },
  decodedDeviceScriptPath: decodedDeviceConfig
    ? internals.deviceConfig.dynamicJsPath(decodedDeviceConfig.version)
    : undefined,
  dynamic: {
    loaded: typeof window.AliyunCaptcha === "function",
    error: dynamicError,
    windowKeys: Object.keys(window).filter((key) => /aliyun|captcha/i.test(key)),
    utilityKeys: Object.keys(window.__ALIYUN_CAPTCHA_UTILS || {}),
    cryptoKeys: Object.keys(window.__ALIYUN_CRYPT || {}),
    prototypeKeys: typeof window.AliyunCaptcha === "function"
      ? Object.getOwnPropertyNames(window.AliyunCaptcha.prototype)
      : [],
    instanceKeys: dynamicInstance ? Object.keys(dynamicInstance) : [],
    instanceError: dynamicInstanceError,
    instanceTimedOut: dynamicInstanceTimedOut,
    successToken: dynamicSuccessToken,
    verificationPayload: dynamicVerificationPayload,
    requests: capturedRequests,
    missingCalls: window.__ALIYUN_MISSING_CALLS,
  },
  captchaConfigKeys: Object.keys(internals.captchaConfig),
  captchaConfig: project(internals.captchaConfig, [
    "API_VERSION",
    "APP_VERSION",
    "PLATFORM",
    "region",
    "verifyType",
    "fallbackCount",
    "timeout",
    "apiServers",
    "cdnServers",
    "captchaVerifyCallback",
  ]),
  captchaVerifyCallbackType: typeof internals.captchaConfig.captchaVerifyCallback,
  deviceConfig: project(internals.deviceConfig, [
    "API_VERSION",
    "APP_VERSION",
    "PLATFORM",
    "APP_NAME",
    "APP_KEY",
    "ACCESS_SEC",
    "secretKey",
    "region",
    "sceneId",
    "prefix",
    "appName",
    "appKey",
    "fallbackCount",
    "timeout",
  ]),
  captchaKeys: internals.captchaKeys,
  deviceKeys: internals.deviceKeys,
  frontConstants: internals.frontConstants,
  captchaEndpoints: internals.captchaEndpoints,
  deviceEndpoints: {
    cn2: internals.deviceEndpoints(undefined, "2.0", "cn"),
    cn3: internals.deviceEndpoints(undefined, "3.0", "cn"),
  },
  apiVersion: internals.apiVersion,
  resolvedVerifyTypes: {
    implicit: internals.resolveVerifyType({ SceneId: sceneId }),
    v2: internals.resolveVerifyType({ SceneId: sceneId, verifyType: "2.0" }),
    v3: internals.resolveVerifyType({ SceneId: sceneId, verifyType: "3.0" }),
  },
  samples: {
    timestamp: internals.timestamp(),
    nonce: internals.nonce(),
    signature: internals.signParams(
      { ...sampleParams },
      internals.captchaKeys.KEY_SECRET,
    ),
    encoded: internals.encodeUrl(sampleParams),
    captchaJsPath: internals.captchaConfig.captchaJsPath(
      sampleInitResult.StaticPath,
    ),
    captchaCssPath: internals.captchaConfig.captchaCssPath(
      sampleInitResult.StaticPath,
    ),
    normalizedInit: internals.normalizeInitResult(
      { ...sampleInitResult },
      internals.captchaConfig,
    ),
    deviceFlag: sampleDeviceFlag,
    deviceLog1Data: sampleDeviceData,
  },
};

const requestActions = capturedRequests.map((request) => {
  try {
    return new URLSearchParams(request.body || "").get("Action") || "";
  } catch {
    return "";
  }
}).filter(Boolean);
let compactError = initProbeVerificationError || initProbeError;
if (initProbeSuccessToken) {
  try {
    const decoded = JSON.parse(
      Buffer.from(initProbeSuccessToken, "base64").toString("utf8"),
    );
    if (
      decoded.sceneId !== sceneId
      || decoded.isSign !== true
      || typeof decoded.certifyId !== "string"
      || !decoded.certifyId
      || typeof decoded.securityToken !== "string"
      || !decoded.securityToken
    ) {
      throw new Error("Aliyun success token has an invalid payload");
    }
  } catch (error) {
    compactError = formatConsoleValue(error);
    initProbeSuccessToken = undefined;
  }
}
if (initProbeSuccessToken && cnblogsCaptchaSourcePath && !cnblogsFingerprint?.fp) {
  compactError = cnblogsFingerprintError || "CNBlogs fingerprint was not generated";
  initProbeSuccessToken = undefined;
}
if (initProbeSuccessToken && assertFingerprintShims && !fingerprintShims.passed) {
  const missing = Object.entries(fingerprintShims)
    .filter(([name, passed]) => name !== "passed" && !passed)
    .map(([name]) => name)
    .join(", ");
  compactError = `Aliyun fingerprint runtime did not exercise: ${missing}`;
  initProbeSuccessToken = undefined;
}
const compactOutput = {
  success: Boolean(initProbeSuccessToken),
  captchaVerifyParam: initProbeSuccessToken,
  fp: cnblogsFingerprint?.fp,
  verificationState: initProbeVerificationState,
  challengeState: initProbeChallengeState,
  requestActions,
  fingerprintShims,
  error: compactError || runtimeConsoleMessages[0],
};
const finalOutput = outputMode === "captcha" ? compactOutput : output;

await new Promise((resolve, reject) => {
  process.stdout.write(`${JSON.stringify(finalOutput, null, 2)}\n`, (error) => {
    if (error) reject(error);
    else resolve();
  });
});
process.exit(0);
