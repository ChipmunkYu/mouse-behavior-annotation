# 网站服务器生产部署

本目录提供网站的生产部署配置模板，不代表部署已经执行。唯一服务器依据是仓库外的 `网站服务器文件清单.md`。任何实际服务器命令（包括只读命令）执行前，都必须逐条展示命令、用中文说明用途并获得用户明确确认。

## 路径与当前边界

- 已确认存在：`/opt/mouse-annotation/`、`/opt/mouse-annotation/releases/`、`/data/mouse-annotation/data/`、`/data/mouse-annotation/backups/`。
- 计划发布目录：`/opt/mouse-annotation/releases/<commit>/`；尚未创建。
- 计划软链接：`/opt/mouse-annotation/current`；尚未创建。
- 计划数据库：`/data/mouse-annotation/data/annotation.db`；尚未创建。
- Nginx 当前未启动，网站代码、数据库、域名站点和 HTTPS 均尚未部署或配置。

运行时数据固定放在 `/data/mouse-annotation/data/`，备份目标放在 `/data/mouse-annotation/backups/`；发布代码和每个发布版本自己的虚拟环境放在 `/opt/mouse-annotation/releases/<commit>/`。

## 部署顺序

以下是操作顺序，不是已执行记录；每一步涉及的服务器命令仍须单独确认后才能执行。

1. 将指定提交检出到新的 `releases/<commit>/`，不要直接覆盖当前版本。
2. 在该 release 的 `frontend/` 安装锁定依赖并运行 `npm run build`；生产构建读取已提交的 `.env.production`，API 基址为同源 `/api`。
3. 在该 release 的 `backend/` 创建 `.venv` 并安装 `requirements.txt`。
4. 以 `backend/.env.production.example` 为参考，在计划部署时创建稳定环境文件 `/home/jinghan/.config/mouse-annotation/backend.env`，所有者/组为 `jinghan:jinghan`，权限为 `0600`，并生成实际 `SECRET_KEY` 和生产账号凭据。该文件不提交，也不复制到任何 release；模板中的 `CHANGE_ME` 会被生产配置验证硬拒绝。
5. 从该 release 的 `backend/` 工作目录显式运行 `.venv/bin/python scripts/migrate.py`，运行前先执行 `umask 0027`，再让当前进程导入上述稳定环境文件。首次迁移创建的生产数据库 `annotation.db` 目标权限为 `0640`；`/data/mouse-annotation/data/` 及其运行时子目录保持 `0750`。若数据库已经存在，还须将 `annotation.db` 及 SQLite sidecar（如 `annotation.db-wal`、`annotation.db-shm`）收紧至 `0640`，并检查已有 media/log：普通文件采用 `0640`、目录采用 `0750`，一律禁止 other 访问。迁移脚本会实例化同一 `Settings`，因此无效生产凭据会在接触数据库前失败。Pydantic 从当前工作目录读取的 `.env` 只方便开发，不能作为生产 secret 方案；secret env 仍保持 `0600`。
6. 迁移和构建成功后，才将 `/opt/mouse-annotation/current` 原子切换到该 release。
7. 安装 `deploy/mouse-annotation.service` 后由 systemd 启动后端。`WorkingDirectory` 保持为 `current/backend`，systemd 从稳定路径 `/home/jinghan/.config/mouse-annotation/backend.env` 注入环境，并以 `UMask=0027` 确保新建数据库 sidecar、media、log 等默认不向 other 开放；Uvicorn 仅监听 `127.0.0.1:8000` 且保持单进程。
8. 先通过 SSH 端口转发访问 `127.0.0.1:8000` 完成后端验收。
9. 安装并启用 `deploy/nginx/mouse-annotation.conf`，先使用 `jerrylab.xyz` 的 HTTP 模板验证 Nginx、SPA fallback 和 `/api` 反向代理。模板保留 `/api` 前缀、关闭请求缓冲，并将请求体、发送及代理读写超时设为 600 秒。`20 GiB` 是 Nginx 入口的初始可调上限；后端仍流式写入 `/data` 并在写入过程中检查磁盘空间。只有真实数据确需更大且已评估数据盘容量和风险时，才审慎调高该上限。
10. HTTP 验证通过后再由 certbot 申请证书并修改 Nginx，最终以 `https://jerrylab.xyz` 验收。仓库中的模板不表示 HTTPS 已启用。
11. 部署完成后按既定策略配置并验证 `/data/mouse-annotation/backups/` 下的数据库一致性备份和媒体备份；未验证前不得声称备份已生效。

回滚时将 `current` 切回已验证 release，并重启单进程服务；涉及数据库版本不兼容时，应先依据对应迁移与备份制定回滚步骤，不能仅切换代码。
