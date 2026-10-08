# Project 2 分类法迁移运维流程

> 类型：当前运维流程
> 权威范围：Project 2 一次性分类法迁移、Clip 软退役与整体回滚

脚本固定检查 project `2`、schema `0019`、16 类初始状态及实时标注总数 `1734`（13/14/19/25 分别 `123/160/41/50`）。变更为 Moving 合并、停用 14/19/25、新增 Grooming/Rearing、15 个 active 类别、保留历史审核并将全部存活实时标注退回待审核。项目 2 既有 Clip 全部软退役；行和媒体路径不删除。

## 执行

以下命令必须在生产 release 的虚拟环境中执行。`<RELEASE>` 是部署目录名，也是该目录 `git rev-parse HEAD`；脚本会现场自检，不在代码中预写未知提交。

```bash
# 1. 在线只读预演，保存输出的 fingerprint 和 plan_hash
python deploy/operations/update_project2_categories.py plan \
  --db /data/mouse-annotation/data/annotation.db \
  --release-backend /opt/mouse-annotation/releases/<RELEASE>/backend

# 2. 在线创建 SQLite 一致性备份；脚本执行 quick_check、foreign_key_check 和 SHA256
python deploy/operations/update_project2_categories.py backup \
  --db /data/mouse-annotation/data/annotation.db \
  --release-backend /opt/mouse-annotation/releases/<RELEASE>/backend \
  --destination /data/mouse-annotation/backups/project2-taxonomy-<UTC>.db

# 3. 停止网站及 worker，确认数据库无写入者，然后升级 schema
cd /opt/mouse-annotation/releases/<RELEASE>/backend
.venv/bin/alembic upgrade 0019

# 4. 单事务 apply；使用步骤 1/2 的原样输出
.venv/bin/python ../deploy/operations/update_project2_categories.py apply \
  --db /data/mouse-annotation/data/annotation.db --release-backend "$PWD" \
  --expect-fingerprint <FINGERPRINT> --expect-plan-hash <PLAN_HASH> \
  --confirm-release <RELEASE> --confirm-schema 0019 --confirm-project 2 \
  --confirm-backup /data/mouse-annotation/backups/project2-taxonomy-<UTC>.db \
  --confirm-backup-sha256 <BACKUP_SHA256>

# 5. fresh read-only connection 复核后才启服
.venv/bin/python ../deploy/operations/update_project2_categories.py verify \
  --db /data/mouse-annotation/data/annotation.db --release-backend "$PWD"
```

`apply` 在 commit 前执行专项 verify，失败自动 rollback；commit 后再用 fresh read-only connection 重复 verify。任一步失败均保持停服。

## 整体回滚

保持服务和 worker 停止，确认数据库无打开者；保留失败库供排查，将步骤 2 已校验备份整体复制回 `/data/mouse-annotation/data/annotation.db`，再次执行 SQLite `quick_check` 与 `foreign_key_check`，确认恢复库 revision 后再启动旧 release。禁止用手写反向 SQL 回滚；Clip 媒体文件从未删除，无需恢复媒体目录。
