# 标注网站后端（P1 + 批次 2 视频上传 + 批次 3 提交与审核闭环 + 批次 4 精确片段与缩略图 + 批次 5 生产跨视频片段库）

模块化单体的最小后端，服务于多人在线行为标注网站（对应 `../需求文档.md`）。

- 技术栈：Python 3.11、FastAPI、SQLite、SQLAlchemy 2.x、Pydantic v2
- 范围：真实数据模型 + CRUD；Mock/seed 仅用于账号、项目、视频元数据
- 批次 2：真实视频流式上传（分块写入 + 磁盘余量保护 + 原子 rename），保留 P1 的 JSON Mock 视频元数据接口
- 批次 3：提交与审核闭环（submit / review queue / review 裁决 / 审核历史），
  标注写入与审核工作流联动（非 draft 修改回 draft + Clip 行与实体文件清理）
- 批次 4：仅审核通过（approved）的视频，后台精确重编码每条标注为 H.264 MP4 片段并生成
  JPG 缩略图——单进程单任务执行、可恢复/重试、修订隔离；媒体执行器可替换（测试无需本机 ffmpeg）
- 批次 5：生产跨视频片段库——跨视频聚合审核通过标注与对应 ready Clip 的分页只读接口，
  含类别统计与类别/视频/标注者/关键词筛选（无 Alembic 迁移，复用现有表）
- 全部 API 位于 `/api` 前缀下，认证使用 JWT Bearer 令牌

## 目录结构

```
backend/
├── alembic.ini            # Alembic 配置（迁移目录为 migrations/）
├── migrations/            # Alembic 迁移脚本（版本号只存在于 scripts/versions 内）
│   ├── env.py             # 迁移环境（SQLite 批处理模式，复用 app 模型元数据）
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_baseline_p1.py       # baseline：P1 原版 6 张表
│       ├── 0002_review_clip_job.py   # 增量：Video 工作流字段 + Review/Clip/BackgroundJob
│       ├── 0003_fk_ondelete_explicit.py # 增量：users 外键 ON DELETE 策略显式化（SET NULL / RESTRICT）
│       └── 0004_background_job_dedupe_attempts.py # 增量：BackgroundJob 幂等去重键 + 重试计数
├── app/
│   ├── main.py            # 应用工厂（启动时自动幂等迁移、CORS、路由注册、媒体 worker 生命周期）
│   ├── config.py          # 环境变量配置
│   ├── database.py        # SQLAlchemy 引擎 / Session / ensure_schema
│   ├── migration.py       # 程序化 Alembic 入口（启动自动迁移 / CLI 共用）
│   ├── models.py          # User / Project / ProjectMembership / BehaviorCategory / Video / Annotation / Review / Clip / BackgroundJob
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── auth.py            # 密码哈希（PBKDF2）+ JWT
│   ├── seed.py            # 北医 12 类初始化 + demo 账号
│   ├── deps.py            # 项目成员权限依赖
│   ├── media.py           # 媒体执行器：ffmpeg/ffprobe 子进程封装（无 shell）+ 命令构造
│   ├── media_jobs.py      # 媒体任务编排：单 worker 领取 / 逐片重编码 / 重启恢复 / 修订隔离
│   └── routers/           # health / auth / projects / categories / videos / annotations / reviews / clips / media
├── scripts/               # 本地工具脚本（仅开发，不注册到应用）
│   ├── migrate.py         # 数据库迁移 CLI（全新库 / P1 旧库升级 / 幂等）
│   └── seed_demo.py       # 幂等演示数据脚本（第一阶段本地演示）
├── tests/                 # pytest 聚焦测试
├── data/                  # 运行时数据（数据库/视频/导出，已 gitignore）
├── requirements.txt
├── .env.example
└── README.md
```

## 快速开始

```bash
cd backend

# 1. 创建隔离环境
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制并（按需）修改配置
copy .env.example .env

# 4. 启动（默认 http://127.0.0.1:8000）
uvicorn app.main:app --reload --port 8000
```

健康检查：`GET /api/health`。接口文档（Swagger）：`http://127.0.0.1:8000/docs`。

## 数据库迁移（Alembic）

数据模型由 Alembic 版本脚本管理（`migrations/versions/`），**不在业务代码中硬编码迁移版本**。

**启动策略**：`create_app` 在建库前自动执行幂等迁移——全新空库直接建立完整 schema；
已存在的 P1 未版本化数据库（有 `users` 等表、无有效版本行）会先安全标记
baseline（0001）再升级到 head（0004），**不删除任何已有数据**；重复启动无副作用。
因此 README 的最短启动方式对全新库与 P1 旧库同样有效。

> **自动迁移的进程边界**：`create_app` 内的自动迁移只适合**单进程启动**
> （如开发时 `uvicorn app.main:app`）。**多 worker 部署**
> （`uvicorn --workers N` / gunicorn 多 worker 等）必须**先手动运行一次
> `scripts/migrate.py` 完成迁移**，再启动多 worker，否则多个 worker 同时
> 尝试迁移同一 SQLite 库会互相干扰（`Database is locked` 或重复 DDL）。
> 部署/CI 统一约定：迁移由显式命令完成，应用启动不再承担迁移职责。

**空版本表**：迁移状态以 `alembic_version` 表的**版本行**为准，而非仅看表是否存在。
若该表存在但没有任何版本行（例如先前 `alembic check` 留下的副作用），
按“无有效版本行”处理——含 P1 核心表视为未版本化 P1 旧库（stamp baseline 后升级），
不含 P1 核心表视为全新空库，均不会误判为已版本化。
非预期表 / 未知版本 / 版本表损坏会被安全拒绝（抛错），**不会盲 stamp**。

需要显式迁移时（部署/CI 推荐），从 `backend` 目录运行：

```bash
# 使用配置的 DATABASE_URL（默认 data/annotation.db）
.venv\Scripts\python scripts\migrate.py

# 指定其它数据库
.venv\Scripts\python scripts\migrate.py --db-url sqlite:///./data/annotation.db

# 仅检查当前状态，不做任何修改（可区分“空版本表”并显示当前版本号）
.venv\Scripts\python scripts\migrate.py --check
```

- 全新空库 → `upgrade head`（0001 建 P1 全表，0002 增量，0003 外键策略显式化，0004 任务去重键）。
- P1 旧库（未版本化，含空版本表缺陷形态）→ 自动 `stamp 0001` 标记 baseline 后 `upgrade head`，旧数据原样保留。
- 0003 已版本化库 → 增量 `upgrade head` 到 0004（新增列 + 唯一索引，数据原样保留）。
- 已版本化 → 幂等 `upgrade head`。
- 非预期表 / 未知版本 / 版本表损坏 → `--check` 与迁移均报错退出（退出码 2），不执行任何修改。

### 后台任务去重与重试（0004）

0004 为 `background_jobs` 增加两个字段（SQLite 批处理重建表，已有数据保留）：

| 列 | 可空 | 说明 |
|---|---|---|
| `dedupe_key` | 是 | 幂等去重键：同一视频+修订只允许一行任务（唯一索引兜底并发重复入队，防重复任务；NULL 允许多个，兼容全局清理等任务） |
| `attempts` | 否（默认 0） | 任务领取/中断重排次数，用于重启恢复的重试上限判定 |

### 外键删除策略（0003，Oracle Gate 1 整改）

0003 把 P1 阶段未显式声明的 users 外键策略显式化，运行期由应用的
`PRAGMA foreign_keys=ON` 强制生效：

| 表.列 | 可空 | 引用 | ON DELETE |
|---|---|---|---|
| `videos.uploaded_by` | 是 | `users.id` | `SET NULL`（删除上传者后视频保留、上传者置空） |
| `annotations.reviewer_id` | 是 | `users.id` | `SET NULL`（删除审核人后标注保留、审核人置空） |
| `projects.created_by` | 否 | `users.id` | `RESTRICT`（被项目引用时禁止删除创建者） |
| `annotations.annotator_id` | 否 | `users.id` | `RESTRICT`（被标注引用时禁止删除标注者） |

### 后续整改批次（未实施）

- **批次 6**：分类导出；**批次 7**：基于 `cleanup-issues.log` 的孤儿文件补偿清理任务、
  导出 ZIP 过期清理等生命周期任务。

批次 4（媒体任务去重、精确片段/缩略图后台生成）与批次 5（生产跨视频片段库）已实现，
见下文对应章节。

## Demo 账号

| 用户名 | 密码 | 说明 |
|---|---|---|
| `demo` | `demo123` | **仅开发使用**；部署前必须通过环境变量 `DEMO_USERNAME` / `DEMO_PASSWORD` 覆盖，或自行创建用户 |

## 演示数据

`scripts/seed_demo.py` 为第一阶段本地演示提供幂等的演示数据（项目 / 视频 / 标注 / demo 账号），
只复用现有配置、模型与 seed 逻辑，不修改任何接口。从 `backend` 目录运行：

```bash
.venv\Scripts\python scripts\seed_demo.py                      # 仅 Mock 元数据
.venv\Scripts\python scripts\seed_demo.py --video-source C:/path/to/demo.mov
.venv\Scripts\python scripts\seed_demo.py --duration 5 --fps 30
```

- 幂等创建/复用项目 `北医行为标注演示`，demo 用户为 `owner`；新建项目时初始化同样的项目级 12 类。
- 可选 `--video-source`：校验源文件存在后，以硬链接优先、复制回退的方式放入 `DATA_DIR/videos/demo_attack.mov`，
  数据库 `storage_path` 存相对名；不提供则仅创建 Mock 元数据。
- 创建/复用视频 `demo_attack.mov`（默认 `duration=10.0`、`fps=25`、`status=ready`，可用 `--duration` / `--fps` 覆盖），
  以及一条 `1.0-3.0s / 25-75 帧` 的“攻击行为”标注。
- 可重复运行不产生重复数据；输出项目/视频/标注 id 与 demo 登录信息，不输出密码哈希。

## 配置

数据库、上传视频、导出片段、clip/thumbnail 与清理异常日志均从环境变量配置，
默认位于 `backend/data/` 下（已被 gitignore）：
- `DATA_DIR/videos/` 上传视频；`DATA_DIR/exports/` 导出；
  `DATA_DIR/clips/` 与 `DATA_DIR/thumbnails/` 片段产物（批次 4 生成）；
  `DATA_DIR/cleanup-issues.log` 清理异常 JSONL（越界路径 / 删除失败）。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 数据根目录（含数据库） |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/annotation.db` | 数据库连接串 |
| `SECRET_KEY` | 开发用弱密钥 | JWT 密钥，生产必须覆盖 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | 令牌有效期（分钟） |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `demo` / `demo123` | 种子开发账号 |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 允许的前端来源（本地 Vite） |
| `UPLOAD_DISK_RESERVE_BYTES` | `1073741824`（1 GiB） | 上传写入前/每块前检查磁盘可用空间，需保留的安全余量，不足返回 507 |
| `UPLOAD_CHUNK_SIZE` | `1048576`（1 MiB） | 上传分块流式写入的块大小（字节），应用层不设文件大小上限 |
| `FFMPEG_PATH` / `FFPROBE_PATH` | `ffmpeg` / `ffprobe` | 媒体可执行文件（默认取 PATH 命令名；本机无 ffmpeg 时可注入替换执行器） |
| `MEDIA_CRF` | `23` | libx264 CRF（越小质量越高、体积越大） |
| `MEDIA_PRESET` | `veryfast` | libx264 预设（速度/体积权衡） |
| `MEDIA_TIMEOUT_SECONDS` | `600` | 单条媒体命令超时（秒），超时清理半成品并判失败 |
| `MEDIA_MAP_AUDIO` | `false` | 片段是否映射音频（`-map 0:a:0?` + aac；默认仅视频） |
| `MEDIA_MAX_ATTEMPTS` | `3` | 重启恢复时 running 任务被重排/判失败的 attempts 阈值 |
| `MEDIA_SYNCHRONOUS` | `false` | 测试用：媒体 worker 在请求线程内同步执行（配合可替换执行器） |

## API 一览

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/auth/login` | JSON `{username, password}` → `{access_token, user}` |

### 项目

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects` | 当前用户的成员项目及项目内角色 `role` |
| `POST` | `/api/projects` | 创建项目；创建者自动获得 `owner` 并初始化 12 个类别 |

### 类别（仅项目成员可访问，返回启用类别）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/categories` | 按 `sort_order` 返回启用的类别 |

### 视频

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/videos` | 项目视频列表 |
| `POST` | `/api/projects/{project_id}/videos` | JSON 创建视频元数据（P1 Mock 上传，保留） |
| `POST` | `/api/projects/{project_id}/videos/upload` | 真实视频流式上传（multipart 字段 `file`）→ 201 |
| `GET` | `/api/videos/{video_id}/stream` | 若 `storage_path` 解析到配置视频目录内且文件存在则 `FileResponse`，否则 404 |

### 真实视频上传（批次 2）

- 仅 **active 项目成员** 可上传（非成员 403、项目不存在 404、未登录 401）。
- multipart 字段 `file`；**应用层不设文件大小上限**，`UploadFile` 按 `UPLOAD_CHUNK_SIZE`
  分块流式写入临时文件，绝不一次性 `read()` 全部内容。
- 扩展名（**大小写不敏感**）是唯一校验依据，`Content-Type` 仅辅助、不参与校验。
  允许：`mp4, mov, avi, mkv, webm, m4v, wmv, mpeg, mpg`；拒绝空文件、无扩展名/不允许的扩展名。
- 媒体 `status` 映射（本批不运行 ffprobe/ffmpeg）：浏览器通常可直接播放的
  `mp4/webm/mov/m4v` → `uploaded`；`avi/mkv/wmv/mpeg/mpg` → `needs_transcode`。
- 磁盘目标使用 **UUID 不可碰撞名**，`storage_path` 存相对路径（限制在 `DATA_DIR/videos` 内）；
  临时 `.part` 文件写入成功后 **原子 rename**；失败/取消/DB 提交失败均清理临时与最终孤儿文件。
- 每次写入前/每块写入前用 `shutil.disk_usage` 检查 `videos_dir` 可用空间，
  写入后须保留 `UPLOAD_DISK_RESERVE_BYTES` 安全余量，不足返回 **507**。
- 响应为现有 `VideoOut`（201），`uploaded_by` = 当前用户、`workflow_status=draft`、`annotation_revision=1`。
- 错误文案稳定（见 `app/routers/videos.py` 顶部常量）。

### 提交与审核（批次 3）

审核工作流状态机：`draft → submitted → approved / rejected`（`rejected` 可重新提交）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/projects/{project_id}/videos/{video_id}/submit` | 提交视频审核 → `VideoOut` |
| `GET` | `/api/projects/{project_id}/reviews/queue` | 审核队列 → `VideoOut[]`（仅 `submitted`） |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/reviews` | 审核历史 → `ReviewOut[]`（含所有修订） |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/review` | 审核裁决 → `ReviewOut` |

- **submit**：仅 `owner/admin/annotator`；至少 1 条标注；仅 `draft/rejected` 可提交
  （`submitted/approved` 拒绝 400）；提交后视频 `workflow_status=submitted`、`submitted_at` 更新，
  本修订全部标注 `review_status=pending`、`reviewer_id=null`。
- **queue**：仅 `owner/admin/reviewer`；只返回 `submitted` 视频（跨项目隔离，按 `submitted_at` 倒序）。
- **reviews（历史）**：项目成员均可读该视频完整审核历史，跨修订累积，不因失效删除。
- **review**：仅 `owner/admin/reviewer`；仅 `submitted` 可裁决（其余状态 400，重复裁决 400）；
  追加一条 `Review`（`annotation_revision` = 裁决时视频修订号）并同步：
  `approved` → 视频 `approved/approved_at/approved_by`、标注 `approved/reviewer_id`；
  `rejected` → 视频 `rejected`、清空 approved 字段、标注 `rejected/reviewer_id`。

**标注写入与工作流联动**：标注新增/删除/修改（PATCH 实际字段）在视频处于
`submitted/approved/rejected` 时，先在同一事务内把视频失效回 `draft`：
`annotation_revision +1`、清空 `submitted_at/approved_at/approved_by`、删除该视频所有 Clip
记录与实体 clip/thumbnail 文件（`clips_dir` / `thumbnails_dir` 内）；Review 历史与 Annotation
保留。已处于 `draft` 时连续修改不再递增修订号。

- 标注写入仅 `owner/admin/annotator`；reviewer 不可写（403）。
- 创建标注固定 `review_status=pending`；直接写 `review_status`（创建非 `pending` 值 / 任意 PATCH）
  一律 422，审核状态只能走审核 API。

**实体文件清理失败策略（单机原型）**：
- 顺序：DB 事务先行提交（Clip 行删除 + 视频回 draft），再删除实体文件——保证数据库
  永不引用已删除文件；若先删文件后提交 DB，事务失败会留下指向已删文件的悬空 Clip 行。
- 越界路径（绝对路径逃逸 / `../` 穿越 / 等于根目录）**绝不删除**，写入异常日志。
- 删除失败与越界均追加一条 JSONL 到 `DATA_DIR/cleanup-issues.log`（`kind ∈ {delete-failed,
  out-of-bounds}`）并记入应用日志，**不阻断业务请求、绝不无声**；批次 4 清理任务据此补偿。

### 批次 4：精确片段与缩略图

仅审核通过（`approved`）的视频才会生成片段。approve 提交成功后自动创建
当前 video+revision 的媒体任务（`job_type=media`，`dedupe_key=media:video:{id}:rev:{revision}`）
与每条标注的 `pending` Clip 行并调度；`rejected` 不入队。已存在
`queued/running/succeeded` 任务则幂等复用，`failed/cancelled` 重置回 `queued` 重试。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/videos/{video_id}/media-status` | 媒体生成状态 → `MediaStatusOut`（项目成员可读） |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/media/generate` | 触发/重试生成 → `JobOut`（仅 owner/admin/reviewer、仅 approved） |
| `GET` | `/api/projects/{project_id}/jobs/{job_id}` | 任务详情 → `JobOut`（项目成员可读） |

**精确重编码**：每条标注按 `[start_time, end_time)` 生成 `libx264 + yuv420p + faststart`
H.264 MP4（`-ss` 前置输入定位 + 重编码，帧精确）；可选音频映射（`MEDIA_MAP_AUDIO`）。
缩略图从片段**中点**（`(start+end)/2`）抽一帧 JPEG（`-frames:v 1 -q:v 2`）。

**执行器与并发**：
- `app/media.py` 封装 subprocess，命令一律**参数列表**调用、**禁用 `shell=True`**；
  `FFMPEG_PATH` / `FFPROBE_PATH` 可注入替换执行器（本机无 ffmpeg 时测试用 FakeMediaProcessor）。
- 输入源解析严格限制在 `DATA_DIR/videos` 内（绝对/相对路径统一校验，越界或缺失 → 该 Clip 失败）。
- 输出先写临时 `.part` 文件，成功后在 `clips_dir` / `thumbnails_dir` **原子替换**；
  失败清理临时与半成品；stderr 截断写入 Clip/任务错误字段。DB 存相对路径。
- **单 worker**（`ThreadPoolExecutor(max_workers=1)`，app.state 管理）；领取用条件
  `UPDATE ... WHERE status='queued'` 原子独占，杜绝两个线程领取同一任务；
  `MEDIA_SYNCHRONOUS=true` 时在请求线程内同步执行（测试确定性）。
- **重启恢复**：启动时 `running` 视为中断——`attempts < MEDIA_MAX_ATTEMPTS` 则重排
  并 `attempts+1`，否则判 `failed`（重试上限耗尽）；同时调度全部 `queued` 任务。
- **修订隔离**：处理前 / 每片前 / 完成后都校验视频仍 `approved` 且 revision 与任务
  payload 一致；失效 → 任务 `cancelled` 并清理**本次运行产出**的实体文件，**绝不复活**
  已被删除的 Clip 行（worker 从不创建 Clip 行）。
- **部分失败**：失败 Clip 置 `failed` 并写截断错误，Job 置 `failed` 并记录摘要，
  成功片段保留；重试（`media/generate`）只处理 `pending/failed`（未 ready）的 Clip。

### 批次 5：生产跨视频片段库

跨视频聚合「审核通过的标注 + 对应 ready 的 Clip」的分页只读接口（**无需 Alembic 迁移**，
复用现有 Annotation/Video/Clip/BehaviorCategory/User 表；不改变任何现有路由前缀）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/clips` | 片段库分页列表 → `ClipPageOut`（项目成员可读） |
| `GET` | `/api/projects/{project_id}/clips/categories` | 审核通过片段的类别统计 → `ClipCategoryCount[]` |

**入库条件**：标注 `review_status=approved` **且** 所属视频当前 `workflow_status=approved`。
失效回 draft 后仍残留的 approved 标注一并排除（杜绝库内出现已失效片段）；
pending/rejected 一律隔离。`review_status` 查询参数仅接受 `approved`（其余值 422）。

**ClipItem 字段**：`annotation_id, video_id, video_filename, category_id, category_name,
start_time, end_time, start_frame, end_frame, confidence, clip_path, thumbnail_path,
annotator_name, review_status, created_at`。
`clip_path` / `thumbnail_path` 取当前修订（`Clip.source_revision == video.annotation_revision`）
对应 Clip 的相对路径；**Clip 缺失或非 ready（pending/processing/failed）时为 null**，
不校验实体文件是否存在。

**分页与排序**：默认 `page_size=20`、上限 100（`page<1` 或 `page_size>100` → 422）；
排序 `start_time ASC, id ASC` 保证稳定分页。响应 `{items, total, pages}`。

**筛选**：`category_id` / `video_id` / `annotator_id` 精确过滤；`search` 按
类别名或视频文件名大小写不敏感模糊匹配（`%kw%`）。

**实现要点**：
- COUNT 与分页先用**轻量 id 查询**（只 join Video/类别做过滤），随后再一条 join
  取关联列（Video/类别/标注者/当前修订 Clip 一次到位）——**无 N+1**，也不加载
  `crop_region` 等重列。
- 仅项目成员可访问（与其余只读接口一致，不校验 membership.active）。
- **不批量加载视频流**：本接口只返回元数据与相对路径，客户端自行按需请求单条 blob
  （如经 `GET /api/videos/{video_id}/stream` 或后续的 clip 文件接口）。

### 标注

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/videos/{video_id}/annotations` | 视频标注列表 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/annotations` | 新建标注（标注者须为项目成员） |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/annotations/export` | 统一事件 JSON 列表 |
| `PATCH` | `/api/projects/{project_id}/videos/{video_id}/annotations/{annotation_id}` | 更新标注（标注者本人或 owner/admin） |
| `DELETE` | `/api/projects/{project_id}/videos/{video_id}/annotations/{annotation_id}` | 删除标注（同上权限） |

### 其它

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查 |

## 数据模型要点

- `User`：独立登录账号，**无全局角色**。
- `ProjectMembership`：`user_id + project_id` 唯一，`role ∈ {owner, admin, annotator, reviewer}`。
- `BehaviorCategory`：项目级类别（name/group/color/sort_order/is_active）；创建项目时初始化北医 12 类。
- `Video`：项目级元数据（filename/duration/fps/width/height/storage_path/status）。
  - 媒体 `status ∈ {metadata, uploaded, needs_transcode}`：`metadata` 为 P1 Mock 创建；
    批次 2 真实上传按扩展名映射为 `uploaded`（mp4/webm/mov/m4v）或 `needs_transcode`（avi/mkv/wmv/mpeg/mpg）。
  - 新增独立的工作流字段：
    `workflow_status ∈ {draft, submitted, approved, rejected}`（默认 `draft`）、
    `annotation_revision`（≥1，默认 1）、`submitted_at` / `approved_at` / `approved_by`（可空）。
- `Annotation`：视频级标注（起止时间、起止帧、confidence、review_status、crop_region 可空）。
  `annotator_id` `NOT NULL + ON DELETE RESTRICT`（仍被引用时不可删用户），
  `reviewer_id` 可空并 `ON DELETE SET NULL`（删除用户不销毁标注）。
- `Video.uploaded_by` 可空并 `ON DELETE SET NULL`（删除上传者后视频元数据保留）；
  `Project.created_by` `NOT NULL + ON DELETE RESTRICT`（仍被引用时不可删创建者）。
- `Review`：审核历史表（project/video/reviewer/result/comment/annotation_revision/created_at）；
  `reviewer_id` 可空并 `ON DELETE SET NULL`，删除用户不销毁审核历史。
- `Clip`：标注片段（project/annotation/source_revision/status/clip_path/thumbnail_path/error/
  generated_at/created_at/updated_at）；`annotation_id + source_revision` 唯一，支持修订隔离——
  已审核标注被修改后按新修订生成新 clip，旧 clip 由未来生命周期显式删除。
  `status ∈ {pending, processing, ready, failed, stale}`（默认 `pending`）。
- `BackgroundJob`：后台任务（clip 生成 / export / cleanup 共用）——
  job_type/status/progress 0..100/payload/result_path/error/started_at/finished_at/expires_at；
  `project_id` 可空以支持全局清理任务；`status ∈ {queued, running, succeeded, failed, cancelled}`；
  批次 4 新增 `dedupe_key`（可空唯一，同视频+修订仅一行任务，防重复）与 `attempts`
  （领取/中断重排计数，重启恢复重试上限判定）。
- 类别被标注引用时不可物理删除（外键约束）；P1 仅提供类别读取。

## 校验规则

- `end_time > start_time`、`end_frame > start_frame`，且均 ≥ 0。
- 类别与视频必须属于同一项目。
- 创建标注的标注者（当前登录用户）必须是项目成员。
- 更新/删除标注：仅标注者本人或项目 `owner/admin`；标注写入（增/改/删）另限 `owner/admin/annotator`（reviewer 403）。
- `confidence ∈ {certain, uncertain, occluded}`；`review_status ∈ {pending, approved, rejected}`（由审核 API 流转，直接写 422）。
- 提交：仅 `owner/admin/annotator`、至少 1 条标注、仅 `draft/rejected`。
- 审核队列/裁决：仅 `owner/admin/reviewer`；裁决仅 `submitted`。
- Clip 实体文件删除严格限制在 `clips_dir/thumbnails_dir` 内，越界一律不删除并记录异常。

## 导出格式

`GET .../annotations/export` 返回事件 JSON 列表，字段符合 `../需求文档.md` §2.3：

```json
{
  "video_id": "video_1",
  "start_time": 12.4,
  "end_time": 14.84,
  "start_frame": 310,
  "end_frame": 371,
  "behavior": "攻击行为",
  "crop_region": null,
  "confidence": "certain",
  "annotator": "demo",
  "reviewer": null,
  "review_status": "pending"
}
```

## 测试

```bash
cd backend
.venv\Scripts\activate
pytest -q
```

覆盖：登录、创建项目（owner + 12 类）、跨项目访问拒绝、有效/无效标注、更新/删除、导出字段与类别名，
视频流式上传（权限/跨项目、扩展名大小写、空文件、同名不覆盖、分块流式写入、磁盘不足 507、
写入异常/DB 失败清理、上传后流式读取与路径安全、无固定大小限制、Content-Type 仅辅助），
批次 3 提交与审核（提交角色/状态门/至少一条标注/标注审核字段重置、队列角色与 submitted 过滤、
审核历史成员可读与跨修订累积、approved/rejected 同步视频与标注、重复操作与跨项目拒绝），
批次 3 失效联动（三种非 draft 状态下的创建/修改/删除回 draft、revision 仅 +1 且 draft 后稳定、
Clip 行与实体文件删除、仅影响本视频 Clip、Review 历史保留、越界路径不删除且记日志、
文件删除失败记日志不阻断、reviewer 不可写标注、直接写 review_status 422），
批次 4 媒体（ffmpeg 命令构造无 shell / 超时 / 音频映射 / stderr 截断、approved 自动入队与
pending Clip、rejected 不入队、生成幂等/重试/角色与 approved 状态门、media-status 与 job 查询
成员权限、进度与部分失败保留成功片段、重试只处理 failed、重启恢复 running 重排/判失败、
修订失效竞态取消且不复活 Clip、文件原子性与半成品清理、输入源路径安全、实际 DB 迁移 0004），
批次 5 片段库（仅审核通过且视频 approved 才入库、pending/rejected 与失效残留隔离、
非 ready Clip 路径为 null、ready 路径与批次 4 产物一致、分页默认 20/上限 100/稳定排序、
category/video/annotator/search 筛选、跨项目隔离、多视频聚合、类别统计、成员权限、
ClipItem 字段完整性、review_status 仅允许 approved），
以及迁移验收（全新库建全表 / P1 旧库数据保留并新增列默认正确 / 空 alembic_version 表缺陷回归 /
0002 与 0003 已版本化库到 0004 / 未知版本与非预期表安全报错 / 重复迁移幂等 / 启动自动迁移 /
CLI --check 输出区分空版本表 / 外键 ON DELETE：删除用户后 uploaded_by、reviewer_id 置空，
被 created_by、annotator_id 引用时删除被拒绝 / 新模型约束：唯一性、外键级联、状态默认与检查约束 /
dedupe_key 唯一约束防重复任务）。

## 已知边界

- 未实现用户注册接口（通过 `data/` 内联管理或后续 P2 补充）。
- 视频上传（批次 2）已支持真实文件流式上传（`POST .../videos/upload`）；
  P1 的 JSON Mock 接口（`POST .../videos`）保留不变。**未实现**：ffprobe 元数据探测、
  进度回调。
- 视频流安全边界：`GET .../stream` 只服务解析后位于配置视频目录（`DATA_DIR/videos`）**内部**的文件；绝对路径或含 `../` 的路径逃出该目录一律 404，避免任意读取项目外敏感文件。
- 批次 4 已实现精确片段/缩略图后台生成（仅 approved，单 worker 串行、可恢复/重试、修订隔离）；
  媒体执行器依赖本机 ffmpeg/ffprobe（或经 `FFMPEG_PATH`/`FFPROBE_PATH` 注入），
  本机无 ffmpeg 时不影响 API 与审核流程（任务以失败状态记录，可重试）。
- 批次 5 片段库只读接口已实现（跨视频聚合审核通过标注与 ready Clip）；
  分类导出、`cleanup-issues.log` 补偿清理任务（孤儿文件清理）留待批次 6/7；
  类别停用/删除管理界面留待后续批次。
