# 烈举网文章发布接口

## 1. 概述

本文描述媒体文章发布服务提供的烈举网（`lieju`）发布接口。服务使用已启用的烈举媒体账号、
数据库中的分类映射和该分类当前的发布表单完成发布。

基础地址示例：`http://127.0.0.1:8000`

| 项目 | 说明 |
| --- | --- |
| 请求与响应格式 | `application/json` |
| 路由前缀 | `/lieju` |
| 请求级认证 | 当前服务未定义额外的请求认证头；账号 Cookie 由服务端从媒体账号存储中读取 |
| 分类来源 | `tb_medium_platform_category` 中 `platform = 'lieju'` 的启用记录 |
| 分类值格式 | `city_id/fid`，例如 `95/111`；调用方只传完整分类路径，不传此值 |

调用发布接口前，账号必须存在、启用、未删除，且其 `platform` 为 `lieju`。分类必须已配置为
完整路径，例如 `商务服务/网站/软件服务`；服务会将其解析为烈举远端分类值。

## 2. 查询分类发布字段

### `GET /lieju/articles/publish/requirements`

读取该账号和分类当前的烈举发布表单，返回调用 `POST /lieju/articles/publish` 所需的动态平台字段。
由于烈举不同分类的字段、选项、是否支持图片和验证码状态可能不同，调用方应在每次发布前查询。

#### 查询参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | integer | 是 | 大于 `0` | 烈举媒体账号 ID |
| `category` | string | 是 | 去除首尾空白后长度 `1-128` | 已配置的完整分类路径 |

#### 请求示例

```sh
curl -G "http://127.0.0.1:8000/lieju/articles/publish/requirements" \
  --data-urlencode "account_id=3" \
  --data-urlencode "category=商务服务/网站/软件服务"
```

#### 成功响应示例

```json
{
  "account_id": 3,
  "platform": "lieju",
  "category": "商务服务/网站/软件服务",
  "publishable": true,
  "captcha_required": true,
  "fields": [
    {
      "key": "zone_id",
      "label": "区域",
      "control_type": "select",
      "required": true,
      "multiple": false,
      "default": null,
      "options": [
        {"value": "830", "label": "其他"}
      ],
      "validation": "*",
      "validation_message": "请选择区域"
    },
    {
      "key": "leibie",
      "label": "服务类型",
      "control_type": "radio",
      "required": true,
      "multiple": false,
      "default": null,
      "options": [
        {"value": "5", "label": "软件开发"}
      ],
      "validation": "*",
      "validation_message": null
    },
    {
      "key": "peitaosheshi",
      "label": "配套设施",
      "control_type": "checkbox",
      "required": false,
      "multiple": true,
      "default": [],
      "options": [
        {"value": "1", "label": "上门服务"}
      ],
      "validation": null,
      "validation_message": null
    }
  ]
}
```

`fields` 的结构如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | string | 写入 `platform_fields` 的字段名，不包含 `postdb[...]` 外层名称 |
| `label` | string | 当前烈举表单显示的标签 |
| `control_type` | string | `text`、`textarea`、`select`、`radio` 或 `checkbox` |
| `required` | boolean | 当前分类表单是否要求该字段 |
| `multiple` | boolean | 为 `true` 时请求值必须为字符串数组 |
| `default` | `string \| string[] \| null` | 当前表单的默认值 |
| `options` | array | 选项型字段的合法值及显示文本；提交时必须使用 `value` |
| `validation` | `string \| null` | 烈举表单提供的校验规则标记 |
| `validation_message` | `string \| null` | 烈举表单提供的校验提示 |

`publishable` 表示当前服务已具备继续发布的条件。`captcha_required` 为 `true` 时仍可能
`publishable = true`，这表示表单需要验证码，但服务已配置可用的验证码处理流程；若
`publishable = false`，发布请求会返回验证码相关的业务错误。

接口不会将文章的 `title`、`content`、验证码字段或表单内部的 `autofill` 字段放入 `fields`。
调用方不得将 `postdb[ticket]`、`postdb[randstr]` 或 `atc_yzm` 写入 `platform_fields`。

## 3. 发布文章

### `POST /lieju/articles/publish`

根据账号、分类和动态表单字段发布一篇文章。发布是同步等待式调用：HTTP 响应仅在上游烈举网
返回且服务确认发布成功后返回。

#### 请求体

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `account_id` | integer | 是 | 大于 `0` | 烈举媒体账号 ID |
| `title` | string | 是 | 长度 `1-200`，去除首尾空白后不可为空 | 文章标题 |
| `category` | string | 是 | 长度 `1-128`，去除首尾空白后不可为空 | 数据库中已配置的完整分类路径 |
| `content` | string | 是 | 不能全为空白 | 文章正文 |
| `content_type` | string | 否 | `markdown` 或 `html`，默认 `markdown` | 正文格式 |
| `platform_fields` | object | 否 | 值为 string 或 string[] | 需求查询接口返回的动态字段，默认 `{}` |

`platform_fields` 的键必须完全匹配当前分类需求查询中的 `fields[].key`。`checkbox` 类型或
`multiple = true` 的字段传字符串数组；其余字段传字符串。选项型字段必须使用 `options[].value`。
服务会校验未知字段、缺失必填字段、选项值和表单声明的 `*min-max` 长度规则。

#### 请求示例

以下值仅作为请求结构示例。`zone_id`、`leibie` 及其他字段必须以当前需求查询响应为准。

```sh
curl -X POST "http://127.0.0.1:8000/lieju/articles/publish" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": 3,
    "title": "物流 AI 助手服务介绍",
    "category": "商务服务/网站/软件服务",
    "content": "# 服务介绍\\n\\n提供物流业务场景的智能化经营支持。",
    "content_type": "markdown",
    "platform_fields": {
      "zone_id": "830",
      "leibie": "5",
      "dizhi": "线上服务（全国）",
      "mobphone": "13800000000",
      "linkman": "测试联系人"
    }
  }'
```

#### 成功响应

接口 HTTP 状态为 `200`；响应中的 `http_status` 是烈举上游发布请求的 HTTP 状态。

```json
{
  "account_id": 3,
  "platform": "lieju",
  "success": true,
  "http_status": 200,
  "article_url": "https://as.lieju.com/shangwu/123456.html",
  "message": "Lieju 发布成功"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `account_id` | integer | 原请求中的账号 ID |
| `platform` | string | 固定为 `lieju` |
| `success` | boolean | 发布成功时为 `true` |
| `http_status` | integer | 烈举上游发布响应状态码 |
| `article_url` | `string \| null` | 服务从成功页识别出的文章链接；上游未提供时为 `null` |
| `message` | string | 发布结果说明 |

## 4. 正文与图片处理

- `markdown` 会先渲染为 HTML，`html` 直接解析；两者均转换为保留段落换行的纯文本后提交给烈举。
- 正文内的 Markdown 图片、HTML `<img src>` 和带图片扩展名的裸链接会被提取并去重。
- 图片地址必须是 HTTP/HTTPS，且不能包含用户名或密码；仅支持 JPG、PNG、GIF。
- 每张源图片最大为 20 MiB、像素总数最大为 40,000,000。服务会尝试压缩图片至 1 MiB 以下。
- 图片数量由当前发布表单的上传槽位决定；当前分类不支持图片或数量超限时，接口返回
  `invalid_platform_fields`。

## 5. 业务错误响应

除请求模型校验错误外，错误响应的结构为：

```json
{
  "detail": {
    "code": "invalid_platform_fields",
    "message": "Lieju 发布参数与分类表单不匹配",
    "missing_fields": ["zone_id"],
    "unknown_fields": ["zone"],
    "invalid_fields": {"leibie": "选项无效：999"}
  }
}
```

| HTTP 状态 | `detail.code` | 触发条件 |
| --- | --- | --- |
| `404` | `account_not_found` | 媒体账号不存在 |
| `409` | `account_unavailable` | 账号未启用或已删除 |
| `409` | `login_failed` | 已执行登录但烈举未接受账号密码 |
| `409` | `login_expired` | 账号登录态已过期 |
| `409` | `captcha_required` | 当前表单需要验证码且服务未具备处理条件 |
| `422` | `platform_mismatch` | 账号平台不是 `lieju` |
| `422` | `invalid_platform_fields` | 动态字段缺失、未知、类型不符、选项无效、图片不符合要求或字符集不支持 |
| `422` | `invalid_request` | 分类未配置或烈举分类配置格式不合法 |
| `502` | `upstream_publish_error` | 烈举表单、图片下载或发布请求失败，或上游未确认发布成功 |
| `502` | `login_network_error` / `login_protocol_error` | 登录刷新流程的网络或协议错误 |
| `503` | `refresh_unavailable` / `persistence_unavailable` | 发布超时、刷新协调或状态持久化不可用 |

字段级请求校验（例如 `account_id <= 0`、空标题、未知 `content_type`）由 FastAPI/Pydantic 在
业务服务调用前返回 `422`，其 `detail` 为校验错误数组，不使用上述业务错误结构。

## 6. 测试用例

可执行的离线接口契约测试位于
[`tests/test_lieju_api.py`](../tests/test_lieju_api.py)，通过依赖覆盖替换发布服务，不访问数据库、
Redis、烈举网或验证码服务。

```sh
.venv/bin/pytest tests/test_lieju_api.py -q
```

| 编号 | 场景 | 输入/模拟 | 预期结果 |
| --- | --- | --- | --- |
| `TC-LIEJU-01` | 查询动态字段 | 有效账号和分类，模拟包含 select、radio、checkbox 的需求 | `200`，返回字段类型、选项、验证码状态和 `publishable` |
| `TC-LIEJU-02` | 正常发布 | 合法 Markdown 正文和动态平台字段 | `200`，返回发布响应；服务收到默认 `markdown` 类型与原始动态字段 |
| `TC-LIEJU-03` | 平台字段校验失败 | 模拟缺失字段、未知字段和无效选项 | `422`，`detail.code = invalid_platform_fields`，且返回字段级错误详情 |
| `TC-LIEJU-04` | 请求模型校验失败 | 不支持的 `content_type` | `422`，发布服务不应被调用 |
| `TC-LIEJU-05` | 空白分类 | `category` 仅包含空白字符 | `422`，`detail.code = invalid_request`，需求服务不应被调用 |

## 7. 相关实现

- 路由：[app/api/platform/lieju/article_publish_router.py](../app/api/platform/lieju/article_publish_router.py)
- 通用路由契约：[app/api/platform/publish_router_factory.py](../app/api/platform/publish_router_factory.py)
- 烈举发布器：[app/api/platform/lieju/article_publisher.py](../app/api/platform/lieju/article_publisher.py)
- 请求/响应模型：[app/schemas.py](../app/schemas.py)
