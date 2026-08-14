# 网站服务器生产部署

本目录提供网站的生产部署配置模板，不代表部署已经执行。唯一服务器依据是仓库外的 `网站服务器文件清单.md`。任何实际服务器命令（包括只读命令）执行前，都必须逐条展示命令、用中文说明用途并获得用户明确确认。

## 路径与当前边界

- 已确认存在：`/opt/mouse-annotation/`、`/opt/mouse-annotation/releases/`、`/data/mouse-annotation/data/`、`/data/mouse-annotation/backups/`。
- 计划发布目录：`/opt/mouse-annotation/releases/<commit>/`；尚未创建。
- 计划软链接：`/opt/mouse-annotation/current`；尚未创建。
- 计划数据库：`/data/mouse-annotation/data/annotation.db`；尚未创建。
- Nginx、网站代码、数据库、域名站点和 HTTPS 的实际部署状态以服务器清单为准；本目录只维护部署模板和操作流程。

运行时数据固定放在 `/data/mouse-annotation/data/`，备份目标放在 `/data/mouse-annotation/backups/`；发布代码和每个发布版本自己的虚拟环境放在 `/opt/mouse-annotation/releases/<commit>/`。

## 部署顺序

以下是操作顺序，不是已执行记录；每一步涉及的服务器命令仍须单独确认后才能执行。

1. 将指定提交检出到新的 `releases/<commit>/`，不要直接覆盖当前版本。
2. 在该 release 的 `frontend/` 安装锁定依赖并运行 `npm run build`；生产构建读取已提交的 `.env.production`，API 基址为同源 `/api`。
3. 在该 release 的 `backend/` 创建 `.venv` 并安装 `requirements.txt`。
4. 以 `backend/.env.production.example` 为参考，在计划部署时创建稳定环境文件 `/home/jinghan/.config/mouse-annotation/backend.env`，所有者/组为 `jinghan:jinghan`，权限为 `0600`，并生成实际 `SECRET_KEY` 和生产账号凭据。该文件不提交，也不复制到任何 release；模板中的 `CHANGE_ME` 会被生产配置验证硬拒绝。
5. 从该 release 的 `backend/` 工作目录显式运行 `.venv/bin/python scripts/migrate.py`，运行前先执行 `umask 0027`，再让当前进程导入上述稳定环境文件。首次迁移创建的生产数据库 `annotation.db` 目标权限为 `0640`；`/data/mouse-annotation/data/` 及其运行时子目录保持 `0750`。若数据库已经存在，还须将 `annotation.db` 及 SQLite sidecar（如 `annotation.db-wal`、`annotation.db-shm`）收紧至 `0640`，并检查已有 media/log：普通文件采用 `0640`、目录采用 `0750`，一律禁止 other 访问。迁移脚本会实例化同一 `Settings`，因此无效生产凭据会在接触数据库前失败。Pydantic 从当前工作目录读取的 `.env` 只方便开发，不能作为生产 secret 方案；secret env 仍保持 `0600`。
6. 迁移和构建成功后，才将 `/opt/mouse-annotation/current` 原子切换到该 release。
7. 部署时先创建 `/data/mouse-annotation/tmp`，所有者/组为 `jinghan:jinghan`、权限为 `0750`，再安装 `deploy/mouse-annotation.service` 并由 systemd 启动后端。`WorkingDirectory` 保持为 `current/backend`，systemd 从稳定路径 `/home/jinghan/.config/mouse-annotation/backend.env` 注入环境，并设置 `TMPDIR=/data/mouse-annotation/tmp`；Starlette multipart 对超过 1 MiB 的上传会在解析请求体时 spool 到该数据盘临时目录，之后后端才按块写入最终 `/data/mouse-annotation/data/` 目标，并非从网络直接流式写入最终文件。`ExecStartPre` 会拒绝在临时目录缺失时启动。服务以 `UMask=0027` 确保新建数据库 sidecar、media、log 等默认不向 other 开放；Uvicorn 仅监听 `127.0.0.1:8000` 且保持单进程。
8. 先通过 SSH 端口转发访问 `127.0.0.1:8000` 完成后端验收。
9. 使用管理员提供的已签发 PEM 证书链和匹配私钥，不使用 Certbot，也不要将证书或私钥放入仓库。将证书安全安装到 `/etc/nginx/ssl/jerrylab.xyz.pem`，所有者/组设为 `root:root`、权限设为 `0644`；将私钥安全安装到 `/etc/nginx/ssl/jerrylab.xyz.key`，所有者/组设为 `root:root`、权限设为 `0600`。安装时避免在命令历史、日志或宽权限临时文件中暴露私钥，并在启用前核对证书链、域名和私钥匹配关系。
10. 安装并启用 `deploy/nginx/mouse-annotation.conf`。模板只将域名 HTTP 请求以 `308` 固定重定向到 `https://jerrylab.xyz`，未知 Host 在 80/443 均返回 `444`；HTTPS 提供 SPA fallback 和 API 反向代理，代理 Host 固定为 `jerrylab.xyz`。Nginx 通过 internal `/_auth` 调用 `/api/auth/me` 执行 `auth_request` 前置认证；公开例外仅为精确路径 `/api/health` 和 `/api/auth/login`。普通 API 请求体上限为 `10 MiB`。`20 GiB` 只适用于以下实际上传路由，并且每个客户端 IP 最多同时 2 个上传连接：`/api/projects/{project_id}/videos/upload`、`/api/projects/{project_id}/video-import-batches/{batch_id}/files/{video|tracks|metadata}`、`/api/projects/{project_id}/videos/{video_id}/detection-imports`。只有这些上传 location 关闭 Nginx 请求缓冲；multipart 大于 1 MiB 的请求仍会由应用解析器 spool 到上述 `TMPDIR`。请求体、发送及代理读写超时保持 600 秒。只有真实数据确需更大且已评估数据盘容量和风险时，才审慎调高上传上限。
11. 配置和证书就位后，必须先运行 `nginx -t`；仅在配置检查通过后才 reload Nginx。随后完整验证 HTTP 到 HTTPS 的重定向、HTTPS 证书链、SPA fallback、大文件入口限制和 `/api` 反向代理。暂不启用 HSTS，待 HTTPS 完整验证后再决定，避免错误配置被浏览器长期锁定。
12. 人工证书需要在到期前由管理员更换；更换时仍须核对证书链、域名和私钥匹配关系，保持上述所有者与权限，并遵循“先 `nginx -t`、通过后 reload”的顺序。证书是否已安装及当前证书有效期只记录在服务器清单中，不能作为仓库通用模板文档中的既成事实。
13. 部署完成后按既定策略配置并验证 `/data/mouse-annotation/backups/` 下的数据库一致性备份和媒体备份；未验证前不得声称备份已生效。

回滚时将 `current` 切回已验证 release，并重启单进程服务；涉及数据库版本不兼容时，应先依据对应迁移与备份制定回滚步骤，不能仅切换代码。
