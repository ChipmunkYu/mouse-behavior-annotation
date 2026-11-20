# 导出缓存隔离验收证据

来源：2026-11-20 从标注服务器 `/data/mouse-annotation/candidates/export-cache-20261120-a1/` 下载的三个自动验收 JSON；关联 WI-20261117-01。

- `LINUX_ACCEPTED.json`：262 项首轮通过与修正旧媒体夹具后的 7 项通过，按测试名称核对覆盖，总计 269 项、无跳过；不是一次全项目全量测试结果。
- `real-export-check.json`：真实历史资产受控单条冷热导出、成员 SHA、ffprobe 和空间不足故障注入。
- `http-export-check.json`：Project 2 / manual / Dispersal 三条的真实公开 API 发起、轮询、下载和重复导出。

详细范围、实例位置及限制见 [隔离验收记录](../../../2026-11-20｜部署：导出缓存隔离验收.md)。不下载或提交 candidate.env、短期令牌、原始失败 traceback 或完整数据库；保留证据追溯期间不作缓存删除。
