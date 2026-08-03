# Hepan 网文章发布接口文档

## 1. 概述

本文档说明媒体文章发布服务对 Hepan 网（平台标识：`hepan`）提供的接口。服务从数据库读取
已启用的 Hepan 媒体账号、分类映射和登录态，将文章转换为 Hepan 门户文章表单并提交。
调用方只传递 `account_id`，不需要也不应该在请求体中传递 Cookie、密码或 `formhash`。

- 服务名称：媒体文章发布服务
- 服务版本：`2.0.0`
- 默认地址：`http://127.0.0.1:8000`
- 请求格式：`application/json`（查询发布要求接口使用 URL 查询参数）
- 交互文档：启动服务后访问 `GET /docs`

接口如下：

```text
GET  /hepan/articles/publish/requirements
POST /hepan/articles/publish
```

推荐先查询发布要求，再提交文章。Hepan 当前没有额外平台字段，因此查询接口的 `fields`
固定为空数组；该接口仍会校验账号已有可用登录态。

## 2. 运行前配置

服务需要 MySQL 和 Redis。启动前至少配置以下环境变量：

```sh
export DB_HOST='127.0.0.1'
export DB_PORT='3306'
export DB_USER='USER'
export DB_PASSWORD='PASSWORD'
export DB_NAME='DATABASE'
export DB_CHARSET='utf8mb4'
export REDIS_URL='redis://127.0.0.1:6379/0'

.venv/bin/python -m uvicorn app.main:app --reload
```

### 2.1 媒体账号

`account_id` 对应 `tb_medium_account.id`，账号必须满足：

- `status = 1`；
- ``del`` = `0`；
- `platform = 'hepan'`；
- `sync_status = 1` 且存在可解析 Cookie，或允许服务通过 Hepan 登录提供器刷新登录态。

刷新登录态时，服务从该账号的 `account` 和 `password` 字段读取手机号/密码。登录成功后，
Cookie 会写回账号并缓存到 Redis。登录 Cookie 只在服务内部使用，不会通过接口响应返回。

### 2.2 分类映射

分类名必须在 `tb_medium_platform_category` 中存在、启用且未删除。Hepan 的
`category_value` 必须是一个非空的单个分类 ID，不能包含逗号：

```sql
INSERT INTO tb_medium_platform_category
    (platform, category_name, category_value)
VALUES
    ('hepan', '行业资讯', '121');
```

`'121'` 仅为示例值，应替换为 Hepan 后台实际分类 ID。未配置分类、分类值为空或配置为
`121,122` 时，发布请求返回 `422`。

## 3. 通用约定

### 3.1 账号与路由

路由中的平台必须与账号的 `platform` 一致。例如使用 `/hepan/...` 时，账号必须是 Hepan
账号；平台不一致时不会请求 Hepan 上游。

### 3.2 业务错误格式

可映射的业务错误统一返回：

```json
{
  "detail": {
    "code": "错误代码",
    "message": "错误说明"
  }
}
```

请求体的类型、长度、枚举校验失败时，返回 FastAPI 标准 `422` 响应，`detail` 为错误项数组。

### 3.3 内容格式

`content_type` 支持：

- `markdown`：默认值。服务使用 `extra` 和 `sane_lists` 扩展转换为 HTML5，再提交给 Hepan；
- `html`：正文原样提交，不再次转换。

## 4. 查询发布要求

### 请求

```http
GET /hepan/articles/publish/requirements?account_id=5&category=%E8%A1%8C%E4%B8%9A%E8%B5%84%E8%AE%AF
```

| 参数 | 位置 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `account_id` | query | integer | 是 | 大于 `0` | Hepan 媒体账号 ID |
| `category` | query | string | 是 | 1 至 128 个字符；去除首尾空白后不可为空 | 已配置的业务分类名 |

### 成功响应

状态码：`200 OK`

```json
{
  "account_id": 5,
  "platform": "hepan",
  "category": "行业资讯",
  "publishable": true,
  "captcha_required": false,
  "fields": []
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_id` | integer | 请求的账号 ID |
| `platform` | string | 固定为 `hepan` |
| `category` | string | 去除首尾空白后的分类名 |
| `publishable` | boolean | 当前发布器是否允许发布；Hepan 当前为 `true` |
| `captcha_required` | boolean | 当前是否要求调用方补充验证码；Hepan 当前为 `false` |
| `fields` | array | 平台附加字段定义；Hepan 当前为空数组 |

### 调用示例

```sh
curl -G 'http://127.0.0.1:8000/hepan/articles/publish/requirements' \
  --data-urlencode 'account_id=5' \
  --data-urlencode 'category=行业资讯'
```

## 5. 发布文章

### 请求

```http
POST /hepan/articles/publish
Content-Type: application/json
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | integer | 是 | 大于 `0` | Hepan 媒体账号 ID |
| `title` | string | 是 | 1 至 200 个字符；去除首尾空白后不可为空 | 文章标题 |
| `category` | string | 是 | 1 至 128 个字符；去除首尾空白后不可为空 | 数据库中已配置的分类名 |
| `content` | string | 是 | 去除空白后不可为空 | 文章正文 |
| `content_type` | string | 否 | `markdown` 或 `html`；默认 `markdown` | 正文格式 |
| `platform_fields` | object | 否 | 键不可为空；值为字符串或字符串数组 | 预留的平台扩展字段，Hepan 当前不消费 |

请求示例：

```json
{
  "account_id": 5,
  "title": "物流 AI 助手的落地实践",
  "category": "行业资讯",
  "content": "# 物流 AI 助手的落地实践\n\n这里是 Markdown 正文。",
  "content_type": "markdown",
  "platform_fields": {}
}
```

```sh
curl -X POST 'http://127.0.0.1:8002/hepan/articles/publish' \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": 4,
    "title": "物流 AI 助手的落地实践",
    "category": "行业资讯",
    "content": "# 物流 AI 助手的落地实践",
    "content_type": "markdown",
    "platform_fields": {}
  }'
```

### 成功响应

状态码：`200 OK`

```json
{
  "account_id": 5,
  "platform": "hepan",
  "success": true,
  "http_status": 200,
  "article_url": "https://www.hepan.com/portal.php?op=edit&aid=123",
  "message": "Hepan 发布成功"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_id` | integer | 请求的账号 ID |
| `platform` | string | 固定为 `hepan` |
| `success` | boolean | 成功时为 `true` |
| `http_status` | integer | Hepan 上游发布响应的 HTTP 状态码 |
| `article_url` | string or null | 从成功页中提取的 `op=edit&aid=...` 链接；通常是后台编辑链接，不保证是公开阅读链接 |
| `message` | string | 成功时为 `Hepan 发布成功` |

## 6. 服务到 Hepan 的请求流程

调用发布接口后，服务在同一个登录会话中执行：

1. `GET https://www.hepan.com/portal.php?mod=portalcp&ac=article&catid={category_value}`，
   携带服务内部保存的 Cookie；
2. 从 `form#articleform input[name=formhash]` 读取当前表单令牌；
3. 将标题、分类 ID、正文、`formhash` 等字段组装为 `multipart/form-data`，向
   `POST https://www.hepan.com/portal.php?mod=portalcp&ac=article` 提交；
4. 跟随重定向，检查响应文本是否包含“发布文章成功”，并提取 `op=edit&aid=...` 链接。

服务会自动生成 multipart boundary，不应在调用方手动设置 `Content-Type`。如果 GET 或 POST
返回登录页/登录重定向，会按登录态过期处理；调用编排层在可安全重试时刷新 Cookie 并重试一次。

## 7. 错误码

| HTTP 状态 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `account_not_found` | 媒体账号不存在 |
| `409` | `account_unavailable` | 账号未启用或已删除 |
| `409` | `login_expired` | Cookie 无效、未配置 Cookie，或刷新后登录态仍失效 |
| `409` | `captcha_required` | 登录刷新被 Hepan 要求验证码 |
| `422` | `platform_mismatch` | 账号不是 Hepan 账号 |
| `422` | `invalid_request` | 未配置分类、分类 ID 为空/包含逗号，或发布器配置不满足约束 |
| `422` | 标准校验错误 | 参数缺失、类型不匹配、字符串为空/超长或 `content_type` 非法 |
| `502` | `upstream_publish_error` | Hepan 请求失败、缺少 `formhash` 或上游未确认发布成功 |
| `502` | `login_network_error` | 登录刷新网络失败 |
| `502` | `login_protocol_error` | 登录刷新响应格式无效 |
| `503` | `refresh_unavailable` | Redis 刷新锁或刷新等待不可用 |
| `503` | `persistence_unavailable` | 登录态持久化失败 |
| `501` | `unsupported_platform` | 平台发布器未注册 |
| `500` | `internal_error` | 未分类的服务端异常 |

示例：分类配置为多 ID 时：

```json
{
  "detail": {
    "code": "invalid_request",
    "message": "Hepan 分类 行业资讯 必须配置为单个分类 ID"
  }
}
```

## 8. 测试用例

测试使用本地假 HTTP 客户端，不访问真实 Hepan，也不需要真实 Cookie、数据库或 Redis。
项目虚拟环境中执行：

```sh
.venv/bin/python -m pytest tests/test_hepan_api.py tests/test_api.py -q
```

覆盖矩阵：

| 用例 | 验证点 | 预期 |
| --- | --- | --- |
| `test_hepan_markdown_content_is_converted_to_html` | Markdown 转 HTML5 | multipart 的 `content` 为转换后的 HTML |
| `test_hepan_html_content_is_submitted_unchanged` | HTML 内容透传 | multipart 的 `content` 与输入完全一致 |
| `test_hepan_rejects_multiple_category_ids` | 单分类 ID 约束 | 抛出配置错误且不发起上游请求 |
| `test_hepan_requires_formhash_before_publish` | 表单令牌解析 | 缺少 `formhash` 返回上游发布错误 |
| `test_hepan_login_redirect_is_reported_as_expired` | 登录重定向识别 | 返回登录态过期错误 |
| `test_hepan_unsuccessful_result_is_not_reported_as_success` | 成功文案校验 | 未出现“发布文章成功”时返回上游发布错误 |
| `test_hepan_publish_route_returns_response_contract` | 对外 POST 合约 | 返回 `platform/success/http_status/article_url/message` |

运行完整回归测试：

```sh
.venv/bin/python -m pytest -q
```
