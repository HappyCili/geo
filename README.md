# 媒体文章发布服务

## 环境变量

```sh
export DB_HOST='127.0.0.1'
export DB_PORT='3306'
export DB_USER='USER'
export DB_PASSWORD='PASSWORD'
export DB_NAME='DATABASE'
export DB_CHARSET='utf8mb4'
export REDIS_URL='redis://127.0.0.1:6379/0'
export OBSERVABILITY_ACCOUNT_SALT='STABLE_SECRET_AT_LEAST_16_CHARACTERS'
# 可选：默认 DEBUG，输出完整请求 URL、headers、body 和 response.text。
# export APP_LOG_LEVEL='INFO'
# 可选：当 tb_medium_account.account/password 未配置时用于 CNBlogs 自动登录。
# export CNBLOGS_USERNAME='USERNAME'
# export CNBLOGS_PASSWORD='PASSWORD'
```

`OBSERVABILITY_ACCOUNT_SALT` 用于生成日志中的账号关联值；未设置时使用数据库密码作为
HMAC 密钥。DEBUG 请求日志会按原文记录 URL、headers、body 和上游 `response.text`，
其中可能包含账号、密码、Cookie、Token 或 Session，应按敏感日志管理服务输出。

## Hepan 登录刷新

当 `tb_medium_account.platform` 为 `hepan` 时，登录刷新直接从同一行读取 `account` 与
`password`。`account` 必须是已绑定的手机号；服务会以 Hepan 的“手机号 / 密码登录”模式换取
新的 Cookie 并写回数据库和 Redis。登录页出现宝塔人机验证时，服务会在同一 HTTP 会话中执行
页面提供的验证请求后重试登录页。Hepan 不使用环境变量登录凭据回退，避免跨平台误用账号。

## Lieju 登录刷新

当 `tb_medium_account.platform` 为 `lieju` 时，刷新从同一行读取手机号 `account` 和密码
`password`。服务先进入 `https://hz.lieju.com/` 发现当前登录页，再用手机号填充当前的
用户名/密码表单；Node.js 仅执行页面返回的 WAF Cookie 计算，不接收账号密码。登录成功后，
服务会预热 `post.lieju.com` 的发布域，将 `lieju_passport`、认证 Cookie 与发布域 WAF Cookie
一起写回 MySQL 和 Redis。Lieju 不使用环境变量凭据回退。

执行 [schema/tb_medium_platform_category.sql](schema/tb_medium_platform_category.sql)，并为每个平台维护分类记录。例如：

```sql
INSERT INTO tb_medium_platform_category (platform, category_name, category_value)
VALUES ('hepan', '行业资讯', '121');
```

CNBlogs 的 `category_value` 使用一个或多个逗号分隔的数字分类 ID，例如 `12,34`。
Lieju 使用 `city_id/fid`，并以完整分类路径作为 `category_name`。SQL 文件已经包含
安顺城市 `95` 的 234 条分类映射，例如 `生活服务/家政 -> 95/71`。

## 启动

```sh
.venv/bin/python -m uvicorn app.main:app --reload
```

## 发布接口

接口使用 `async def`/`await` 完成数据库查询和平台 HTTP 请求，响应会等待发布完成后返回。

```sh
curl -X POST http://127.0.0.1:8000/hepan/articles/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": 1,
    "title": "文章标题",
    "category": "行业资讯",
    "content": "# Markdown 正文",
    "content_type": "markdown",
    "platform_fields": {}
  }'
```

`content_type` 可省略，默认 `markdown`；可传 `html`。Lieju 会将两种格式统一转换为
保留段落换行的纯文本后提交。
`platform_fields` 可省略。Lieju 根据分类页表单动态读取该对象中的平台字段；字段值为
字符串，复选字段为字符串数组。

### 单参数 API 调用脚本

启动服务后，`scripts/publish_lieju_via_api.py` 的唯一命令行参数是媒体账号 ID。它读取
项目根目录的 `摘星货蚁物流AI助手推荐.md` 作为文章，并调用 `POST /lieju/articles/publish`：

```sh
export LIEJU_ZHAIXING_PHONE='13800000000'
export LIEJU_ZHAIXING_CONTACT='联系人'
.venv/bin/python scripts/publish_lieju_via_api.py 3
```

默认接口地址为 `http://127.0.0.1:8000`；服务不在本机时设置
`PUBLISH_API_BASE_URL`。可选的 `LIEJU_ZHAIXING_REGION`、
`LIEJU_ZHAIXING_SERVICE_TYPE` 和 `LIEJU_ZHAIXING_ADDRESS` 分别覆盖默认的
区域、服务类型和地址。

### CNBlogs 单参数 API 调用脚本

`scripts/publish_cnblogs_via_api.py` 同样只接收账号 ID，并在发布前确认该账号属于
CNBlogs：

```sh
.venv/bin/python scripts/publish_cnblogs_via_api.py 5
```

脚本默认使用已配置映射的 `行业资讯` 分类；需要其他分类时设置 `CNBLOGS_CATEGORY`。
文章同样读取 `摘星货蚁物流AI助手推荐.md`，接口地址使用 `PUBLISH_API_BASE_URL` 或本机默认值。

### CNBlogs 登录接口脚本

`scripts/cnblogs_signin.py` 通过纯 HTTP 完成登录页初始化、验证码 token 获取、
AES-GCM/RSA-OAEP 加密和最终登录请求，不启动浏览器。验证码配置只取自博客园当前
`/api/captcha` 响应；本地 Node 运行时执行官方 `AliyunCaptcha.js`，每次登录尝试中的
网络请求经由同一个 `httpx.Client` 转发。

```sh
printf '%s\n' '{"username":"USERNAME","password":"PASSWORD"}' \
  | .venv/bin/python scripts/cnblogs_signin.py --submit --credentials-stdin
```

默认 dry-run 只构造加密请求，不签发短时验证码：

```sh
.venv/bin/python scripts/cnblogs_signin.py
```

运行需要 Node.js 和本地
`artifacts/cnblogs-signin/aliyun/AliyunCaptcha.js`。官方动态 FeiLin/cx 脚本从
`https://g.alicdn.com` 加载；其初始化、设备 `Log2/Log3` 和
`VerifyCaptchaV3` 请求仅允许发往 `*.aliyuncs.com`。脚本不调用 Google
reCAPTCHA、外部验证码服务，也不读取验证码 JSON 文件。博客园 bundle
`artifacts/cnblogs-signin/711-es2015.js` 在同一 Node 沙箱内生成登录必需的本地
`fp` 字段，不产生额外网络请求。可用
`--captcha-timeout` 调整单次验证码运行时总超时，`--captcha-attempts` 控制最多三次
全新登录会话重试（默认两次），`--node-binary` 指定 Node 路径。仅当博客园明确返回
`captcha_required` 时才会重新初始化会话、获取新验证码 token 并重试；其他登录错误不会
重复提交。运行时会自动触发 Aliyun 复选挑战并等待 `success` 回调，再将返回的
`captchaVerifyParam` 与本地生成的 `fp` 写入加密登录请求。

## Lieju 发布

先查询指定账号与分类当前需要的字段：

```sh
curl -G http://127.0.0.1:8000/lieju/articles/publish/requirements \
  --data-urlencode 'account_id=3' \
  --data-urlencode 'category=生活服务/家政'
```

Lieju 发布示例：

```json
{
  "account_id": 3,
  "title": "专业家政服务信息",
  "category": "生活服务/家政",
  "content": "提供长期、可靠、专业的家政服务内容。",
  "content_type": "markdown",
  "platform_fields": {
    "zone_id": "827",
    "leibie": "1",
    "dizhi": "西秀区测试路一号",
    "mobphone": "13800000000",
    "linkman": "联系人"
  }
}
```

选项字段必须使用需求查询接口返回的 `value`。

正文中的 Markdown 图片和 HTML `<img src>` 会自动提取、去重并下载，再作为
`local_fileN` 与 `photodb[N]/ftype[N]` 字段一起提交到 Lieju 的
`POST /{city_id}/{fid}?action=postnew` multipart 发布接口。图片源仅支持 HTTP/HTTPS
地址，支持 JPG、PNG 或 GIF。原图超过 1MB 时会自动压缩至小于 1MB 后上传；原图下载
上限为 20MB，压缩后仍超限时会返回内容字段校验错误。图片数按当前发布表单返回的限额执行。

### 腾讯验证码配置

发布表单已经带有有效的 `atc_yzm`、`postdb[ticket]` 和 `postdb[randstr]` 时，服务会
直接提交这些字段。字段为空时，服务通过 Python HTTP 获取当前 prehandle、背景图和 TDC
脚本，由超级鹰题型 `9602` 识别缺口，再由内嵌 QuickJS 执行脚本自身的加密实现。整个发布
流程不启动 Chrome、Playwright 或 Node，成功结果中的 ticket/randstr 会立即合并进发布表单。

将下面的值放入项目根目录 `.env` 或进程环境：

```sh
LIEJU_CAPTCHA_BROWSER_ENABLED=false
LIEJU_TDC_PAYLOAD_PATH=/absolute/path/to/assets/lieju/tdc-browser-payload.json
LIEJU_TDC_DYNAMIC=true
LIEJU_TDC_RUNTIME_TIMEOUT_SECONDS=20
LIEJU_TDC_FEATURE_FLAGS=qf_7Pf__H
LIEJU_TDC_IS_NEW_ENTRY=1
LIEJU_TDC_POW_TIMEOUT_MS=30000
LIEJU_CAPTCHA_TIMEOUT_SECONDS=30
LIEJU_CHAOJIYING_USERNAME=<username>
LIEJU_CHAOJIYING_PASSWORD=<password>
LIEJU_CHAOJIYING_SOFT_ID=<software-id>
LIEJU_CHAOJIYING_TYPE=9602
LIEJU_CHAOJIYING_POINT_INDEX=0
LIEJU_CHAOJIYING_API_BASE=https://upload.chaojiying.net
LIEJU_CHAOJIYING_TIMEOUT_SECONDS=30
LIEJU_CAPTCHA_MAX_ATTEMPTS=2
```

纯 Python 模式从当前 prehandle 的 `dyn_show_info` 读取背景图逻辑尺寸、拼图块尺寸、初始位置
和轨道范围，并按腾讯控件宽度生成与答案一致的滑动轨迹。模板只提供浏览器指纹字段结构；
会话、时间、URL、轨迹、性能数据、PoW、`eks` 和 `collect` 均按当前请求实时生成。

如需固定历史脚本，仍可关闭动态模式并配置静态 TDC profile：

```sh
LIEJU_TDC_PAYLOAD_PATH=/absolute/path/to/tdc-payload.json
LIEJU_TDC_DYNAMIC=false
LIEJU_TDC_SHA256=<exact-sha256-of-the-selected-tdc-script>
LIEJU_TDC_KEY=<16-byte-tdc-xtea-key-or-32-hex-characters>
LIEJU_CAPTCHA_ANSWER=510,181

LIEJU_TDC_FEATURE_FLAGS=<captured-ft-value>
LIEJU_TDC_IS_NEW_ENTRY=0
LIEJU_TDC_POW_TIMEOUT_MS=30000
LIEJU_CAPTCHA_TIMEOUT_SECONDS=15
LIEJU_CAPTURE_REQUEST_PATH=/absolute/path/to/CAPTURE_REQUEST.json
LIEJU_TDC_CALLBACK=callback
```

静态 profile 与 prehandle 返回的脚本 SHA 必须一致。任一求解器配置完成后，需求查询仍返回
`captcha_required=true`，但同时返回 `publishable=true`；缺少必填配置时发布接口返回 `409`。
