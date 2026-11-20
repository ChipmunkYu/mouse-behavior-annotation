# 2026-11-20｜测试复制：export cache 私有候选

## 单条真实历史资产冷热小验收（脚本交付，未远端执行）

重绑定 `_snapshot()` 保留全部表行数对比，仅对 submissions（排除获准四个身份字段）、background_jobs（完整旧 refs）、detection_snapshots 及小型标注/审核表按主键流式 SHA256（无主键 fixture 回退 rowid）。不将行集排序驻留或拼接；巨量 raw/state 等表只 count、不重复 hash 内容，不能据此声称全库内容一致。保护依据为 snapshot 摘要、仅四列受限 UPDATE 和原 trigger SQL 精确恢复；报告 `digest_scope` 明示 count-only 表及验证边界，缺失可选表兼容。

`check_export_candidate_real.py` 只访问固定候选 ROOT，要求 `PREPARED.ok`、已 committed 的 `rebind-export-report.json` 与私有无链接数据树；候选服务须未启动。读取 `candidate.env.json`，仅注入候选 `backend/`、`deps/`，不安装依赖、不 seed、不改用户或审核数据，不调用生产数据库。身份重绑定的权威实现见同目录 `rebind_export_candidate.py`；下方旧准备阶段 identity 限制描述不代表已重绑定后的状态。

脚本从 Project 2 manual 当前发布资产按源 size、标注区间长度选择一条，正规 enqueue 后仅缩小本次 queued job 的冻结集合，仍调用 `_frozen_rows` 校验；精确 `_run_job(new_id)`，不 drain/recover/prewarm、不重试 1973。生成 cold/warm 独立 ZIP 和一次实例 reserve 超过 free 的新故障 job，检查四成员内容、CRC、真实 ffprobe、range/crop/fps，以及 warm 三项计数为零。已有选中缓存则拒绝冒充冷测，不删除缓存。每测试600秒，失败保留现场，报告 `ROOT/real-export-check.json`（不覆盖已有报告）。这只是内部受控单条真实历史资产验证，不是全710或公共 API 全量验收。

本地零 I/O 检查：`python -B deploy/acceptance/check_export_candidate_real.py --selfcheck`。以下仅交付后台命令，不代表已执行，不启动服务：

```sh
ROOT=/data/mouse-annotation/candidates/export-cache-20261120-a1
nohup /opt/mouse-annotation/current/backend/.venv/bin/python -B "$ROOT/backend/deploy/acceptance/check_export_candidate_real.py" --execute > "$ROOT/real-export-check.log" 2>&1 < /dev/null &
```

上传时将脚本放到以上明确路径；若源码部署保留仓库根而非 backend 根，调整脚本位置但不改变脚本内固定 ROOT。解释器只读使用生产 venv，导入路径仍固定候选代码。

本轮已远端执行并完成隔离自动验收，状态和证据唯一见 [2026-11-20 隔离验收记录](../2026-11-20｜部署：导出缓存隔离验收.md)。下列准备命令仅用于追溯，不可对既有候选目录重跑。固定候选目录不可预先创建；需同步已核对 A2 脚本供 `--wal-helper` 使用。生产 venv Python 只读复用，额外测试依赖只安装到候选 `deps/`。

默认 `python -B prepare_export_cache.py --plan` 无文件 I/O。执行前须完成媒体 du 预算核对，三个 bytes 参数均为实际预算，不是猜测。空间要求 DB+媒体+最大单文件+ZIP预算+2GiB，并额外保留20GiB；复制循环守住22GiB余量。ZIP参数只是准备阶段预算，**不是应用运行时 ZIP 硬上限**。

```sh
# 以下仅交付命令，不代表已执行；先替换脚本/helper绝对路径及三个字节预算。
nohup /opt/mouse-annotation/current/backend/.venv/bin/python -B /path/prepare_export_cache.py --execute --wal-helper /path/prepare_b4b9566.py --media-bytes MEDIA_BYTES --max-single-copy-bytes MAX_FILE_BYTES --zip-max-bytes ZIP_BYTES > /data/mouse-annotation/candidates/export-cache-20261120-a1.prepare.log 2>&1 < /dev/null &
tail -n 30 /data/mouse-annotation/candidates/export-cache-20261120-a1.prepare.log
# 以下需 PREPARED.ok 且 backend/dist 已部署；不改 Nginx。
nohup /opt/mouse-annotation/current/backend/.venv/bin/python -B /data/mouse-annotation/candidates/export-cache-20261120-a1/boot.py > /data/mouse-annotation/candidates/export-cache-20261120-a1/backend.log 2>&1 < /dev/null &
nohup /opt/mouse-annotation/current/backend/.venv/bin/python -m http.server 15180 --bind 127.0.0.1 --directory /data/mouse-annotation/candidates/export-cache-20261120-a1/dist > /data/mouse-annotation/candidates/export-cache-20261120-a1/frontend.log 2>&1 < /dev/null &
nohup /opt/mouse-annotation/current/backend/.venv/bin/python -B /data/mouse-annotation/candidates/export-cache-20261120-a1/boot.py --tests tests/test_CHOSEN.py > /data/mouse-annotation/candidates/export-cache-20261120-a1/tests.log 2>&1 < /dev/null &
```

前端 bundle 必须预先构建 API_BASE 为 `http://127.0.0.1:18080/api`。准备脚本本身不改历史 identity；最终落盘后由 `rebind_export_candidate.py` 核对历史 SHA/size，仅在候选库事务性适配四项物理身份并精确恢复冻结 trigger，才进行历史资产新导出。生产库、旧任务 refs、逻辑标注和 source SHA 均不改。历史导出文件不复制，旧下载可能404；本轮新产物已实际下载验收。独立随机 demo_password 配置不等于已有密码；原账号密码不重置。失败保留现场，不覆盖重跑。
