# 平台目录重构任务计划

## 目标

将平台 API 路由和平台实现统一组织到 `app/api/platform/<platform>/`，替代当前的
`app/api/site/` 与 `app/models/`。公共发布契约和注册表迁入 `app/utils/`。

本次仅调整内部模块边界和文件命名；现有 HTTP 路径、响应契约、发布行为和依赖注入方式保持不变。

## 目标目录

```text
app/api/platform/
  __init__.py
  publish_router_factory.py
  cnblogs/
    __init__.py
    article_publish_router.py
    article_publisher.py
  hepan/
    __init__.py
    article_publish_router.py
    article_publisher.py
  lieju/
    __init__.py
    article_publish_router.py
    article_publisher.py
    captcha_solver.py
    tdc_runtime.py

app/utils/
  publisher_contract.py
  publisher_registry.py
  request.py
```

## 文件职责与迁移映射

| 当前文件 | 目标文件 | 主要职责 |
| --- | --- | --- |
| `app/api/site/_publish.py` | `app/api/platform/publish_router_factory.py` | 生成各平台文章发布和需求查询路由 |
| `app/api/site/<platform>/publish.py` | `app/api/platform/<platform>/article_publish_router.py` | 注册指定平台的文章发布路由 |
| `app/models/base.py` | `app/utils/publisher_contract.py` | `ArticlePublisher` 公共发布契约 |
| `app/models/registry.py` | `app/utils/publisher_registry.py` | 平台发布器注册与查找 |
| `app/models/cnblogs.py` | `app/api/platform/cnblogs/article_publisher.py` | CNBlogs 文章发布实现 |
| `app/models/hepan.py` | `app/api/platform/hepan/article_publisher.py` | Hepan 文章发布实现 |
| `app/models/lieju.py` | `app/api/platform/lieju/article_publisher.py` | Lieju 表单、图片和文章发布实现 |
| `app/models/lieju_captcha.py` | `app/api/platform/lieju/captcha_solver.py` | Lieju 腾讯验证码求解流程 |
| `app/models/lieju_tdc_runtime.py` | `app/api/platform/lieju/tdc_runtime.py` | Lieju TDC JavaScript 运行时与载荷编码 |

## 实施步骤

1. 创建 `app/api/platform` 及三个平台子包，按目标目录迁移路由和平台实现。
2. 迁移公共 `ArticlePublisher` 与 `PublisherRegistry` 至 `app/utils`，保留现有公开类名和方法签名。
3. 更新 `app/api/__init__.py`、`PublishOrchestrator`、平台模块内部导入、运行脚本和测试导入路径。
4. 删除 `app/api/site` 与 `app/models`，不保留 re-export 或兼容转发模块。
5. 保持 `/cnblogs/articles/publish`、`/hepan/articles/publish`、`/lieju/articles/publish` 及对应 requirements 路径不变。

## 验证与验收

- 所有 Python 导入均指向新目录；运行时代码不再引用 `app.api.site` 或 `app.models`。
- `PublisherRegistry` 继续注册 Hepan、CNBlogs、Lieju 三个发布器。
- FastAPI 继续注册现有平台 HTTP 路由，响应模型和错误映射不变。
- 现有发布器、Lieju 验证码、TDC、脚本和 API 测试全部更新导入并通过。
- 执行 `.venv/bin/python -m py_compile` 和 `.venv/bin/python -m pytest -q`。

## 约束

- 不变更数据库结构、环境变量或外部 HTTP API。
- 不修改 `app/utils/request.py` 的统一请求行为。
- 文件名必须反映主要职责，避免使用含义模糊的 `base.py`、`publish.py` 或平台混合模块名。
