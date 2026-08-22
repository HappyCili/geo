# Hepan 登录流程与运行约束

本文档对应当前 FastAPI 服务中的 Hepan 登录刷新和文章发布链路。它补充
[Hepan 发布接口文档](hepan-api.md)，重点说明 Cookie 生命周期、目标站人机验证、
IP 相关风险，以及会影响实际行为的环境变量。

## 结论摘要

- 未发现服务端针对 Hepan 实现的 IP 白名单、黑名单、`X-Forwarded-For`、`remote_addr` 或 GeoIP 判断。
- 登录页出现的 `renji_...` 脚本和运行日志中的动态 `...yanzheng_ip.php` 验证地址来自 Hepan 页面，
  不存在于本地项目文件中。该验证可能把 Cookie、浏览器会话和出口 IP 关联；这是根据目标站流程作出的运行风险判断，
  不是本地代码可以确认的规则。
- Hepan 账号和密码只从 `tb_medium_account.account`、`tb_medium_account.password` 读取，
  没有 `HEPAN_USERNAME`、`HEPAN_PASSWORD` 或其他 Hepan 环境变量凭据回退。
- HTTP 客户端没有显式关闭 `trust_env`。因此服务进程若设置了代理或 CA 证书环境变量，
  其出口 IP、路由和 TLS 行为会随之变化。

## 代码入口与职责

| 文件 | 职责 |
| --- | --- |
| [app/services/publish_orchestrator.py](../app/services/publish_orchestrator.py) | 发布入口、账号状态判断、刷新后仅重试一次发布 |
| [app/repositories.py](../app/repositories.py) | 从 MySQL 读取账号元数据、Cookie 载荷和登录凭据；原子更新刷新版本和结果 |
| [app/services/cookie_store.py](../app/services/cookie_store.py) | Redis Cookie 缓存读取、回源 MySQL 和缓存回写 |
| [app/services/refresh.py](../app/services/refresh.py) | Redis 分布式刷新锁、刷新 fence、登录结果持久化 |
| [app/services/login.py](../app/services/login.py) | Hepan 登录页、人机验证、表单参数和 AJAX 登录 |
| [app/api/platform/hepan/article_publisher.py](../app/api/platform/hepan/article_publisher.py) | 带 Cookie 获取发布表单、提交文章并确认发布结果 |
| [app/config.py](../app/config.py) | `.env` / 进程环境配置和默认超时 |
| [app/utils/request.py](../app/utils/request.py) | HTTP 请求、重试和脱敏日志 |
| [hepan_publish.py](../hepan_publish.py) | 独立的历史手工发布脚本，不属于 API 登录刷新链路 |

## 当前主流程

```text
POST /hepan/articles/publish
  -> PublishOrchestrator.publish()
  -> AccountRepository.get_active()
  -> PublishOrchestrator._ensure_credentials_before_publish()
     -> CookieStore.load() 或 RefreshCoordinator.refresh()
  -> HepanLoginProvider.login()（需要刷新时）
  -> HepanPublisher.publish_article()
```

### 1. 读取账号状态和 Cookie 可用性

`PublishOrchestrator.publish()` 对每次发布都会调用 `AccountRepository.get_active()`。
该查询同时读取账号状态、Cookie 版本、刷新结果，并在同一条 SQL 中计算
`cookie_material_available`：

- 数据库中 `cookie` 和 `cookies` 都为空时，即使旧的 `sync_status = 1`，返回给编排层的有效
  `sync_status` 也会变为 `0`；
- 编排层会在调用任何平台发布器之前执行 `_ensure_credentials_before_publish()`；Cookie 缺失时先完成
  登录、持久化新 Cookie，再调用 `HepanPublisher.publish_article()`，不会先向 Hepan 尝试发布；
- 缺少 Cookie 时不会把 Redis 中同版本的旧 Cookie 当作有效登录态；
- 这不是为 Redis 命中额外增加的一次 MySQL 查询。发布本来就要读取账号元数据，判断被合并在该查询中。

当账号元数据仍有效时，`CookieStore.load()` 先读取
`media:cookie:v1:{account_id}`。只有 Redis 记录的 `version`、`result`、Cookie 映射和
`cookie_header` 都与数据库元数据一致，才直接复用缓存。缓存缺失、版本不一致或 Redis 异常时，
才回源读取数据库完整 Cookie 载荷并重新写入 Redis。

### 2. 刷新协调与并发控制

需要刷新时，`RefreshCoordinator` 使用 Redis 锁
`media:refresh-lock:v1:{account_id}` 保证同一账号同一时间只有一个刷新者：

1. 获得锁的请求读取当前账号、声明数据库 refresh fence，并从 `account` / `password` 获取登录凭据；
2. 其他请求等待刷新结果，不会并发提交相同账号的登录请求；
3. 登录成功后，数据库写入新的 Cookie、`session_id`、递增 `cookie_version`，再写入 Redis；
4. 刷新失败时，数据库和 Redis 记录失败结果。登录本身失败时会在持有刷新锁期间再尝试一次，第二次仍失败
   返回 `login_failed`；已有 Cookie 在发布重试时仍然失效才返回 `login_expired`。`network_error` 映射为
   `login_network_error`，解析不到预期页面/响应格式映射为 `login_protocol_error`；
5. 刷新获得新 Cookie 后，当前发布请求使用新 Cookie 再执行一次发布。运行时发现登录态过期时，
   仅在该发布操作可安全重试的情况下执行一次“刷新后重试”。

### 3. Hepan 登录 HTTP 会话

`HepanLoginProvider` 为一次登录创建一个 `httpx.AsyncClient`，该客户端贯穿以下全部登录请求，
因此挑战 Cookie、验证请求和登录请求在同一 Cookie jar 中连续执行：

1. `GET /member.php?mod=logging&action=login&referer=` 获取登录页；
2. 若收到指向同一登录页的 `403` JavaScript 跳转页，按页面要求重新加载一次；重复出现则归类为
   `captcha_required`；
3. 若页面包含“宝塔防火墙正在检查您的访问”或 `renji_...` 脚本：
   - 获取页面声明的动态脚本；
   - 解析其中的验证路径、`type`、`key` 和 `value`；
   - 按页面脚本规则计算验证值并调用该动态验证地址；
   - 使用同一客户端重新加载登录页；
4. 从页面读取 `formhash` 和 `version`；
5. 向 `POST /plugin.php?id=it618_members:ajax&ac=login&formhash=...` 提交手机号和密码，
   同时带上当前登录页 URL 作为 `Referer`、站点 `Origin` 和 AJAX 请求头；
6. 确认成功标记后，从同一个客户端的 Cookie jar 提取 Cookie 和可能的 `sid`，交给刷新协调器持久化。

登录单个 HTTP 请求的上限为 15 秒，且不进行额外 HTTP 重试；整个登录流程的外层超时由
`LOGIN_TIMEOUT_SECONDS` 控制。这样可以让发布请求把剩余时间留给刷新后的发布表单获取和提交。

### 4. 发布阶段与登录会话的关系

登录成功后，`HepanPublisher` 会创建一个新的 HTTP 客户端，并把刚持久化的 Cookie 放入该客户端的
Cookie jar：

1. `GET /portal.php?mod=portalcp&ac=article&catid={category_id}`；
2. 接收 GET 响应新增或更新的会话/WAF Cookie，后续 POST 自动使用同一个 Cookie jar；
3. 从 `form#articleform` 读取当前 `formhash`；
4. 以 `multipart/form-data` 向 `POST /portal.php?mod=portalcp&ac=article` 提交文章；
5. 使用页面成功文案、编辑链接或最终编辑页 URL 确认发布成功。

如果编辑表单 GET 返回登录页、`401` 或没有业务内容的 `403`，此时文章尚未提交，编排层会刷新登录并安全地
重试一次完整发布。若刷新后的登录页仍被 nginx 直接返回 `403`，则会归类为登录协议/人机验证失败，不会进入
文章 POST。
文章 POST 一旦发出，返回登录页或 `403` 时不会自动重发，因为无法可靠判断上游是否已经创建文章，自动重试
可能产生重复内容。GET 为 `200`、紧接着 POST 为 `403` 通常位于 Hepan 的权限/WAF 校验阶段；新流程会保留
GET 新下发的 Cookie，但若仍出现该结果，应继续检查响应页面的人机验证标记、账号发布权限和出口 IP 一致性。

因此，“登录页挑战”内部保证同一会话，但“登录”和“发布”是两个 HTTP 客户端。如果代理按连接轮换出口，
或者服务在两步之间切换网络出口，目标站可能观察到不同 IP，导致 Cookie 或人机验证状态失效。

## IP 限制与出口一致性核查

### 本地源码结论

已搜索 `app/`、`scripts/`、`docs/` 和 Hepan 相关脚本，没有发现以下本地实现：

- 允许或拒绝某些 IP 地址的列表；
- 根据客户端 IP、`X-Forwarded-For` 或 `X-Real-IP` 拒绝请求；
- GeoIP、地区或 ASN 判断；
- 固定代理地址、代理轮换或针对 Hepan 的 IP 配置文件。

`yanzheng_ip.php` 不是本地文件名。它由 Hepan 人机验证页面的动态脚本提供，服务只解析并请求该页面给出的路径。
日志中它被请求成功，说明当前代码能完成该一轮验证；它不等同于项目中配置了本地 IP 限制。

### 目标站侧的风险信号

以下现象说明需要保持出口稳定，但不能单独证明 Hepan 的具体绑定规则：

- 首次访问登录页可能先得到 `403`、JavaScript 跳转和 `renji_...` 人机验证脚本；
- 验证接口名称包含 `ip`；
- 验证页和后续请求使用服务端下发的会话 Cookie；
- 登录页、验证脚本、验证接口和 AJAX 登录在一个客户端内完成。

运行约束：同一次登录及其紧接的发布请求应使用稳定的 DNS、网络栈和出口 IP；不要在这些请求之间切换代理、
自动轮换代理节点、VPN 或 IPv4/IPv6 出口。若部署在负载均衡或多实例环境，还需要保证同一刷新任务不会被
不同出口的节点接续。

## 环境变量与默认值

`Settings` 会从进程环境和项目根目录 `.env` 读取配置。未声明的配置项会被忽略；当前没有任何
`HEPAN_*` 凭据字段。

| 配置 | 是否必需 | 默认值 | 对 Hepan 流程的影响 |
| --- | --- | --- | --- |
| `DB_HOST`、`DB_USER`、`DB_PASSWORD`、`DB_NAME` | 是 | 无 | 读取账号密码、Cookie、刷新状态和分类 |
| `DB_PORT` | 否 | `3306` | MySQL 连接端口 |
| `DB_CHARSET` | 否 | `utf8mb4` | MySQL 字符集 |
| `REDIS_URL` | 是 | 无 | Cookie 缓存和刷新分布式锁；不可用时刷新返回 `refresh_unavailable` |
| `REDIS_TIMEOUT_SECONDS` | 否 | `1.0` 秒 | Redis 建连及请求超时 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `30.0` 秒 | 发布表单 GET 和文章 POST 的 HTTP 超时 |
| `LOGIN_TIMEOUT_SECONDS` | 否 | `75.0` 秒 | 单次 Hepan 登录全流程上限 |
| 源码常量 `HEPAN_REQUEST_TIMEOUT_SECONDS` | 固定 | `15.0` 秒 | 登录内每一个 HTTP 请求的上限，取该值和 `LOGIN_TIMEOUT_SECONDS` 的较小值 |
| `PUBLISH_DEADLINE_SECONDS` | 否 | `150.0` 秒 | 单个 API 发布请求的总 deadline，必须不小于登录超时和刷新等待 |
| `COOKIE_CACHE_TTL_SECONDS` | 否 | `86400` 秒 | Redis Cookie 记录的过期时间 |
| `REFRESH_LOCK_TTL_SECONDS` | 否 | `90` 秒 | 同账号刷新锁租约；必须大于续租间隔 |
| `REFRESH_LOCK_RENEW_SECONDS` | 否 | `20` 秒 | 刷新锁续租间隔 |
| `REFRESH_WAIT_SECONDS` | 否 | `80.0` 秒 | 未获得刷新锁的请求等待共享结果的时间 |
| `OBSERVABILITY_ACCOUNT_SALT` | 否 | 无 | 日志账号关联值的 HMAC 盐；未设置时使用数据库密码作为密钥 |
| `APP_LOG_LEVEL` | 否 | `DEBUG` | 日志级别；`DEBUG` 会记录完整请求 URL、headers、body 和 `response.text` |

当前 `.env` 已配置数据库、Redis、发布请求超时及 Lieju 相关项；`LOGIN_TIMEOUT_SECONDS`、
`PUBLISH_DEADLINE_SECONDS`、Cookie 缓存和刷新锁项未在 `.env` 中声明时使用上表默认值。进程环境的同名变量
仍可覆盖 `.env`，因此启动服务的 tmux、IDE、容器或系统服务环境也需要一并检查。

### HTTPX 隐式环境变量

`HepanLoginProvider`、`HepanPublisher` 和 `BaseRequest` 创建 `httpx.AsyncClient` 时均未传递
`trust_env=False`。当前依赖的 HTTPX 默认 `trust_env=True`，以下环境变量会在不经过
`app/config.py` 的情况下影响外连：

| 变量 | 影响 | 登录排障关注点 |
| --- | --- | --- |
| `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` | 改变请求代理与出口 IP | 不使用轮换代理；确认验证、登录、发布看见同一稳定出口 |
| `NO_PROXY` | 对部分主机绕过代理 | 确认 `www.hepan.com` 不会在步骤之间走不同路径 |
| `SSL_CERT_FILE`、`SSL_CERT_DIR` | 改变 TLS CA 证书来源 | 企业代理、私有 CA 或错误证书路径可能表现为连接失败/超时 |

本次整理所用的交互 shell 未设置上述变量，但 `8002` 服务进程可能由不同的启动环境继承变量，
不能据此推断该服务进程也为空。排查时只记录是否设置，不把代理 URL 或凭据写入日志，例如：

```sh
env | rg '^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|SSL_CERT_FILE|SSL_CERT_DIR)=' | sed 's/=.*$/=<set>/'
```

`PUBLISH_API_BASE_URL` 仅被其他平台的命令行辅助脚本读取，不影响 FastAPI 的
`POST /hepan/articles/publish`，也不参与 Hepan 上游登录。

## 历史手工脚本的边界

[hepan_publish.py](../hepan_publish.py) 会读取根目录的 `hepan_config.json` 和浏览器抓取的请求头。
它是独立手工发布路径，不会被 API、`HepanLoginProvider` 或刷新协调器调用。

`hepan_config.json` 中的 Cookie 是敏感登录材料；文档、日志和接口响应都不应复制其值。该文件中的旧 Cookie、
旧浏览器请求头或与当前服务不同的网络出口，不会改变 API 的登录流程，但同时使用这条历史路径会让排障难以判断
到底是哪一套 Cookie 和出口产生了问题。

## 故障定位顺序

### `login_protocol_error`

该错误表示登录页或 AJAX 响应不再符合当前解析规则，例如缺少 `formhash`、`version` 或成功标记。
优先检查：

1. `app.request` 日志中的登录页、验证脚本、验证接口和 `plugin.php` 的状态码与耗时；
2. 是否反复返回人机验证页或新的页面结构；
3. 服务进程是否设置了代理/证书环境变量，出口是否稳定；
4. 数据库中该账号的 `account`、`password` 是否仍是已绑定手机号和有效密码。

### `login_network_error` 或请求超时

该错误来自登录外层超时、HTTPX 请求异常或上游 `5xx`。按以下顺序检查：

1. `HEPAN_REQUEST_TIMEOUT_SECONDS = 15` 秒的单请求限制，以及 `LOGIN_TIMEOUT_SECONDS` 的总限制；
2. DNS、IPv4/IPv6、企业代理和 TLS CA 环境；
3. Redis 是否能在 `REDIS_TIMEOUT_SECONDS` 内获得刷新锁；
4. 是否有多个服务进程使用不同环境或不同出口同时处理同一个账号。

### 发布返回 `upstream_publish_error`

登录成功并不等于文章提交已确认成功。发布器会先读取文章编辑表单的 `formhash`，再提交 multipart 表单，
最后检查成功文案或编辑链接。发生该错误时应查看脱敏后的页面失败提示、HTTP 状态码和响应 URL，
并确认分类 ID 与账号发布权限，而不是仅检查登录 Cookie。编辑表单在发布 POST 之前缺少 `formhash` 时，
服务会将其视为可安全重试的登录态失效，先刷新登录态并完整重试一次；刷新后仍缺少令牌时才返回
`login_expired`。

如果发布响应包含“只能发布 N 篇文章”等额度提示，服务会返回 `429 publish_limit_reached`，
这表示账号额度已耗尽，不会触发登录刷新。

## 验证清单

1. 使用本地假 HTTP 客户端运行 Hepan 相关测试：

   ```sh
   venv/bin/python -m pytest test_hepan_publish_flow.py -q
   ```

2. 检查 `GET /hepan/articles/publish/requirements` 是否能读取现有登录态；
3. Cookie 缺失时，确认日志按 `publish_refresh_triggered`、`refresh_owner`、
   `refresh_claimed`、`refresh_login_completed`、`cookie_cache_write_ready`、`refresh_ready`
   的顺序出现；
4. 对照服务实际启动环境，确认代理与证书变量是否被设置，并保持整个登录到发布期间出口稳定；
5. 不在测试输出、提交记录或文档中写入账号、密码、Cookie、完整代理地址或会话 ID。
