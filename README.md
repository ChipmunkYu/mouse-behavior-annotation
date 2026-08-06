# 数据标注网站（标注网站/）

多小鼠社会行为在线标注网站，替代 BORIS 桌面单机方案，为行为识别阶段提供多人在线协作标注能力。当前为 **P1 本地原型**：自动化测试与真实 HTTP smoke 均已通过，处于浏览器人工验收前的状态。

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

## 前端演示分支快速启动

当前 `demo/frontend-showcase` 分支用于录制功能演示。无需启动后端：

```bat
cd /d D:\lab\行为识别\标注网站\frontend
npm install
npm run demo
```

浏览器访问 http://localhost:5173，使用 `demo / demo123` 登录。演示模式包含登录、项目、视频上传、标注、审核、跨视频片段库、分类导出和项目管理页面；所有媒体生成、导出和管理操作均为前端模拟，不会执行真实 ffmpeg 或写入后端。

## 快速开始（Windows，首次安装）

终端 1（后端）：

```bat
cd /d D:\lab\行为识别\标注网站\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
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
pytest -q            :: 当前批次 2 基线 89 passed

cd /d D:\lab\行为识别\标注网站\frontend
npm run build        :: 生产构建验证
```

## P1 已实现 / 未实现边界

**真实后端已实现**：登录；项目入口 / 项目内身份；创建项目时初始化北医 12 类；真实视频流式上传；Mock 视频元数据；视频流（Bearer 认证）；区间标注 CRUD；时间轴 / 快捷键；统一事件 JSON 导出。

**仅前端演示、后端尚未完成**：提交审核与审核队列、实体片段与缩略图、跨视频片段库、分类导出、任务分配、类别管理。演示模式会模拟这些流程并明确标注，不代表真实媒体产物已经生成。

**仍未实现**：ffmpeg 精确重编码、YOLO / 画框、生产部署。浏览器中的 MOV 编码兼容性仍需按实际编码验证。

## 详见

- 后端启动、配置、API 一览与测试：`backend/README.md`
- 前端功能与技术要点：`frontend/README.md`
- 需求、数据模型与 P1/P2 边界：`需求文档.md`
