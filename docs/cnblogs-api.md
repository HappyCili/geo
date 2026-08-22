# 博客园文章发布接口文档

## 1. 概述

本文档说明本服务对外提供的博客园（`cnblogs`）文章发布接口。服务负责读取已配置的博客园媒体账号和登录凭据，并将文章提交至博客园；调用方不需要传递 Cookie、`XSRF-TOKEN`、博客园会话 ID 或分类。

- 服务名称：媒体文章发布服务
- 服务版本：`2.0.0`
- 默认地址：`http://127.0.0.1:8000`
- 数据格式：请求和响应均为 `application/json`，除查询参数接口外
- 交互文档：启动服务后可访问 `GET /docs`

博客园发布前建议先调用“查询发布要求”接口，确认账号属于博客园，再调用发布接口。

```text
GET  /cnblogs/articles/publish/requirements
POST /cnblogs/articles/publish
```

## 2. 运行前配置

服务启动前需配置数据库和 Redis 连接。媒体账号保存在数据库中。

```sh
export DB_HOST='127.0.0.1'
export DB_PORT='3306'
export DB_USER='USER'
export DB_PASSWORD='PASSWORD'
export DB_NAME='DATABASE'
export DB_CHARSET='utf8mb4'
export REDIS_URL='redis://127.0.0.1:6379/0'

# 可选回退：媒体账号表没有 account/password 时用于自动登录。
export CNBLOGS_USERNAME='USERNAME'
export CNBLOGS_PASSWORD='PASSWORD'

.venv/bin/python -m uvicorn app.main:app --reload
```

登录凭据优先从 `tb_medium_account.account/password` 读取；两列任一为空时，服务使用
`CNBLOGS_USERNAME/CNBLOGS_PASSWORD` 回退。Cookie 失效后，服务会在同一个发布请求内登录、
持久化新 Cookie，并重试原文章发布一次。若博客园拒绝验证码 token，登录脚本会自动以全新
登录会话重新获取加密参数和验证码配置，默认最多尝试两次；可通过 `--captcha-attempts` 调整
为 1 至 3 次。验证码运行时会自动触发 Aliyun 复选挑战、等待成功回调并将返回 token 与
本地生成的指纹一同提交。

博客园发布不要求 `category`。省略该字段时，服务不查询分类映射，并向博客园提交
`categoryIds: null`。为兼容已有调用，也可显式传入已配置的分类名；此时分类名必须存在于
`tb_medium_platform_category`，并且对应记录为启用、未删除状态。博客园的 `category_value`
是一个或多个逗号分隔的数字分类 ID，例如：

```sql
INSERT INTO tb_medium_platform_category (platform, category_name, category_value)
VALUES ('cnblogs', '行业资讯', '12,34');
```

显式传入分类时，服务会将 `12,34` 转换为博客园所需的 `[12, 34]`。分类 ID 为空或包含非数字时，发布请求返回 `422`。

## 3. 通用约定

### 3.1 账号约束

`account_id` 指向的媒体账号必须满足以下条件：

- 账号存在；
- 账号状态为启用；
- 账号未删除；
- 账号平台为 `cnblogs`。

接口路径与账号平台不一致时，服务不会向博客园发起发布请求，并返回 `422 platform_mismatch`。

### 3.2 业务错误格式

业务错误统一使用如下结构：

```json
{
  "detail": {
    "code": "错误代码",
    "message": "错误说明"
  }
}
```

请求字段不符合 FastAPI/Pydantic 的基础校验时，响应为标准 `422` 校验错误，`detail` 为错误项数组。

## 4. 查询发布要求

查询指定博客园账号是否可用于发布。`category` 可省略；当前博客园实现不需要额外平台字段，因此成功响应中的 `fields` 固定为空数组。

### 请求

```http
GET /cnblogs/articles/publish/requirements?account_id=5
```

| 参数 | 位置 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `account_id` | query | integer | 是 | 大于 `0` | 博客园媒体账号 ID |
| `category` | query | string | 否 | 传入时为 1 至 128 个字符，去除首尾空白后不可为空 | 可选的已配置业务分类名 |

### 成功响应

状态码：`200 OK`

```json
{
  "account_id": 5,
  "platform": "cnblogs",
  "category": null,
  "publishable": true,
  "captcha_required": false,
  "fields": []
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_id` | integer | 请求的账号 ID |
| `platform` | string | 固定为 `cnblogs` |
| `category` | string or null | 省略时为 `null`；传入时为去除首尾空白后的分类名 |
| `publishable` | boolean | 当前发布器是否允许发布；博客园当前为 `true` |
| `captcha_required` | boolean | 当前是否要求调用方补充验证码；博客园当前为 `false` |
| `fields` | array | 平台附加字段定义；博客园当前为空数组 |

### 调用示例

```sh
curl -G 'http://127.0.0.1:8000/cnblogs/articles/publish/requirements' \
  --data-urlencode 'account_id=5'
```

## 5. 发布文章

服务将文章发布到账号所属的博客园。若检测到登录态失效，服务会尝试刷新博客园登录凭据，并在可安全重试时重新提交一次发布请求。

### 请求

```http
POST /cnblogs/articles/publish
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | integer | 是 | 大于 `0` | 博客园媒体账号 ID |
| `title` | string | 是 | 1 至 200 个字符；去除首尾空白后不可为空 | 文章标题 |
| `category` | string | 否 | 传入时为 1 至 128 个字符，去除首尾空白后不可为空 | 可选的数据库分类名 |
| `content` | string | 是 | 去除空白后不可为空 | 文章正文；内容原样交由博客园处理 |
| `content_type` | string | 否 | `markdown` 或 `html`，默认 `markdown` | 正文格式；决定博客园的 Markdown 标记 |
| `platform_fields` | object | 否 | 键不可为空；值为字符串或字符串数组 | 平台扩展字段。博客园当前不消费此对象，建议传 `{}` |

### 请求示例

```json
{
  "account_id": 5,
  "title": "物流 AI 助手的落地实践",
  "content": "# 物流 AI 助手的落地实践\n\n这里是 Markdown 正文。",
  "content_type": "markdown",
  "platform_fields": {}
}
```

```sh
curl -X POST 'http://127.0.0.1:8002/cnblogs/articles/publish' \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": 2,
    "title": "物流 AI 助手的落地实践",
    "content": "# 物流 AI 助手的落地实践\\n\\n这里是 Markdown 正文。",
    "content_type": "markdown",
    "platform_fields": {}
  }'
```

### 成功响应

状态码：`200 OK`

```json
{
  "account_id": 5,
  "platform": "cnblogs",
  "success": true,
  "http_status": 200,
  "article_url": "https://www.cnblogs.com/example/p/12345678",
  "message": "CNBlogs 发布成功"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_id` | integer | 请求的账号 ID |
| `platform` | string | 固定为 `cnblogs` |
| `success` | boolean | 成功时为 `true` |
| `http_status` | integer | 博客园上游发布接口返回的 HTTP 状态码 |
| `article_url` | string or null | 从博客园响应的 `url`、`postUrl`、`link` 或嵌套 `data` 中解析的文章地址；上游未返回时为 `null` |
| `message` | string | 成功时为 `CNBlogs 发布成功` |

## 6. 错误码

| HTTP 状态 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `account_not_found` | 媒体账号不存在 |
| `409` | `account_unavailable` | 账号未启用、已删除或未配置可用登录凭据 |
| `409` | `login_failed` | 已执行登录但博客园未接受账号密码 |
| `409` | `login_expired` | 登录态失效且刷新或重试后仍不可用 |
| `409` | `captcha_required` | 自动验证码重试次数已耗尽 |
| `422` | `platform_mismatch` | 账号不是博客园账号 |
| `422` | `invalid_request` | 显式传入的分类未配置，或分类 ID 为空、非数字等业务校验失败 |
| `422` | `invalid_platform_fields` | 平台字段校验失败；当前博客园不要求扩展字段 |
| `422` | 标准校验错误 | 参数缺失、类型不匹配、字符串长度或枚举值不合法 |
| `502` | `upstream_publish_error` | 博客园发布请求失败、缺少 `XSRF-TOKEN` 或上游未确认发布成功 |
| `502` | `login_network_error` | 登录刷新网络请求失败 |
| `502` | `login_protocol_error` | 登录刷新响应无效 |
| `503` | `refresh_unavailable` | 登录刷新服务暂不可用 |
| `503` | `persistence_unavailable` | 登录刷新状态或凭据持久化失败 |
| `501` | `unsupported_platform` | 平台发布器未注册 |
| `500` | `internal_error` | 未分类的服务端异常 |

业务错误示例：

```json
{
  "detail": {
    "code": "platform_mismatch",
    "message": "路由平台与媒体账号平台不一致"
  }
}
```

字段校验错误示例：

```json
{
  "detail": [
    {
      "type": "greater_than",
      "loc": ["body", "account_id"],
      "msg": "Input should be greater than 0",
      "input": 0,
      "ctx": {
        "gt": 0
      }
    }
  ]
}
```

## 7. 调用流程

1. 调用 `GET /cnblogs/articles/publish/requirements`，确认响应中的 `platform` 为 `cnblogs` 且 `publishable` 为 `true`。
2. 调用 `POST /cnblogs/articles/publish` 提交文章，无需传递 `category`。
3. 以响应的 `success`、`http_status` 与 `article_url` 作为发布结果；`article_url` 为 `null` 时可依据标题在博客园后台确认文章。

项目还提供脚本 `scripts/publish_cnblogs_via_api.py`。该脚本会读取根目录的 `摘星货蚁物流AI助手推荐.md`，先执行发布要求检查，再调用发布接口：

```sh
PUBLISH_API_BASE_URL='http://127.0.0.1:8000' \
.venv/bin/python scripts/publish_cnblogs_via_api.py 5
```
