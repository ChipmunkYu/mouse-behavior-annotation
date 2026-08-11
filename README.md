# 数据标注网站（标注网站/）

多小鼠社会行为在线标注网站，替代 BORIS 桌面单机方案，为行为识别阶段提供多人在线协作标注能力。当前 `feature/spatial-annotation` 已在生产扩展闭环上完成检测结果导入、叠加、参与对象标注、track 修正、三类修订审核与修正后 track 结果/项目 ZIP 导出；尚待真实视频端到端人工验收、生产部署及合并到 `main`。

> 对应工作项：WI-20260805-22 ｜ 需求与边界：`需求文档.md`
>
> 当前项目的权威术语与写作规则见 `项目术语表.md`；其他文档与其冲突时以术语表为准。

## 目录

```
标注网站/
├── backend/ # FastAPI + SQLite 后端（含 tests/、scripts/seed_demo.py）
├── frontend/ # React + TypeScript + Vite 前端
├── 参考文档/ # AUTO_PIPELINE.md、VIDEO_ANNOTATION_TOOL.md
├── 需求文档.md # 需求文档（v0.6）
├── 项目术语表.md # 当前项目权威术语基线
├── 服务器规格估算.md # 用户负载、存储、带宽与费用的参数化估算
├── README.md # 本文件
└── boris-9.13.0-win64-setup.exe # BORIS 桌面版安装包（参考用）
```

> `backend/data/`（数据库、演示视频、导出片段）为 gitignored 运行时数据，不作为资产登记。

## 快速开始（Windows，首次安装）

终端 1（后端）：

```bat
cd /d D:\lab\行为识别\标注网站\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python scripts\migrate.py
```

终端 2（前端）：

```bat
cd /d D:\lab\行为识别\标注网站\frontend
npm install
copy .env.example .env
```

## 最短启动步骤（两个终端）

终端 1（后端，默认 http://127.0.0.1:8000）：

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\activate
uvicorn app.main:app --reload --port 8000
```

终端 2（前端，默认 http://localhost:5173）：

```bat
cd /d D:\lab\行为识别\标注网站\frontend
npm run dev
```

应用在单进程启动时会自动执行幂等数据库迁移。部署或 CI 推荐先从 `backend` 目录显式迁移；多 worker 启动前必须先执行该命令：

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\activate
.venv\Scripts\python scripts\migrate.py
```

## ffmpeg 配置

精确片段、缩略图和缺失片段补生成依赖 ffmpeg/ffprobe。可将其加入 `PATH`，或在 `backend/.env` 中配置可执行文件的绝对路径：

```dotenv
FFMPEG_PATH=C:\path\to\ffmpeg.exe
FFPROBE_PATH=C:\path\to\ffprobe.exe
```

当前开发机未安装 ffmpeg；自动化测试使用 Fake processor，因此只验证任务编排、命令契约和失败处理，不代表真实媒体编码已经验收。

## 演示数据

从 `backend` 目录运行；不指定 `--video-source` 时仅创建 Mock 视频元数据，指定后以硬链接优先放入 `backend/data/videos/demo_attack.mov`：

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\python scripts\seed_demo.py
.venv\Scripts\python scripts\seed_demo.py --video-source "D:\lab\行为识别\data\北医标注-行为例子\社交行为\5.攻击行为\社交-攻击1.mov"
```

## 访问地址与 Demo 账号

- 前端：http://localhost:5173
- 后端 API：http://127.0.0.1:8000/api
- Swagger 接口文档：http://127.0.0.1:8000/docs

| 用户名 | 密码 | 说明 |
|---|---|---|
| `demo` | `demo123` | 仅开发使用；部署前须通过环境变量 `DEMO_USERNAME` / `DEMO_PASSWORD` 覆盖 |

## 测试

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\activate
pytest -q :: 当前 298 passed, 3 skipped, 1 warning

cd /d D:\lab\行为识别\标注网站\frontend
npm run build :: 生产构建验证
```

当前验证结果：后端全量 `298 passed, 3 skipped, 1 warning`；前端 `npm run build` 通过。

## 生命周期清理

清理原视频以外的过期导出 ZIP、已知临时文件、清理异常和过期任务记录前，先从 `backend` 目录执行 dry-run：

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\activate
.venv\Scripts\python scripts\cleanup_retention.py --dry-run
```

确认报告后，去掉 `--dry-run` 才会执行实际清理。脚本会先确保数据库 schema，因此 dry-run 的零副作用范围不包含数据库迁移或建表。

## 当前能力与边界

**已实现**：登录与项目角色、12 类初始化、行为标注、审核、视频片段与缩略图、片段库、分类 ZIP 和生命周期清理；原始视频/`tracks.jsonl`/`metadata.json` 三文件导入批次与替换、框/track ID/关键点/骨架叠加、参与对象 `mouse_ids`、Split、Merge、检测抑制与撤销、三类修订审核、修正后 track 结果 `tracks.corrected.jsonl`，以及 `clip_file` 集中 ZIP 索引均已实现。单视频事件 API 返回完整事件字段（`clip_file` 可为 `null`），ZIP 中 `clip_file` 必须是安全非空路径；修正后 track 结果保留全部帧，空帧写为 `detection_count=0`、`detections=[]`，可重新导入。原始检测始终不可变。

**本地 / 生产边界**：当前仍是未部署、未合并分支，不声称完成真实 ffmpeg 编码、真实长视频浏览器流程或生产部署验收。正式输入应使用原始 `社交-攻击1.mov`；已上传的 `社交-攻击1_all_ids.mp4` 是烧录调试视频。生产使用前还须完成完整人工流程并配置强密钥、账号、CORS、持久化 `DATA_DIR`、数据库备份和 ffmpeg/ffprobe。

**进程边界**：媒体、导出与周期清理共用当前应用内的单进程 executor/worker 设计，不得直接让多个应用进程共享同一任务库和数据目录。多进程部署前除显式执行数据库迁移外，还必须为任务领取增加持久化 lease/owner、heartbeat 与过期回收机制。

空间标注生产能力已整合到 `main`；后续界面与数据库优化分别在 `feature/spatial-ui-optimization`、`feature/spatial-db-optimization` 开展。旧演示与阶段性功能分支不再维护，确认无保留价值后删除。

## 部署带宽优化点（规划，未实施）

以下为生产部署前计划实施的带宽优化方向，均未实施，供后续排期参考：

1. 视频播放改为 HTTP Range 流式（现状为前端整包下载 blob，单次打开即下载整个文件）。
2. 播放用低码率副本：存储母版，标注/审核走转码后的低码率版本（如 H.264 CRF 28–30，或 H.265，注意 Firefox 桌面不支持 HEVC 需回退）。
3. 缓存：视频与缩略图增加浏览器缓存头（Cache-Control/ETag）；反向代理层加 proxy_cache 或 CDN，多人重复观看同一视频时命中缓存。
4. 前端加载策略：视频 `preload="metadata"`、缩略图懒加载并压缩为 WebP、修复前端 `/thumbnails/` 无对应后端路由的问题。
5. 播放版本按需异步生成（复用现有 MediaWorker 队列），避免预转全部视频。

## 详见

- 后端启动、配置、API 一览与测试：`backend/README.md`
- 前端功能与技术要点：`frontend/README.md`
- 需求、数据模型与 P1/P2 边界：`需求文档.md`
- 权威术语、状态与修订定义：`项目术语表.md`
- 用户负载到服务器规格的参数化估算：`服务器规格估算.md`
