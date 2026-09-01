# 视频流式播放与 HTTP Range 实施计划

> **历史归档（2026-09-01）：本文是 2026-08-11 形成的短期媒体票据 query 方案，未按本文直接实施。后续 HTTP Range 改用双 Cookie 认证，当前生产又采用低码率完整认证 GET 与真实字节加载进度；本文仅保留早期架构取舍和测试矩阵，不是现行实施或部署依据。**
>
> 日期：2026-08-11  
> 对应工作项：WI-20260805-22  
> 状态：**已确认计划，尚未实现**  
> 优先级：**P0 / 第一优先级**  
> 实施分支目标：`feature/spatial-ui-optimization`

## 1. 目标与非目标

### 1.1 目标

1. 取消三个视频页面当前的“前端 `fetch` 后 `res.blob()` 整包缓冲”播放路径，让浏览器原生 `<video>` 能按自身媒体策略发起普通请求和 HTTP byte-range 请求。
2. 在不把长期登录凭据放入 URL 的前提下，为原生 `<video src>` 提供短期、限用户、限视频、只读的流媒体访问票据。
3. 继续复用后端 `FileResponse`，由经过锁定和验证的 Starlette 实现 Range 协议，不在业务代码手写 Range 解析。
4. 保持视频存在性、active 项目成员、路径边界和磁盘文件检查，并在每一次携带票据的媒体请求中即时复查。
5. 建立协议、权限、浏览器、反向代理与量化性能基线，使上线、灰度和回滚均可验证。

### 1.2 非目标

- 本轮不生成低码率播放副本，不改变视频编码、上传、片段、缩略图或 ZIP 导出流程。
- 本轮不把登录体系整体迁移到 Cookie；Cookie 只作为长期认证迁移备选。
- 本轮不承诺浏览器绝不下载完整文件，也不干预浏览器自身预取、缓冲和解码策略。
- 本轮不手写单 Range、多 Range、`If-Range` 或边界计算逻辑。
- 本计划不实施代码、不修改依赖、不运行迁移，也不代表功能已经完成。

根 `README.md` 的“部署带宽优化点（规划，未实施）”已把 HTTP Range 列为第一优先级；本文件只细化可执行方案，不重复修改 README。

## 2. 当前事实与证据

### 2.1 前端现状

- `frontend/src/api/index.ts:183-201` 的 `fetchVideoStreamUrl` 通过 `apiRaw` 获取响应，再执行 `res.blob()` 和 `URL.createObjectURL(blob)`。这会在 `<video>` 接管播放前强制前端整包接收并缓冲视频。
- 三个调用点为：
  - `frontend/src/pages/AnnotatePage.tsx:962`
  - `frontend/src/pages/ReviewPage.tsx:220`
  - `frontend/src/pages/ClipsPage.tsx:376`
- 登录 token 位于 `localStorage`；`frontend/src/api/client.ts` 中的 `apiRaw`/`apiFetch` 会注入 `Authorization: Bearer ...`。
- HTML `<video>` 不能像 `fetch` 一样配置任意 Bearer 请求头，因此不能直接把当前受 Bearer 保护的 URL 赋给 `src`。

### 2.2 后端现状

- `backend/app/routers/videos.py:236-274` 已完成：视频存在、视频所属项目成员、membership 为 `active`、`storage_path` 解析后仍在 `videos_dir` 内、磁盘目标为文件等检查，并以内联 `FileResponse` 返回视频。
- `backend/tests/test_videos.py` 当前只覆盖流读取的 200、权限和路径安全等场景，未建立 Range 协议测试。
- `backend/requirements.txt` 仅直接声明 `fastapi>=0.110,<1.0`，未显式约束 Starlette。
- 2026-08-11 本地实测组合为 FastAPI `0.141.1` / Starlette `1.3.1`。
- Starlette 官方 release notes 记录：`FileResponse` 从 `0.39.0` 加入 HTTP Range 支持，`0.49.1` 含 Range 解析安全修复。当前官方行为包括 `Accept-Ranges: bytes`、合法 Range 返回 `206 Partial Content`、不可满足的 Range 返回 `416 Range Not Satisfiable`。

## 3. 关键更正与问题定界

“原生 `<video>` + HTTP Range”表示浏览器可以根据元数据、播放位置和缓冲策略按需发请求，**不保证浏览器绝不下载完整文件**。即使服务端支持 206，浏览器也可能顺序读取到文件末尾或主动扩大缓冲范围。

后端目前已经使用 `FileResponse`；核心工作不是重新实现文件流，而是：解决原生 `<video>` 无法附加 Bearer header 的鉴权传递，显式保障并锁定具备安全 Range 行为的依赖组合，建立协议与权限测试，移除前端视频专用 blob 路径，并验证反向代理不会吞 Range 或整包缓冲。

## 4. 架构决策

### 4.1 已确认方案

采用“Bearer 申请短期媒体票据 + 原生 `<video src>` 携票播放”双步骤：

1. 前端以现有 Bearer 调用 `POST /api/videos/{video_id}/stream-ticket`。
2. 后端验证当前用户与视频访问权限，签发短期、限用户、限视频、只读票据。
3. 前端把基于可信 `API_BASE` 生成的 `/api/videos/{video_id}/stream?ticket=...` 交给原生 `<video src>`；开发环境允许受控跨端口访问，生产环境以同源部署为目标。
4. 浏览器后续发起普通 GET 或一个/多个 Range 请求；每次请求均重新验票并重新检查实时授权和文件边界。

明确拒绝把现有有效期 7 天、具备完整登录权限的 JWT 放入 query。媒体票据只允许访问指定视频的 stream 资源。

### 4.2 票据最小契约

| Claim | 含义 |
|---|---|
| `sub` | 申请票据的用户 ID |
| `video_id` | 唯一允许读取的视频 ID |
| `aud` | 固定媒体受众，例如 `video-stream` |
| `iat` | 签发时间 |
| `exp` | 过期时间 |
| `jti`（可选） | 审计、定向吊销或故障关联 ID |

- 票据**不能设计为单次使用**：原生视频播放可能为同一资源连续发起元数据、多个 Range、重试和恢复请求。
- 建议 TTL 为 **60 分钟**。最终值属于实施前待确认项，但不改变已确认架构。
- 推荐使用媒体专用 secret、固定 audience 和独立配置，不与完整登录 JWT 的签发语义混用。
- 票据申请响应建议只返回 `{ url, expires_at }`；不把票据拆散存入长期前端状态或持久化存储。

### 4.3 续票与每次请求复查

- 正常播放不定时轮询续票。任意媒体 `onError` 在每个加载世代最多申请新票并重试一次，恢复失败前的 `currentTime` 与播放/暂停状态；一次续票仍失败时停止自动重试。
- 原生媒体元素通常不会向 React 暴露底层 HTTP 状态，因此不得根据 `onError`推断 401/403。票据申请 API 使用现有 `apiFetch`/Bearer，只有该 API 的响应可以分类处理 401/403，其中登录失效的 401 继续沿用自动登出流程；媒体错误本身统一按网络、票据过期或编码等通用类别提示。
- 每次 `/stream?ticket=...` 都必须检查 ticket 签名、`aud`、`iat`、`exp`，URL 与 claim 的 `video_id` 一致，`sub` 用户仍可用且仍是 active member，并重新检查视频、路径边界、磁盘文件及非零字节。

## 5. 鉴权优先级与防降级

1. **请求带 `ticket`：只验证 ticket。** ticket 伪造、过期、audience 错误、视频不匹配或用户失效时直接拒绝，绝不回退 Bearer。
2. **请求不带 `ticket`：**只有兼容开关明确开启时才接受旧 Bearer 流路径。
3. ticket 与 Bearer 同时出现时仍按 ticket 分支处理，防止凭据混淆和降级绕过。
4. 收口阶段关闭 legacy Bearer 开关；稳定观察后删除旧视频 blob 能力。

生产目标是同源 HTTPS 和受控反向代理，但反向代理本身不能读取浏览器 `localStorage`，因此“交给反代”不能解决当前 Bearer 传递问题。Cookie 会自然随 `<video>` 请求发送，可作为长期认证迁移方案，但不是本轮前置条件。

## 6. 后端详细改动计划

### 6.1 单一授权与路径解析 helper

- 从现有 `stream_video` 提取唯一 helper，输入 `db/settings/video_id/user_id`，输出已验证的 `(video, path)` 或统一 HTTP 错误。
- 签票接口和每次 stream 请求复用该 helper，不能维护两套 membership/path 规则。
- 继续使用 `Path.resolve()` 与 `is_relative_to(videos_dir)`；symlink 解析后越界同样拒绝。
- 增加零字节文件拒绝，避免空文件进入 Range 响应层。

### 6.2 票据签发与 Stream

- 增加 `StreamTicketResponse`，建议字段为 `url`、`expires_at`。
- 新增 `POST /api/videos/{video_id}/stream-ticket`，只接受现有 Bearer；签发前调用统一 helper。
- `/api/videos/{video_id}/stream` 接收可选 ticket，并严格执行第 5 节优先级；ticket 分支完整验票后以 `sub` 复查实时权限和路径。
- 返回现有内联 `FileResponse`，首版固定使用 `Cache-Control: private, no-store`，使票据过期或成员失效后的后续 Range 必须重新到达后端；浏览器、反向代理和 CDN 均不得缓存媒体响应。只有未来另立安全设计并重新定义实时撤权语义后，才可评估受控缓存。
- 日志、统一异常和监控不得记录完整 URL、query 或 ticket；审计最多记录用户 ID、视频 ID、结果、可选 `jti` 哈希/前缀和错误类别。
- 不手写 Range 解析；合法/非法/多 Range、`If-Range` 与响应 headers 由锁定版本的 Starlette `FileResponse` 承担并通过测试固定。

### 6.3 配置与依赖

建议配置概念：ticket TTL（建议 3600 秒并校验上下限）、生产必填的媒体专用 secret、固定 `video-stream` audience、ticket 功能开关、legacy Bearer 开关。

- 显式增加 Starlette 能力约束，建议至少 `starlette>=0.49.1,<2.0`，避开 Range 解析安全修复之前的版本。
- 同时核对 FastAPI 对 Starlette 的兼容区间；选择官方兼容且通过全量测试的组合，不盲目升级。
- 用生产部署 constraints/lock 固定经过测试的 FastAPI、Starlette、Uvicorn、httpx 组合；宽泛 requirements 不能替代可复现锁定。
- P0 先记录当前 `0.141.1 / 1.3.1` 行为，P1 改依赖后重跑同一协议矩阵。

## 7. 前端详细改动计划

### 7.1 统一能力层

- 新增统一函数，建议规范命名为 `getVideoStreamSource(videoId)`。
- 该函数用 `apiFetch`/Bearer 调用 ticket API，返回 `{ url, expiresAt }`，不再读取视频 body。
- URL 保持同源相对路径优先，不得进入错误文本、analytics、console 或持久状态。
- 异步请求使用 request sequence、AbortController 或 effect cancellation；组件卸载、选择其他片段或切换视频后，旧签票结果不得覆盖新视频。

### 7.2 三页迁移与生命周期

按 `ClipsPage` → `ReviewPage` → `AnnotatePage` 灰度；三个页面最终全部迁移。每个 `<video>` 增加 `preload="metadata"`，但明确它只是浏览器提示，不是禁止完整下载的强制策略。

- 只删除**视频播放专用** blob URL 创建、`URL.revokeObjectURL` 和相关状态生命周期。
- 不得误删缩略图、ZIP 下载、JSON 导出或其他仍合理使用 blob/object URL 的逻辑。
- `onError` 保存失败前 `currentTime`、播放状态和目标 video ID；媒体错误本身不按 HTTP 状态分类。单次续票成功后等待 metadata/canplay，再恢复时间和播放状态；只有签票 API 的失败可以显示明确的 401/403/404 类别。
- 每个加载世代最多续票一次，切换视频后重置。错误 UI 只显示 HTTP 类别、视频 ID 和可行动建议，绝不打印完整 ticket URL。
- 生产 console、APM breadcrumb、网络错误包装和 metrics label 都必须脱敏 query。

## 8. 分阶段工作图

| 阶段 | 入口条件 | 产物 | 验收 | 回滚 |
|---|---|---|---|---|
| P0 协议基线和指标 | 当前代码、依赖和真实视频可运行 | Range/headers/代理/浏览器现状；旧 blob 的 heap、传输量、metadata/首帧/seek 基线 | 样本、版本与复现命令固定；指标可重复采集 | 无代码切换；保留当前路径 |
| P1 后端双栈 | P0 明确协议和依赖组合 | helper、ticket schema/route、验证、配置、显式依赖和测试；legacy 暂保留 | 权限与 Range 矩阵通过；无效 ticket 不回退；后端全量回归通过 | 关闭 ticket flag，继续 legacy/blob |
| P2 前端能力层 | P1 API 稳定 | `getVideoStreamSource`、取消/世代保护、续票状态机、脱敏错误 | 能力测试通过；401 自动登出保持；不拉视频 blob | 前端 flag 调回旧 API |
| P3 三页灰度 | P2 可按页开关 | Clips → Review → Annotate 逐页迁移及真实浏览器/代理证据 | 每页完成权限、播放、seek、续票、卸载和原功能回归后再放下一页 | 按页关闭新流媒体开关 |
| P4 收口 | 三页稳定观察、回滚演练完成且指标达标 | 删除旧视频 blob 路径，关闭 legacy，更新部署文档；保留上一版前后端可部署制品至少一个稳定发布周期 | 全仓无视频 `res.blob()`；无 ticket 被拒绝；全量测试、人工验收和上一版制品回滚演练通过 | 删除旧前端路径后必须整体回滚到上一版前后端制品；仅重开后端 legacy 开关不足以恢复播放 |

## 9. 测试矩阵

### 9.1 HTTP 与 Range

| 场景 | 主要断言 |
|---|---|
| 无 Range | `200`、完整内容、正确长度/媒体类型、`Accept-Ranges: bytes`、inline、`Cache-Control: private, no-store` |
| `bytes=0-0` | `206`、1 字节、正确 `Content-Range`/`Content-Length` |
| `bytes=N-M` | `206`、闭区间内容和长度正确 |
| `bytes=N-` | `206`、从 N 到文件末尾 |
| `bytes=-N` | `206`、末尾 N 字节 |
| 起点超界 | `416`，验证实际 `Content-Range` |
| 空、倒置、非数字等非法 Range | 按锁定 Starlette 官方实际行为固定状态和错误体，不由业务层二次解析 |
| 多 Range | 按锁定版本实际官方语义回归；支持时验证 multipart boundary/各段，不支持时固定拒绝状态；不得无记录退化为整包 200 |
| HEAD | 按实际支持验证 headers/空 body；若当前 405，记录后决定是否显式支持，不作为浏览器播放前置 |
| `If-Range` | 按实际支持验证匹配/不匹配，不假设未测试的 ETag/Last-Modified 语义 |
| 客户端中断 | 无未处理异常或资源泄漏；后续 Range 可恢复 |

### 9.2 票据、授权与文件安全

- 正常 ticket；签名伪造；过期；未来 `iat`；错误 `aud`、video、user。
- ticket 的 `video_id` 与 URL 不同；`sub` 不存在/不可用；凭据用户混淆。
- 签票后 membership 删除或改为 inactive，下一次 Range 即时失效。
- ticket + Bearer 同时携带只按 ticket；无效 ticket 不回退有效 Bearer。
- 无 ticket 时验证 legacy flag 开/关；非成员、inactive、跨项目、视频不存在。
- 相对/绝对正常路径；`..`、绝对越界、symlink 越界、目录、文件消失和零字节。
- 完整 ticket 不出现在测试快照、应用日志、访问日志、APM 或 metrics label。
- 正常与 Range 响应都包含 `private, no-store`；成员停用、票据过期后不得因浏览器或代理缓存继续读取旧媒体字节。

### 9.3 浏览器与反向代理

- 真实 Chromium/Edge、Firefox；Safari 是否纳入由待确认项决定。
- 三页覆盖首播、暂停/继续、seek、快速 seek、尾部、重播、长时间过期续票和页面卸载。
- 快速切换片段/审核视频/标注视频时，旧签票响应和媒体事件不得回写当前页面。
- AnnotatePage 验证 `?t=`、检测叠加、快捷键、相邻视频导航和状态清理无回归。
- 反代透传 `Range`/`If-Range` 和 `Accept-Ranges`/`Content-Range`/`Content-Length`；206 不改写为 200，不压缩媒体 body，不整包缓冲，客户端中断向上游传播。
- 网关 access log、错误页、trace 和 APM 对 ticket query 脱敏。

## 10. 可观测指标与量化验收

1. 播放时 JS heap 不再出现接近视频文件大小的 blob 增量。
2. 视频播放链路不再出现前端 `res.blob()`；仓库审计应保留缩略图、ZIP 等合理 blob 用途。
3. 网络可观测 `206` 与正确 `Content-Range`；浏览器选择首次 200 仍属允许行为。
4. 对比首次 `loadedmetadata`、首帧、首次 seek 和快速 seek 延迟（P50/P95）及传输量。
5. 对 1 GB 视频只观看 10 秒，前端不得再因 `res.blob()` **强制**下载完整 1 GB；实际传输仍受浏览器策略、码率、容器索引和缓冲影响。
6. 完整 ticket 在 UI、应用/代理日志、APM、trace、错误上报和 metrics label 中出现次数为 0。
7. 原有权限和 ClipsPage、ReviewPage、AnnotatePage 功能全部无回归。

## 11. 风险与控制

| 风险 | 控制 |
|---|---|
| ticket 位于 URL，可能被日志采集 | 短 TTL、最小权限、专用 secret、应用/网关/APM 全链路脱敏、同源 HTTPS |
| 过期打断长时间标注 | 建议 60 分钟；媒体错误单次续票并恢复播放位置 |
| 无效 ticket 降级到 Bearer | 固定“有 ticket 只验 ticket”并专项测试 |
| 依赖漂移改变 Range 行为 | 最低安全版本、constraints/lock、协议矩阵回归 |
| 代理吞 Range 或整包缓冲 | 真实网关测试；206/header/内存/临时盘观测 |
| 浏览器仍下载较多数据 | 正确认知浏览器策略；后续另立低码率播放副本工作 |
| 续票循环或旧请求污染新视频 | 每世代最多一次、请求取消/序号、video ID 校验、卸载清理 |
| 删除 blob 时误伤其他下载 | 只按视频调用链删除；分类审计 object URL 与 `res.blob()` |

## 12. 回滚开关

1. 后端独立 `ticket enabled` 与 `legacy Bearer enabled` 开关。
2. 前端使用全局或按页面的新流媒体能力开关，P3 可单独回退三个页面。
3. P1–P3 旧前端 blob 路径仍存在时，故障可关闭对应新路径并启用 legacy/blob；本方案无业务数据迁移，不需要回滚数据库。
4. 依赖协议回归时恢复 P0/P1 验证过的 lock，不临时手写 Range。
5. P4 删除旧前端路径前必须完成整体制品回滚演练；删除后保留上一版前后端可部署制品至少一个稳定发布周期，故障时整体回滚，不能声称只重开后端 legacy 开关即可恢复。
6. 稳定发布周期结束后才删除 legacy 开关；之后如需恢复旧方案，以正常修复提交实现，不 reset/force push。

## 13. 明确文件清单（后续预计修改，当前均未修改）

### 后端

- `backend/app/routers/videos.py`：helper、签票路由、鉴权优先级、FileResponse headers。
- `backend/app/schemas.py` 或实际视频 schema 模块：`StreamTicketResponse`。
- `backend/app/config.py`（以实际 Settings 文件为准）：TTL、媒体 secret、audience、ticket/legacy flags。
- 可新增 `backend/app/services/video_stream.py`：票据签发/验证和单一授权路径 helper。
- `backend/requirements.txt` 及部署 constraints/lock。
- `backend/tests/test_videos.py`；可新增 `backend/tests/test_video_stream_tickets.py`。
- 反向代理配置及部署文档（以实际部署目录为准）。

### 前端

- `frontend/src/api/index.ts` 及必要类型文件：统一 `getVideoStreamSource`，最终删除视频专用 blob API。
- 可新增 `frontend/src/utils/videoStream.ts` 或统一媒体 hook：加载世代、续票、状态恢复和 URL 脱敏。
- `frontend/src/pages/ClipsPage.tsx`、`ReviewPage.tsx`、`AnnotatePage.tsx` 及对应测试。

### 文档

- 实现完成后更新 `backend/README.md`、`frontend/README.md` 和根 `README.md` 的“未实施”状态及部署配置；实现前不提前写成已完成。
- `工作项日志.md` 只记录关键阶段与证据，不复制完整测试表。

## 14. 实施顺序

1. 执行 P0：固定样本、依赖版本、响应协议、浏览器/代理行为和旧 blob 指标。
2. 确认 TTL、网关脱敏、Safari、feature flag 基础设施四项。
3. 实施后端 helper 与协议测试，再加票据签发/验证和双栈开关。
4. 锁定依赖组合并跑后端专项与全量测试。
5. 实施前端统一能力层和续票状态机，不立即改三页。
6. 按 ClipsPage → ReviewPage → AnnotatePage 逐页灰度和验收。
7. 在真实反向代理、真实长视频和目标浏览器完成量化对比。
8. 达到完成定义后关闭 legacy、删除视频 blob 路径并同步耐久文档。

## 15. 完成定义（Definition of Done）

- 后端 ticket、实时授权、防降级、路径和 Range 测试矩阵通过。
- 显式依赖约束与部署 constraints/lock 固定一组已验证组合。
- 三页全部通过统一能力层使用原生 URL，视频路径无 `res.blob()`/object URL。
- `preload="metadata"`、单次续票、状态恢复、卸载与切换防旧回写经真实浏览器验证。
- Chromium/Edge、Firefox及最终确认的 Safari 范围通过；真实代理正确透传 Range 且不整包缓冲。
- 量化指标达标，ticket 不出现在 UI、日志、APM、trace 或 metrics label。
- 三页业务、权限和安全无回归，后端全量测试与前端生产构建通过。
- legacy 关闭和回滚演练完成；README 与部署文档更新为准确实现状态。

## 16. 实施前待确认项

以下只影响配置或验收范围，不重新讨论已确认架构：

1. ticket TTL 最终值是否采用建议的 60 分钟。
2. 生产网关日志/APM 对 `ticket` query 的具体脱敏方式和验证责任人。
3. Safari 是否属于正式支持矩阵；若属于，明确最低 macOS/iOS 版本和设备。
4. 是否先建立运行时 feature flag 基础设施；若不建立，至少需要可部署配置和按页构建期开关支撑 P3 回滚。

## 17. 官方依据

- Starlette Responses / `FileResponse`：<https://www.starlette.io/responses/#fileresponse>
- Starlette release notes：<https://www.starlette.io/release-notes/>
- MDN `<video>`（含 `crossorigin` 属性）：<https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/video>
- MDN HTTP Range requests：<https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Range_requests>

实施时应复核锁定版本的官方语义；最终验收仍以自动化协议测试、真实浏览器和生产反向代理结果为准。
