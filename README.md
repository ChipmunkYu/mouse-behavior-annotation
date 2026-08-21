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
- 视频库提供“我的任务 / 待领取 / 全部”，支持 member 待领取批量自领与 owner/admin 批量分配/改派；卡片仅“我的任务”进入标注。项目管理提供成员、邀请码和分工统计；迁移 head 为 `0012`。
- 分工分支最近一次后端全量历史证据为 `414 passed, 3 skipped`，其基线早于本次批量领取改动；本次独立精简验收已通过：`backend/tests/test_assignments.py` 为 `26 passed, 1 warning`（`37.66s`），frontend TypeScript/Vite production build 通过并处理 `59 modules`。本次集成未运行新的全量测试，人工浏览器矩阵尚未执行；分工变更仍仅在 `feature/assignment-module`，尚未进入 `main`、推送或部署。
- 已进入 `main` 的媒体修复以此前 `399 passed, 8 skipped` 为后端全量历史基线。2026-08-17 在 Python 3.11.9 隔离环境中，以 imageio-ffmpeg 0.6.0 内置的 FFmpeg `7.1-essentials_build-www.gyan.dev`（含 libx264）和 npm `@ffprobe-installer/win32-x64@5.1.0` 提供的 ffprobe 5.1 兼容包完成本地验证：真实集成测试 `5 passed, 1 warning`，媒体与项目导出聚焦回归 `66 passed, 1 warning`，均无 skip。已覆盖 25/30/60 FPS、H.264、yuv420p、300x200 crop、各 10 帧及约 `10/fps` 时长、JPEG、成功后无 `.part`/`.staging`、缩略图失败不发布和 MediaWorker `succeeded`/`ready` 状态；warning 为既有 Starlette/httpx deprecation warning。生产服务器 FFmpeg/ffprobe 4.4.2 兼容性仍未验证，媒体修复仍未部署，不能视为生产候选验收。
- 同日还以真实小鼠三文件（3.54 MB `社交-攻击1.mov`、约 1.69 MB `tracks.jsonl`、30 FPS/156 帧 `metadata.json`）仅通过公开 API 完成本地 E2E：导入 1877 条检测，以真实 `track_id=6` 标注帧 0–14 并提交、审核通过，异步媒体 `total=1/ready=1/failed=0`，项目导出成功。下载 ZIP 为 `backend/data/local-e2e/downloads/project-1-job-2.zip`（116603 bytes），片段目录严格只有 `clip.mp4`、`tracks.json`、`annotation.json`、`metadata.json` 且 JSON/计数一致；ffprobe 确认为 H.264、yuv420p、2044×1080、15 帧、0.5 秒，且无 `.part`/`.staging`。本地后端测试完成后已停止并释放 8000 端口；被忽略的 `backend/data/local-e2e` 配置与产物不属于仓库提交。
- 服务器本机与 SSH 隧道验收不等于完整公网验收；多地区 HTTPS 已成功，HTTP 及部分来源仍受备案同步影响，完整公网验收待完成。
- SQLite 与应用内媒体、导出、清理 worker 当前按单应用进程部署；不得直接使用多个应用进程共享同一任务库和数据目录。
