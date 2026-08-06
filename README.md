# 数据标注网站（标注网站/）

多小鼠社会行为在线标注网站，替代 BORIS 桌面单机方案，为行为识别阶段提供多人在线协作标注能力。当前 `feature/backend-expansion` 为**生产扩展分支**：已形成从真实视频上传、标注提交与审核、精确片段生成、跨视频片段库到分类导出和生命周期清理的后端闭环；尚未部署，也未完成真实 ffmpeg 媒体和浏览器人工验收。

> 对应工作项：WI-20260805-22 ｜ 需求与边界：`需求文档.md`

## 目录

```
标注网站/
├── backend/          # FastAPI + SQLite 后端（含 tests/、scripts/seed_demo.py）
├── frontend/         # React + TypeScript + Vite 前端
├── 参考文档/          # AUTO_PIPELINE.md、VIDEO_ANNOTATION_TOOL.md
├── 需求文档.md        # 需求文档（v0.5）
├── README.md         # 本文件
└── boris-9.13.0-win64-setup.exe   # BORIS 桌面版安装包（参考用）
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
pytest -q            :: 当前 190 passed, 3 skipped

cd /d D:\lab\行为识别\标注网站\frontend
npm run build        :: 生产构建验证
```

当前验证结果：后端 `190 passed, 3 skipped`；3 项跳过均因 Windows 当前账户无 symlink 创建权限，相关静态安全 Gate 已复核通过。前端 `npm run build` 通过。

## 生命周期清理

清理原视频以外的过期导出 ZIP、已知临时文件、清理异常和过期任务记录前，先从 `backend` 目录执行 dry-run：

```bat
cd /d D:\lab\行为识别\标注网站\backend
.venv\Scripts\activate
.venv\Scripts\python scripts\cleanup_retention.py --dry-run
```

确认报告后，去掉 `--dry-run` 才会执行实际清理。脚本会先确保数据库 schema，因此 dry-run 的零副作用范围不包含数据库迁移或建表。

## 当前能力与边界

**已实现**：登录与项目角色；北医 12 类初始化；真实视频分块流式上传；视频流与区间标注；提交、审核队列、裁决和修订失效；仅审核通过视频的 H.264 MP4 精确片段与 JPG 缩略图任务；跨视频片段库及筛选统计；按类别导出 ZIP 与 `annotations.json`；过期导出、临时文件、任务日志和清理异常的保留清理。P1 Mock 视频元数据接口和统一事件 JSON 导出仍保留。

**本地 / 生产边界**：当前仍是未部署分支，不声称完成真实 ffmpeg 编码、浏览器人工流程或生产部署验收。生产使用前须配置强密钥、账号、CORS、持久化 `DATA_DIR`、数据库备份和 ffmpeg/ffprobe，并以真实媒体走完整上传、审核、生成、导出和清理验收。类别管理界面及 YOLO / 画框不在本分支范围。

**进程边界**：媒体、导出与周期清理共用当前应用内的单进程 executor/worker 设计，不得直接让多个应用进程共享同一任务库和数据目录。多进程部署前除显式执行数据库迁移外，还必须为任务领取增加持久化 lease/owner、heartbeat 与过期回收机制。

`demo/frontend-showcase` 是独立录屏分支，不再维护。空间标注在 `feature/spatial-annotation`（基线 `1739d0a`）另行继续，已有 YOLO 样例不属于本次后端扩展交付。

## 详见

- 后端启动、配置、API 一览与测试：`backend/README.md`
- 前端功能与技术要点：`frontend/README.md`
- 需求、数据模型与 P1/P2 边界：`需求文档.md`
