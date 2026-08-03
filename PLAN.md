# FastAPI 多平台文章发布服务

## 接口

`POST /{platform}/articles/publish` 使用 `async def`/`await` 发布到 `tb_medium_account.id` 对应的平台账号。
接口等待发布完成后返回 `200`，不创建后台任务。

请求体包含 `account_id`、`title`、`category`、`content` 和 `content_type`。
`content_type` 默认值为 `markdown`，支持 `markdown` 与 `html`。

## 处理流程

1. 异步 session 查询启用且未删除的媒体账号；CNBlogs 登录态失效时按需刷新。
2. 在公共层解析 `cookie` 或 `cookies` JSON，并查询平台共享的中文分类映射。
3. 从固定平台注册表选择 Hepan、CNBlogs 或 Lieju model。
4. 使用异步 HTTP client 调用 model 发布函数，等待结果后返回文章地址。

## 分类映射

分类通过 `tb_medium_platform_category` 按 `platform` 和中文 `category_name`
映射至远端分类 ID。Hepan 使用单个 ID，CNBlogs 使用逗号分隔的 ID 列表，
Lieju 使用完整分类路径和 `city_id/fid`。

Lieju 先读取实时分类表单，以 `platform_fields` 适配不同分类的必填字段、控件类型和
选项，再按 GB18030 编码提交。`GET /lieju/articles/publish/requirements` 用于查询这些字段；
验证码字段已存在时直接提交，否则在配置了脚本绑定的 TDC profile 后获取
ticket/randstr 并合并进发布请求。

## 验证

测试覆盖请求校验、账号状态、Cookie 解析、分类映射、model 分发、两种内容类型和
三个适配器的 HTTP 载荷，以及 Lieju 动态字段、验证码流程和分类种子。
