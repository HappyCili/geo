# CNBlogs 登录接口分析

## 最终接口

- 方法：`POST`
- URL：`https://account.cnblogs.com/api/sign-in?returnUrl=https:%2F%2Fvip.cnblogs.com%2Fmy`
- `Content-Type`：`application/vnd.cnblogs.em`
- `X-XSRF-TOKEN`：来自同一会话 `GET /api/options` 返回的 `xsrfToken`
- Body：二进制 MessagePack 加密信封，不是 JSON 或表单

## 参数来源

| 参数 | 来源 | 最终位置 |
| --- | --- | --- |
| `username` | 密码登录页“登录用户名 / 邮箱”输入框 | 加密 JSON Body |
| `password` | 密码输入框 | 加密 JSON Body |
| `captcha` | 点击登录后 CAPTCHA provider 的成功回调 | 加密 JSON Body |
| `isRemember` | “记住我”复选框，默认 `true` | 加密 JSON Body |
| `returnUrl` | 当前页面 `window.location.search` | POST URL 查询串 |

当前 CAPTCHA provider 顺序为 AliyunCaptchaV2、reCAPTCHA。前者产生
`{"captchaVerifyParam": "..."}`，后者产生
`{"g-recaptcha-response": "..."}`。交互 provider 成功后还会执行当前
`frictionlessProviders` 中的 reCAPTCHA v3（action 为 `SignIn`），将
`{"g-recaptcha-response-v3": "..."}` 合并到同一个 `captcha` 字段。
无论可选 frictionless provider 是否就绪，博客园 bundle 都会用 Fingerprint2 生成
`{"fp": "..."}` 并合并到 `captcha`。本地脚本执行 AliyunCaptchaV2 和官方 `fp`
生成逻辑，不请求 Google 或第三方服务。实测仅提交 `captchaVerifyParam + fp` 已进入
正常的账号密码校验分支；缺少 `fp` 时服务端返回通用“请求异常”。

## AliyunCaptchaV2 参数与顺序

1. `prefix/SceneId` 分别来自博客园 `/api/captcha` 的
   `aliyunCaptchaV2.prefix/sceneId`，`region` 固定为博客园使用的 `cn`。
2. 官方 `AliyunCaptcha.js` 根据存在的 `success` 回调选择 `verifyType=3.0`，生成
   `InitCaptchaV3`。Body 包含 `AaduaneId`、签名版本、UTC `Timestamp`、随机
   `SignatureNonce`、`SceneId`、`Language`、`Mode` 和初始 `DeviceData`；
   `Signature` 由官方 `Ce` 函数按排序后的参数执行 HMAC-SHA1。
3. 初始化响应返回短时 `CertifyId`、`StaticPath` 和加密 `DeviceConfig`。后者由官方
   `me` 解密，`version` 决定 FeiLin 脚本路径，`StaticPath` 决定 cx 动态脚本路径。
4. FeiLin 通过 iframe、Storage、canvas/WebGL、字体、媒体能力、Navigator、Screen
   和 DOM 特征生成设备数据，依次执行设备 `Log2/Log3`，得到 `DeviceToken`。
5. 动态脚本采集登录前的 `keyup/focus/mousemove/scroll` 事件，组装
   `CaptchaVerifyParam={sceneId,certifyId,deviceToken,data}`，再签名请求
   `VerifyCaptchaV3`。
6. 已验证成功响应为 `VerifyCode=T001`、`VerifyResult=true`，并返回
   `securityToken`。官方 `onBizSuccess` 最终生成：
   `base64(JSON({certifyId,sceneId,isSign:true,securityToken}))`。
7. 博客园 `711-es2015.js` 使用 Fingerprint2 计算 128-bit hash，将 hash 字节按随机
   排列编码为 `fp`；该字段与 `captchaVerifyParam` 一起进入登录加密信封。
8. 若无感结果为 `F001/F002`，官方流程会重新初始化 `CHECK_BOX`；分析器已验证其
   `mousedown -> mouseup -> VerifyCaptchaV3` 调用路径。

成功样本的网络动作顺序为：
`InitCaptchaV3 -> UploadLog -> Log2 -> Log3 -> VerifyCaptchaV3`。动态脚本只从
`g.alicdn.com` 加载，API 只允许 `*.aliyuncs.com`。Node 自身不联网，每个请求都通过
JSON-RPC 交给 Python 的同一个 `httpx.Client`。

## 调用顺序

1. `GET /signin?returnUrl=...`，建立页面会话。
2. `GET /api/options`，取得 `xsrfToken` 和 Base64 + MessagePack 编码的
   `[keyId, RSA SPKI publicKey, serverTimestamp]`。
3. `GET /api/sign-in?returnUrl=...`，取得登录页选项。
4. `GET /api/captcha?timestamp=TIMESTAMP&action=SignIn`，初始化验证码。
5. 用户点击登录，Angular 校验 `username/password`。
6. CAPTCHA 成功回调返回交互验证码对象。
7. 执行 frictionless CAPTCHA，并将结果合并到验证码对象。
8. 组装 `{captcha, username, password, isRemember}`。
9. 使用 `/api/options` 的动态参数生成二进制信封。
10. 带 `X-XSRF-TOKEN` 请求头调用 `POST /api/sign-in?returnUrl=...`。

## 加密信封

1. JSON 使用 UTF-8 编码，无多余空格。
2. 随机生成 16 字节 AES key 和 12 字节 IV。
3. `additionalData` 为 `Date.now() + timeOffset` 的 8 字节大端整数。
4. AES-128-GCM 加密 JSON，认证标签截取为 96 bit。
5. MessagePack 编码 `[aesKey, iv, tag, additionalData]`。
6. 使用 RSA-OAEP-SHA256 加密上一步结果。
7. MessagePack 编码 `[1, keyId, rsaCiphertext, aesCiphertext]`。
8. 浏览器发送编码器底层 `ArrayBuffer`。本次样本有效数据 1903 字节，
   线长 2048 字节，尾部为 145 个零字节。

## 源码证据

- `583-es2015.js` byte `1826`：`/api/sign-in` 服务基址。
- `583-es2015.js` byte `1945`：POST 时拼接 `window.location.search`。
- `583-es2015.js` byte `9355`：`username/password/isRemember` 表单定义。
- `583-es2015.js` byte `11296`：合并 `{captcha, ...passwordForm, ...commonForm}`。
- `517-es2015.js` byte `9040`：二进制 Content-Type。
- `517-es2015.js` byte `9353`：AES-GCM、RSA-OAEP 和 MessagePack 信封入口。
- `main-es2015.js` byte `653500`：`GET /api/options`。
- `main-es2015.js` byte `666368`：设置 `X-XSRF-TOKEN`。
- `711-es2015.js` byte `148638`：`GET /api/captcha`。

## Python 脚本

仅构造请求，不提交（不会调用验证码求解器）：

```sh
venv/bin/python scripts/cnblogs_signin.py
```

真实提交会在同一 HTTP 会话中执行官方 AliyunCaptchaV2 初始化、设备指纹上报和
无感验证，再构造并提交加密信封：

```sh
export CNBLOGS_USERNAME='USERNAME'
export CNBLOGS_PASSWORD='PASSWORD'
.venv/bin/python scripts/cnblogs_signin.py --submit
```

脚本不启动浏览器，也不读取验证码文件。Python 从 `/api/captcha` 动态读取
`aliyunCaptchaV2.prefix/sceneId`，本地 Node 沙箱执行官方脚本；所有阿里云 HTTP
请求通过 stdin/stderr RPC 回到同一个 `httpx.Client`。成功响应中的
`certifyId/sceneId/securityToken` 由官方回调编码为 `captchaVerifyParam`，随后立即
放进登录加密信封。网络白名单为 `account.cnblogs.com`、`g.alicdn.com` 和
`*.aliyuncs.com`。

脚本不会打印 XSRF 值、密码或 CAPTCHA 内容。
