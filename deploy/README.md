# 网站服务器生产部署

首次部署、故障复盘、日常运维、发布与回滚详见 [`部署复盘与运维指南.md`](部署复盘与运维指南.md)。

本目录维护网站的生产部署配置模板和操作流程；模板描述不用于断言服务器实时状态。唯一服务器事实依据是仓库外的 `网站服务器文件清单.md`。任何实际服务器命令（包括只读命令）执行前，都必须逐条展示命令、用中文说明用途并获得用户明确确认。

专项生产运维候选入口见 [`operations/`](operations/)；其中 projects/2 操作说明与脚本只是待逐步确认的本地模板，不是服务器或数据库执行记录。

## 路径与当前边界

- 部署根目录：`/opt/mouse-annotation/`；固定提交发布到 `/opt/mouse-annotation/releases/<commit>/`。
- 当前版本入口：`/opt/mouse-annotation/current`；运行时数据：`/data/mouse-annotation/data/`；备份目标：`/data/mouse-annotation/backups/`。
- Nginx、网站代码、数据库、域名站点和 HTTPS 的实际部署状态以服务器清单为准，不在通用命令模板中重复维护当前值。

运行时数据固定放在 `/data/mouse-annotation/data/`，备份目标放在 `/data/mouse-annotation/backups/`；发布代码和每个发布版本自己的虚拟环境放在 `/opt/mouse-annotation/releases/<commit>/`。

## 常规部署顺序

以下是操作顺序，不是已执行记录；每一步涉及的服务器命令仍须单独确认后才能执行。

1. 将指定提交检出到新的 `releases/<commit>/`，不要直接覆盖当前版本。
2. 在该 release 的 `frontend/` 安装锁定依赖并运行 `npm run build`；生产构建读取已提交的 `.env.production`，API 基址为同源 `/api`。
3. 在该 release 的 `backend/` 创建 `.venv` 并安装 `requirements.txt`。
4. 以 `backend/.env.production.example` 为参考，在计划部署时创建稳定环境文件 `/home/jinghan/.config/mouse-annotation/backend.env`，所有者/组为 `jinghan:jinghan`，权限为 `0600`，并生成实际 `SECRET_KEY` 和生产账号凭据。该文件不提交，也不复制到任何 release；模板中的 `CHANGE_ME` 会被生产配置验证硬拒绝。
5. 已有生产实例升级时，先冻结应用入口和所有新写入，再等待 display、media、export、cleanup 等相关任务全部进入 `succeeded`、`failed` 或 `cancelled` 终态；不得只看 display job。随后停止唯一 `mouse-annotation.service`，并同时确认 unit inactive、Uvicorn 进程退出且 8000 不再监听。首次部署没有既有服务时也要确认不存在残留实例。
6. 仅在 worker 已完全退出后，创建 SQLite 一致性备份并对备份执行 `PRAGMA integrity_check`；然后才允许迁移或旧数据清理。禁止在任何 migration、downgrade、旧数据处理或清理期间让后台 worker 运行。
7. 从该 release 的 `backend/` 工作目录显式运行 `.venv/bin/python scripts/migrate.py`，运行前执行 `umask 0027`，并按运维指南使用 Python 安全解析稳定 env；严禁 `source backend.env`、`. backend.env` 或 `set -a`。首次迁移创建的 `annotation.db` 目标权限为 `0640`；运行时目录保持 `0750`，数据库 sidecar、media/log 普通文件为 `0640`，一律禁止 other 访问。Pydantic 从工作目录读取的 `.env` 只方便开发，不能作为生产 secret 方案。
8. 迁移、旧数据处理及检查成功后，才将 `/opt/mouse-annotation/current` 原子切换到该 release。
9. 部署时先创建 `/data/mouse-annotation/tmp`，所有者/组为 `jinghan:jinghan`、权限为 `0750`，再安装 `deploy/mouse-annotation.service`。`WorkingDirectory` 保持为 `current/backend`，systemd 从稳定路径 `/home/jinghan/.config/mouse-annotation/backend.env` 注入环境，并设置 `TMPDIR=/data/mouse-annotation/tmp`；Starlette multipart 对超过 1 MiB 的上传会在解析请求体时 spool 到该数据盘临时目录。`ExecStartPre` 会拒绝临时目录缺失，`UMask=0027` 保护新文件；最终只能启动单实例、单 Uvicorn worker。
10. 先通过 SSH 端口转发访问 `127.0.0.1:8000` 完成后端验收。
11. 使用管理员提供的已签发 PEM 证书链和匹配私钥，不使用 Certbot，也不要将证书或私钥放入仓库。将证书安全安装到 `/etc/nginx/ssl/jerrylab.xyz.pem`，所有者/组设为 `root:root`、权限设为 `0644`；将私钥安全安装到 `/etc/nginx/ssl/jerrylab.xyz.key`，所有者/组设为 `root:root`、权限设为 `0600`。安装时避免在命令历史、日志或宽权限临时文件中暴露私钥，并在启用前核对证书链、域名和私钥匹配关系。
12. 安装并启用 `deploy/nginx/mouse-annotation.conf`。模板只将域名 HTTP 请求以 `308` 固定重定向到 `https://jerrylab.xyz`，未知 Host 在 80/443 均返回 `444`；HTTPS 提供 SPA fallback 和 API 反向代理，代理 Host 固定为 `jerrylab.xyz`。Nginx 通过 internal `/_auth` 调用 `/api/auth/me` 执行 `auth_request` 前置认证；公开例外仅为精确路径 `/api/health` 和 `/api/auth/login`。普通 API 请求体上限为 `10 MiB`。以下认证上传路由由 Nginx 施加 `20 GiB` 请求体上限（`client_max_body_size 20g`），应用层不设额外固定文件大小上限，并且每个客户端 IP 最多同时 2 个上传连接：`/api/projects/{project_id}/videos/upload`、`/api/projects/{project_id}/video-import-batches/{batch_id}/files/{video|tracks|metadata}`、`/api/projects/{project_id}/videos/{video_id}/detection-imports`。只有这些上传 location 关闭 Nginx 请求缓冲；multipart 大于 1 MiB 的请求仍会由应用解析器 spool 到上述 `TMPDIR`。`/_auth` 的 `client_max_body_size 0` 仅用于防止鉴权子请求继承大 `Content-Length` 后触发 1 MiB 413，不会取消实际上传 location 的 20 GiB 上限。调整该上限前必须先评估业务最大文件和数据盘余量；上传还受连接数、600 秒请求体/发送/代理读写超时及应用磁盘安全余量约束。
13. 配置和证书就位后，必须先运行 `nginx -t`；仅在配置检查通过后才 reload Nginx。随后完整验证 HTTP 到 HTTPS 的重定向、HTTPS 证书链、SPA fallback、大文件入口限制和 `/api` 反向代理。暂不启用 HSTS，待 HTTPS 完整验证后再决定，避免错误配置被浏览器长期锁定。
14. 人工证书需要在到期前由管理员更换；更换时仍须核对证书链、域名和私钥匹配关系，保持上述所有者与权限，并遵循“先 `nginx -t`、通过后 reload”的顺序。证书是否已安装及当前证书有效期只记录在服务器清单中，不能作为仓库通用模板文档中的既成事实。
15. 部署完成后按既定策略配置并验证 `/data/mouse-annotation/backups/` 下的数据库一致性备份和媒体备份；未验证前不得声称备份已生效。

回滚时将 `current` 切回已验证 release，并重启单进程服务；涉及数据库版本不兼容时，应先依据对应迁移与备份制定回滚步骤，不能仅切换代码。

## P4 低码率展示代理生产状态与部署基线

P4 与导出热修已以 `50be725743254c0fa55ae3b21de646d457211417` 部署至生产，schema 为 `0016`；service active、health ok。`2751e98d180ee034dce64e9ca7b4e47722bb8c7d` 仅是加载进度前端 release 和前一生产版本。用户公网人工已确认上传、预览、标注、审核和 ZIP 导出可用；上线后 Job 12 `succeeded`，结果为 `export_project_1_12.zip`。成功切换前旧生产完整回滚备份为 `/data/mouse-annotation/backups/pre-e77-20260831T040937Z/`，旧数据不迁移。以下顺序保留为后续发布基线；P4 必须使用维护窗口，不得滚动发布或运行双实例，实时状态与服务器文件路径仍以“网站服务器文件清单”为准：

1. 固定完整 commit 并创建不可变 release；安装后端锁定依赖、`pip check`，再完成前端 `npm ci`、检查和 production build。
2. 冻结应用入口、新视频上传、三文件完成及其他新写入；等待 display、media、export、cleanup 等相关任务全部进入已知终态。
3. 停止唯一 `mouse-annotation.service`，确认 unit inactive、Uvicorn 已退出且 8000 不再监听；除下述紧急停机遗留任务的封闭恢复外，从此直到最后启动禁止任何后台 worker 运行。
4. 创建 SQLite 一致性备份，对备份执行 `PRAGMA integrity_check` 并记录路径、权限与结果。
5. 显式迁移到候选 head `0016` 并核对 revision；再按已确认策略处理旧 file-backed 数据。实际删除前仍须再次明确目标、范围、备份、不可逆影响并留证。
6. 创建 `DATA_DIR/display_proxies/`，核对 owner/group、`0750` 权限、同文件系统原子发布条件和磁盘余量。没有 `PROXIES_DIR` 配置。
7. 保持服务停止和写入冻结，按运维指南使用带显式参数 `--env-file /home/jinghan/.config/mouse-annotation/backend.env` 的安全 Python wrapper 运行 `scripts/display_proxy_preflight.py`。wrapper 必须先核对 `ENV=production`、生产 `DATA_DIR`/数据库目标及开关仍为 `false`，不得输出任何 env 值或 secret。禁止 `source backend.env`、`. backend.env` 或 `set -a`。预检退出码 `0`=可切 strict、`1`=存在未 ready/不一致数据、`2`=配置/数据库/检查错误；非 `0` 一律阻断。
8. 仅在空库或全部 ready 库将稳定 env 的 `DISPLAY_PROXIES_ENABLED` 改为 `true`；同时保持 timeout `3600`、max attempts `3`、synchronous `false`、disk reserve `1073741824`。
9. 原子切换 release，只启动单实例、单 Uvicorn worker。不得让新旧 release 或两个 worker 同时拥有任务。
10. 入口继续冻结，上传代表视频并等待 ready；验证 Preview、Clips、Review、Annotate 完整下载代理，pending/failed 409，片段/Submission/导出仍读原片，并检查日志、代理/temp、磁盘、CPU、内存和 I/O；验收通过后才解除冻结。

若紧急停机后遗留 `queued`/`running` display job，禁止手工修改数据库状态。外部入口和所有写入必须继续冻结，临时使用**同版本、`DISPLAY_PROXIES_ENABLED=true`、唯一服务、单 Uvicorn worker**启动 recovery/worker，且全程不得向用户开放。逐项监测遗留任务直至进入已知终态；任何任务失败、无法可靠进入终态或状态未知，都必须阻断后续流程并转入专项处理。任务成功收敛后再次停止该唯一服务，确认 unit inactive、Uvicorn 已退出且 8000 不再监听，才回到第 4 步继续备份、迁移、preflight 和正式启动。不得启动第二实例或用临时恢复服务与其他实例并行。

唯一开关关闭时会播放原片且新视频不 hash/入队；因此关闭期间必须冻结新视频写入，不能把它当成长期兼容模式。不支持历史 backfill，也没有独立 fallback。

### P4 回滚

- 回滚前冻结上传并等待相关任务进入 terminal，再停止唯一服务。紧急停机留下 active display job 时，必须按上文同版本、`DISPLAY_PROXIES_ENABLED=true`、唯一服务且单 Uvicorn worker 的封闭恢复流程收敛任务；不得向用户开放、手工改 DB、启动第二实例，或在 active job 未处理时继续迁移/清理。
- 一级事故恢复：同 release 设置 `DISPLAY_PROXIES_ENABLED=false` 并重启，只临时恢复原片读取；必须继续冻结新视频写入，保留 `0016` 与代理资产排障。
- 二级旧 release：停服务后先将 schema 按 `0016 → 0015` 处理，或恢复上线前一致性备份，再切换旧 release。迁移后产生的写入可能丢失，执行前必须明确数据丢失窗口并再次确认。

服务器 FFmpeg/ffprobe 4.4.2 的 ordinal CFR 已直接处理 401010242-byte、5402 帧真实 VFR 源；帧间隔 33.300–33.334ms、最大 ordinal PTS 误差约 0.017ms且完整解码通过。真实 ENOSPC、并发 CPU/RSS/I/O、Chrome/Edge/Firefox 目标大 Blob取消/切换，以及更多秋采时长与叠加抽查仍未完成；不得宣称这些扩展验收已通过。

完整的上线检查清单、故障定位决策树和本次真实验收证据见 [`部署复盘与运维指南.md`](部署复盘与运维指南.md)。
