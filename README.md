# 数据标注网站

多小鼠社会行为在线标注网站，提供视频导入、行为标注、track 修正、提交审核、视频片段生成与项目导出。

## 文档入口

- [需求文档](需求文档.md)：当前产品范围与正式契约。
- [项目术语表](项目术语表.md)：全站 canonical wording 与状态定义。
- [全站文档地图](docs/README.md)：现行设计、计划、历史和运维文档的分类与权威优先级。
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
- 后端最终证据为 `397 passed, 3 skipped`；真实 FFmpeg 的 25/30/60 FPS 验收均已通过。
- 服务器本机与 SSH 隧道验收不等于完整公网验收；多地区 HTTPS 已成功，HTTP 及部分来源仍受备案同步影响，完整公网验收待完成。
- SQLite 与应用内媒体、导出、清理 worker 当前按单应用进程部署；不得直接使用多个应用进程共享同一任务库和数据目录。
