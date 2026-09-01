# projects/2 标签更新生产操作说明（待确认模板/本地候选）

> # **命令尚未执行，服务器/DB 未改变**
>
> 本文及配套脚本只是本地候选、待确认执行模板，不是服务器执行记录。任何实际服务器只读或写命令都必须逐条展示、用中文说明用途，并在用户明确确认该条命令后执行；不得把多个服务器命令一次性授权。脚本始终以 `jinghan` 运行，`sudo` 仅用于 `systemctl`。

仓库外 `网站服务器文件清单.md` 截至 2026-08-31 唯一确认的生产基线是 release `50be725743254c0fa55ae3b21de646d457211417`、schema `0016`；本候选包因此固定 backend `/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend`，venv 为该 backend 下 `.venv`。禁止通过 `current` 导入代码。project 2 是否存在/锁定、四类是否缺失、active owner 是否唯一、后台任务是否归零、服务/端口/DB 是否仍有打开者均不是已确认生产事实，必须按下文现场 preflight。若本包执行前先部署了任何候选 release、schema 或触发器定义发生变化，本包立即失效：必须基于届时精确 release/schema/trigger 重新定基线、重新审查并生成执行包，不得沿用本文继续操作。

## 1. 本地上传（待确认）

用途：在本地 PowerShell 计算待上传脚本 SHA256，留作服务器端逐字节核对。

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "<已审查checkout>\deploy\operations\update_project2_categories.py"
```

用途：用户确认后，从本地上传脚本到 annotation server 的用户目录；该动作不运行脚本。

```powershell
scp -i "C:\Users\33800\.ssh\id_ed25519_trackserver" "<已审查checkout>\deploy\operations\update_project2_categories.py" jinghan@101.200.137.83:/home/jinghan/update_project2_categories.py
```

用途：用户确认后，使用同一密钥登录标注服务器，再逐条执行后续服务器命令。

```powershell
ssh -i "C:\Users\33800\.ssh\id_ed25519_trackserver" jinghan@101.200.137.83
```

## 2. 服务器端实施（严格顺序，每条逐步确认）

以下门禁不可跳步：**冻结新写入 → active 后台任务全部归零 → 停服务 → 8000/DB 无打开者 → dry-run → 一致性备份及证据验证 → apply → verify → 启服**。任一步结果不确定或失败都停止。

用途：核对服务器收到的脚本 SHA256，必须与本地值完全一致。

```bash
sha256sum /home/jinghan/update_project2_categories.py
```

用途：先按当次已批准的入口维护方案冻结登录后的所有新写入（包括上传、三文件完成、标注/审核、导出发起及管理操作），并现场验证冻结生效；不得以“稍后停服务”替代此门禁。具体入口变更命令必须按服务器实时配置另行逐条确认，本模板不猜测。

用途：服务仍运行且入口已冻结时，只读确认 display、media、export、cleanup 及其他类型的 `queued`/`running` 后台任务总数全部为 0；查询有任何结果、任务类型/状态无法解释或命令失败都不得停服后继续。

```bash
sqlite3 -readonly -header -column /data/mouse-annotation/data/annotation.db "SELECT job_type,status,count(*) AS count FROM background_jobs WHERE status IN ('queued','running') GROUP BY job_type,status ORDER BY job_type,status;"
```

用途：停止单进程网站服务，进入维护窗口；`sudo` 仅用于本条 systemd 操作。

```bash
sudo systemctl stop mouse-annotation.service
```

用途：确认服务已处于 inactive；结果不是 `inactive` 就停止后续操作。

```bash
systemctl is-active mouse-annotation.service
```

用途：确认 8000 端口没有监听者。

```bash
ss -ltnp 'sport = :8000'
```

用途：先确认只读占用检查工具 `lsof` 已安装；若命令失败或无输出，就停止并另行确定只读占用检查命令，不得继续假设 `lsof` 可用。

```bash
command -v lsof
```

用途：确认生产数据库及实际存在的 sidecar 没有任何打开者；数组只纳入存在的 WAL/SHM，避免把“可选路径不存在”误判为占用检查异常。仅在上一条确认 `lsof` 可用后执行；输出非空就停止，`lsof` 对“无打开者”返回 1 可接受，其他错误必须停止排查。

```bash
db_paths=(/data/mouse-annotation/data/annotation.db); for sidecar in /data/mouse-annotation/data/annotation.db-wal /data/mouse-annotation/data/annotation.db-shm; do [[ ! -e "$sidecar" ]] || db_paths+=("$sidecar"); done; lsof -- "${db_paths[@]}"
```

用途：核对 `current` 精确指向固定 release；后续脚本仍不用 `current`。

```bash
readlink -f /opt/mouse-annotation/current
```

用途：单独核对固定 release backend 目录真实存在。

```bash
test -d /opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend
```

用途：核对固定 release 的 Git HEAD，输出必须是完整固定提交。

```bash
git -C /opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417 rev-parse HEAD
```

用途：在 dry-run 前单独确认固定 release 工作树完全 clean（包括未跟踪文件）；必须无输出，命令失败或出现任何输出都停止。

```bash
git -C /opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417 status --porcelain --untracked-files=all
```

用途：逐字节语义地确认固定 release 工作树中的两组 schema 0016 触发器定义与提交 `50be725743254c0fa55ae3b21de646d457211417` 一致；必须无 diff 且退出码为 0，否则停止。

```bash
git -C /opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417 diff --exit-code 50be725743254c0fa55ae3b21de646d457211417 -- backend/app/authority_triggers.py backend/app/assignee_triggers.py
```

用途：核对固定 release venv Python 可执行文件存在。

```bash
test -x /opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python
```

用途：单独核对生产数据库路径存在且是文件。

```bash
test -f /data/mouse-annotation/data/annotation.db
```

用途：用固定 release venv 只读 dry-run；现场确认 schema `0016`、project 2 恰好存在且已锁定、四类全部缺失、唯一 active owner、既有类别/role key 与全部 trigger 定义，并保存输出中的可复制 `fingerprint`。任何失败均保持停服；不得把计划中的预期当成现场事实。

```bash
/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python /home/jinghan/update_project2_categories.py
```

用途：生成 UTC 备份文件名；单独记录输出值，后续逐条替换 `<UTC>`。

```bash
date -u +%Y%m%dT%H%M%SZ
```

用途：用 SQLite backup API 创建独立一致性备份，不修改源库、不执行 checkpoint；目标必须预先不存在。

```bash
/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python /home/jinghan/update_project2_categories.py --backup /data/mouse-annotation/backups/pre-project2-categories-<UTC>.db
```

用途：独立复核备份文件 SHA256，并与脚本刚输出的值一致。

```bash
sha256sum /data/mouse-annotation/backups/pre-project2-categories-<UTC>.db
```

用途：携 dry-run 的 `<FINGERPRINT>` 执行唯一写事务，并再次显式确认生产 DB、固定 release、已验证备份路径与 SHA256。脚本会重新读取备份、核对 SHA256 和基线 fingerprint；缺少或不匹配时拒绝 apply，不能只凭本文勾选跳过备份门禁。任何非零或 `KEEP_SERVICE_STOPPED` 都不得启服。

```bash
/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python /home/jinghan/update_project2_categories.py --apply --expect-fingerprint <FINGERPRINT> --confirm-production-db /data/mouse-annotation/data/annotation.db --confirm-release 50be725743254c0fa55ae3b21de646d457211417 --confirm-backup /data/mouse-annotation/backups/pre-project2-categories-<UTC>.db --confirm-backup-sha256 <BACKUP_SHA256>
```

用途：以全新只读连接复核四类、最新 replace audit/hash/current snapshot、两组固定 release 定义并集中的全部触发器（名称集合精确相等且规范化 SQL 逐条一致）及数据库完整性。

```bash
/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python /home/jinghan/update_project2_categories.py --verify
```

用途：只读查询 schema、project 2 锁/version、四类关键字段及最新 replace audit 摘要；这不是修改数据库的裸 SQL 主方案。

```bash
sqlite3 -readonly -header -column /data/mouse-annotation/data/annotation.db "SELECT version_num FROM alembic_version; SELECT id,category_scheme_version,category_scheme_locked_at,category_scheme_locked_by FROM projects WHERE id=2; SELECT name,\"group\",color,sort_order,is_active,mouse_count_min,mouse_count_max,participant_mode FROM behavior_categories WHERE project_id=2 AND name IN ('Following','Group locomotion','Social clustering','Dispersal') ORDER BY sort_order,id; SELECT id,actor_id,action,scheme_version,scheme_hash,created_at FROM category_scheme_audits WHERE project_id=2 AND action='replace' ORDER BY created_at DESC,id DESC LIMIT 1;"
```

用途：启服前最终只读门禁，重复确认服务 inactive；此前固定 release/schema、备份 SHA 和 `--verify` 也必须全部通过。

```bash
systemctl is-active mouse-annotation.service
```

用途：启服前再次确认 8000 无监听。

```bash
ss -ltnp 'sport = :8000'
```

用途：启服前再次确认 `current` 仍指向固定 release。

```bash
readlink -f /opt/mouse-annotation/current
```

用途：所有门禁通过且用户再次确认后启动服务；`sudo` 仅用于本条 systemd 操作。

```bash
sudo systemctl start mouse-annotation.service
```

用途：确认服务已变为 active。

```bash
systemctl is-active mouse-annotation.service
```

用途：确认服务只监听本机 8000。

```bash
ss -ltnp 'sport = :8000'
```

用途：检查本机健康接口，不携带 token 或环境变量。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
```

随后由人工登录生产页面验收：project 2 四类顺序/颜色；Following 的 Follower/Leader 各一只；三个群体类至少三只；旧类别、快捷键、标注、Review 与导出无回归。验收结果另行记录，不能由本模板预先声称通过。

## 3. 备份恢复回滚（已提交后首选，仍逐条确认）

发现异常时保持停服，不首选手工删四行。以下 `<UTC>`、`<FAILED-UTC>` 必须替换为本次真实值。

用途：确认服务仍 inactive。

```bash
systemctl is-active mouse-annotation.service
```

用途：在任何回滚取证、移动或恢复操作前，再次确认 8000 端口无监听；命令失败或有输出都停止。

```bash
ss -ltnp 'sport = :8000'
```

用途：再次确认只读占用检查工具 `lsof` 可用；命令失败或无输出就停止。

```bash
command -v lsof
```

用途：在任何回滚取证、移动或恢复操作前，再次确认生产数据库及实际存在的 WAL/SHM 无打开者；数组不纳入不存在的可选 sidecar。有任何输出或检查异常都停止，不得执行后续破坏性操作；`lsof` 对“无打开者”返回 1 可接受。

```bash
db_paths=(/data/mouse-annotation/data/annotation.db); for sidecar in /data/mouse-annotation/data/annotation.db-wal /data/mouse-annotation/data/annotation.db-shm; do [[ ! -e "$sidecar" ]] || db_paths+=("$sidecar"); done; lsof -- "${db_paths[@]}"
```

用途：创建失败库取证目录，目录必须预先不存在。

```bash
mkdir /data/mouse-annotation/backups/failed-project2-<FAILED-UTC>
```

用途：保存失败主库作为取证文件，避免覆盖证据。

```bash
cp -a /data/mouse-annotation/data/annotation.db /data/mouse-annotation/backups/failed-project2-<FAILED-UTC>/
```

用途：若失败库存在 WAL sidecar，则另行保存；文件不存在时不执行本条。

```bash
cp -a /data/mouse-annotation/data/annotation.db-wal /data/mouse-annotation/backups/failed-project2-<FAILED-UTC>/
```

用途：若失败库存在 SHM sidecar，则另行保存；文件不存在时不执行本条。

```bash
cp -a /data/mouse-annotation/data/annotation.db-shm /data/mouse-annotation/backups/failed-project2-<FAILED-UTC>/
```

用途：重新核对实施前备份 SHA256，必须与先前记录一致。

```bash
sha256sum /data/mouse-annotation/backups/pre-project2-categories-<UTC>.db
```

用途：创建待移走数据库的取证目录，目录必须预先不存在。

```bash
mkdir /data/mouse-annotation/backups/displaced-project2-<FAILED-UTC>
```

用途：移走主库，不直接删除。

```bash
mv /data/mouse-annotation/data/annotation.db /data/mouse-annotation/backups/displaced-project2-<FAILED-UTC>/
```

用途：若 WAL sidecar 存在，则另行移走；文件不存在时不执行本条。

```bash
mv /data/mouse-annotation/data/annotation.db-wal /data/mouse-annotation/backups/displaced-project2-<FAILED-UTC>/
```

用途：若 SHM sidecar 存在，则另行移走；文件不存在时不执行本条。

```bash
mv /data/mouse-annotation/data/annotation.db-shm /data/mouse-annotation/backups/displaced-project2-<FAILED-UTC>/
```

用途：从已验证备份恢复主库。

```bash
cp /data/mouse-annotation/backups/pre-project2-categories-<UTC>.db /data/mouse-annotation/data/annotation.db
```

用途：恢复生产数据库所有者和组；脚本和文件均由 `jinghan` 管理。

```bash
chown jinghan:jinghan /data/mouse-annotation/data/annotation.db
```

用途：单独恢复生产数据库 0640 权限。

```bash
chmod 0640 /data/mouse-annotation/data/annotation.db
```

用途：用固定 release 只读 dry-run 复检恢复库完整性、schema 与两组固定 release 定义并集中的全部触发器（名称集合精确相等且规范化 SQL 逐条一致）；应回到实施前 `absent`，而非运行要求四类存在的 `--verify`。

```bash
/opt/mouse-annotation/releases/50be725743254c0fa55ae3b21de646d457211417/backend/.venv/bin/python /home/jinghan/update_project2_categories.py
```

用途：独立只读确认恢复库 quick check、foreign key 与 schema `0016`。

```bash
sqlite3 -readonly /data/mouse-annotation/data/annotation.db "PRAGMA quick_check; PRAGMA foreign_key_check; SELECT version_num FROM alembic_version; SELECT count(*) FROM sqlite_master WHERE type='trigger';"
```

用途：仅在恢复检查全部通过并经用户确认后重新启动服务。

```bash
sudo systemctl start mouse-annotation.service
```

用途：回滚后复核服务 active。

```bash
systemctl is-active mouse-annotation.service
```

用途：回滚后复核本机健康接口。

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
```
