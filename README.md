# 数据标注网站

多小鼠社会行为在线标注网站，提供项目成员与视频分工、视频导入、行为标注、track 修正、提交审核、视频片段生成与项目导出。

## 文档入口

- [需求文档](需求文档.md)：当前产品范围与正式契约。
- [项目术语表](项目术语表.md)：全站 canonical wording 与状态定义。
- [全站文档地图](docs/README.md)：现行设计、计划、历史和运维文档的分类与权威优先级。
- [分工标注模块设计](docs/设计/分工标注模块设计.md)：角色、成员、负责人、接口、迁移与并发规则。
- [后端说明](backend/README.md) / [前端说明](frontend/README.md)：实现、接口与开发命令。
- [生产部署](deploy/README.md)：部署模板与运维入口。

## 快速开始

后端（Windows）：

```bat
cd /d D:\lab\行为识别\标注网站\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python scripts\migrate.py
uvicorn app.main:app --reload --port 8000
```

前端：

```bat
cd /d D:\lab\行为识别\标注网站\frontend
npm install
copy .env.example .env
npm run dev
```

默认入口：前端 `http://localhost:5173`，后端 API `http://127.0.0.1:8000/api`，Swagger `http://127.0.0.1:8000/docs`。Demo 账号 `demo/demo123` 仅供开发使用。

## 当前边界

- 正式项目 ZIP 中，每个 `SubmissionAnnotation` 对应一个独立目录，固定包含 `clip.mp4`、`annotation.json`、`tracks.json`、`metadata.json`；不包含集中式 `annotations.json`、manifest 或 `corrected_tracks/`。
- 单视频 `/annotations/export` 仅是 legacy 兼容 JSON 接口，不代表正式项目 ZIP 契约。
- 项目角色为 `owner/admin/member`；审核能力通过 `can_review` 与角色共同决定。分工只表示责任归属，active 成员仍可编辑和提交。
- 视频库提供“我的任务 / 待领取 / 全部”，支持 member 待领取批量自领与 owner/admin 批量分配/改派；卡片仅“我的任务”进入标注。项目管理提供成员、邀请码和分工统计。当前仓库候选 migration head 为 `0016`（新增低码率展示代理字段与任务租约）；“网站服务器文件清单”当前确认的生产 schema 为 `0015`。这是候选代码与已确认生产事实的区别，不表示 `0016` 已部署；生产 release、schema、服务和备份等实时事实仍只以服务器清单及现场核验为准。
- 生产 release、schema、服务和备份的实时状态仅以仓库外“网站服务器文件清单”为准，本文不复制易过期的提交号或运行状态。
- P4 低码率展示代理最终服务器候选 `ba99fc9cae6acb90e94e7d1bdbe0428f5839f4d2` 已推送，并在网站服务器完成固定 release 构建和隔离短样本验收；尚未切换生产 `current`、迁移生产数据库或启用生产，不能表述为已部署或已上线。新 file-backed Video 在唯一开关开启时计算 hash 并入队；四入口统一认证完整 GET、`response.blob()` 与 object URL；pending/failed 严格返回 409；片段、Submission 和导出始终读取原片。
- 服务器 FFmpeg/ffprobe 4.4.2 已通过 3 秒真实 H.264 原片到 1280×720 H.264 代理的基本兼容与短闭环；秋采长视频帧/PTS/叠加、真实 ENOSPC、并发资源、目标浏览器大 Blob，以及生产维护窗口的备份、旧数据处理、迁移和回滚演练仍是上线门禁。完整证据见[实现计划](docs/计划/低码率展示代理视频实现计划.md)，服务器实时状态与文件路径仍只查“网站服务器文件清单”。
- 不支持历史数据回填或独立原片 fallback。开关关闭时播放原片且不 hash/入队，因此关闭期间禁止接收新视频写入；启用前必须在维护窗口清空/重置旧 file-backed 数据并以只读 preflight 验证通过。metadata-only Video 不在代理范围内。
- 同日还以真实小鼠三文件（3.54 MB `社交-攻击1.mov`、约 1.69 MB `tracks.jsonl`、30 FPS/156 帧 `metadata.json`）仅通过公开 API 完成本地 E2E：导入 1877 条检测，以真实 `track_id=6` 标注帧 0–14 并提交、审核通过，异步媒体 `total=1/ready=1/failed=0`，项目导出成功。下载 ZIP 为 `backend/data/local-e2e/downloads/project-1-job-2.zip`（116603 bytes），片段目录严格只有 `clip.mp4`、`tracks.json`、`annotation.json`、`metadata.json` 且 JSON/计数一致；ffprobe 确认为 H.264、yuv420p、2044×1080、15 帧、0.5 秒，且无 `.part`/`.staging`。本地后端测试完成后已停止并释放 8000 端口；被忽略的 `backend/data/local-e2e` 配置与产物不属于仓库提交。
- 服务器本机与 SSH 隧道验收不等于完整公网验收；多地区 HTTPS 已成功，HTTP 及部分来源仍受备案同步影响，完整公网验收待完成。
- SQLite 与应用内媒体、导出、清理 worker 当前按单应用进程部署；不得直接使用多个应用进程共享同一任务库和数据目录。
