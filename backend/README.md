# 标注网站后端（生产闭环 + YOLO 检测结果导入与 track 修正）

模块化单体的最小后端，服务于多人在线行为标注网站（对应 `../需求文档.md`）。

> 当前技术文档术语遵循[项目术语表](../项目术语表.md)；现行架构见[检测状态、提交审核与独立行为视频片段导出设计](../docs/设计/检测状态、提交审核与独立行为视频片段导出设计.md)。代码/API 标识符保持不变。

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
- YOLO track 能力：三文件导入批次/替换、逐帧检测与修正后 track 查询、行为标注（`Annotation`）`mouse_ids`、Split、Merge、整轨检测抑制/撤销、三类修订审核，以及每个 `SubmissionAnnotation` 独立四文件的项目 ZIP 导出。`mouse_ids` 是语义与目标种类无关的历史兼容字段名。
- 正式项目导出是每片段固定 `clip.mp4`、`annotation.json`、`tracks.json`、`metadata.json` 的四文件 ZIP；单视频 `/annotations/export` 仅为 legacy 兼容 JSON API，两者不是同一契约。

## 目录结构

```
backend/
├── alembic.ini # Alembic 配置（迁移目录为 migrations/）
├── migrations/ # Alembic 迁移脚本（版本号只存在于 scripts/versions 内）
│ ├── env.py # 迁移环境（SQLite 批处理模式，复用 app 模型元数据）
│ ├── script.py.mako
│ └── versions/
│ ├── 0001_baseline_p1.py # baseline：P1 原版 6 张表
│ ├── 0002_review_clip_job.py # 增量：Video 工作流字段 + Review/Clip/BackgroundJob
│ ├── 0003_fk_ondelete_explicit.py # 增量：users 外键 ON DELETE 策略显式化（SET NULL / RESTRICT）
│ ├── 0004_background_job_dedupe_attempts.py # 增量：BackgroundJob 幂等去重键 + 重试计数
│ ├── 0005_detection_import_foundation.py # YOLO 导入、修正后 track、IdentityEdit/抑制及多修订模型
│ ├── 0006_detection_import_batch_paths.py # 三文件批次及导入文件路径
│ ├── 0007_detection_import_source_relative.py # DetectionImport.source_relative 前向迁移
│ ├── 0008_detection_submission_foundation.py # sparse draft、Submission 与 DetectionSnapshot 基础
│ ├── 0009_submission_media_authority.py # Submission 媒体 authority 与独立片段关系
│ ├── 0010_submission_integrity_digests.py # snapshot 完整性 digest
│ └── 0011_immutable_authority_file_identity.py # authority 不可变 trigger 与源文件 identity
├── app/
│ ├── main.py # 应用工厂（自动迁移、CORS、路由注册、媒体/导出 worker 生命周期）
│ ├── config.py # 环境变量配置
│ ├── database.py # SQLAlchemy 引擎 / Session / ensure_schema
│ ├── migration.py # 程序化 Alembic 入口（启动自动迁移 / CLI 共用）
│ ├── models.py # 业务、审核/媒体、YOLO 检测结果导入与 track 修正模型
│ ├── schemas.py # Pydantic 请求/响应模型
│ ├── auth.py # 密码哈希（PBKDF2）+ JWT
│ ├── seed.py # 北医 12 类初始化 + demo 账号
│ ├── deps.py # 项目成员权限依赖
│ ├── media.py # 媒体执行器：ffmpeg/ffprobe 子进程封装（无 shell）+ 命令构造
│ ├── media_jobs.py # 媒体任务编排：单 worker 领取 / 逐片重编码 / 重启恢复 / 修订隔离
│ ├── export_jobs.py # 项目分类导出：缺失片段补生成、ZIP 打包、任务恢复
│ └── routers/ # health / auth / projects / categories / videos / annotations / reviews / clips / media / exports / detection_imports / identity_edits / suppressions
├── scripts/ # 本地工具脚本（仅开发，不注册到应用）
│ ├── migrate.py # 数据库迁移 CLI（全新库 / P1 旧库升级 / 幂等）
│ └── seed_demo.py # 幂等演示数据脚本（第一阶段本地演示）
├── tests/ # pytest 聚焦测试
├── data/ # 运行时数据（数据库/视频/导出，已 gitignore）
├── requirements.txt
├── .env.example
└── README.md
```

## 快速开始

```bash
cd backend

# 1. 创建隔离环境
python -m venv .venv
.venv\Scripts\activate # Windows
# source .venv/bin/activate # Linux/macOS

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
baseline（0001）再升级到 head（0011），**不删除任何已有数据**；重复启动无副作用。
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

- 全新空库 → `upgrade head`（0001 建 P1 全表，0002 增量，0003 外键策略显式化，0004 任务去重键，0005～0007 增加检测导入；0008 增加 sparse detection state 与不可变提交基础并回填可恢复的当前有效状态；0009 允许新 Clip 仅关联不可变 `SubmissionAnnotation`；0010 校验既有 0009 authority 后增加并回填 DetectionSnapshot 完整性 digest；0011 增加稳定源文件 identity 与 SQLite authority 不可变 trigger）。
- P1 旧库（未版本化，含空版本表缺陷形态）→ 自动 `stamp 0001` 标记 baseline 后 `upgrade head`，旧数据原样保留。
- 0002～0010 已版本化库 → 按迁移链增量 `upgrade head` 到 0011；进入 0008 前严格预检 legacy current state，不完整时硬失败，进入 0010 前严格预检既有 0009 snapshot authority。
- 已版本化 → 幂等 `upgrade head`。
- 非预期表 / 未知版本 / 版本表损坏 → `--check` 与迁移均报错退出（退出码 2），不执行任何修改。

0010 在任何 digest 列 DDL 前验证既有 0009 snapshot 的 raw/state count、same-import
关系、header 与 pose metadata；关键点名称必须是非空、唯一、去除首尾空白的字符串数组，
skeleton edge 必须恰含两个非 bool 整数，索引有效，并禁止自环及正反向重复边。损坏数据会
保持 revision 0009 且不遗留 digest 列。0011 为已存在 snapshot 的 raw baseline 增加
INSERT/UPDATE/DELETE 数据库 trigger，并保护 frozen Submission authority；迁移和内存库
`create_all` 共用同一 trigger 定义，downgrade 会移除。

Submission 锁外 SHA-256 与稳定 identity 从同一个已打开 descriptor/Windows handle 前后取得；
短 Video gate 再验证当前 storage key、media revision 与路径 identity。媒体 worker 从一个
已验证 open handle 流式 hash/copy 到 `videos_dir` 内 job 私有 staging 文件，ffmpeg 只读取
该 staging 副本，所有成功、失败和重试路径均清理 staging。时间参数保持 9 位小数。
25/30/60 FPS 已在真实 ffmpeg/ffprobe 环境完成实际帧数、尺寸、时长、首末帧和缩略图验收。

### Sparse writer 切换维护命令

仅在从 legacy writer 切换到 Phase 2 sparse writer 的维护窗口执行一次；可重复运行的前提是
DraftIdentityEdit/DraftDetectionChange 栈为空且所有旧后端/worker 已停止：

```bash
# 1. 停止全部后端与 worker，备份 SQLite 数据库
# 2. 确认 schema 已在 0008
.venv\Scripts\python scripts\migrate.py --check
# 3. 显式确认 legacy writer 已停止并执行 BEGIN IMMEDIATE reconciliation
.venv\Scripts\python scripts\reconcile_detection_state.py \
  --confirm-legacy-writer-stopped \
  --db-url sqlite:///./data/annotation.db
```

命令会严格验证 legacy current state、清空并重建 active sparse override、重算 cursor，要求
`shadow_difference_count=0` 后才同步 Video/Annotation identity revision；draft 栈非空、并发写、
shadow 差异或任何异常都会整体 rollback。成功后再启动当前代码；当前代码不再写旧身份/抑制表。

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

批次 4（媒体任务）、批次 5（生产跨视频片段库）、批次 6（项目分类导出）与
批次 7（生命周期清理）已实现，
见下文对应章节。

## Demo 账号

| 用户名 | 密码 | 说明 |
|---|---|---|
| `demo` | `demo123` | **仅开发使用**；部署前必须通过环境变量 `DEMO_USERNAME` / `DEMO_PASSWORD` 覆盖，或自行创建用户 |

## 演示数据

`scripts/seed_demo.py` 为第一阶段本地演示提供幂等的演示数据（项目 / 视频 / 标注 / demo 账号），
只复用现有配置、模型与 seed 逻辑，不修改任何接口。从 `backend` 目录运行：

```bash
.venv\Scripts\python scripts\seed_demo.py # 仅 Mock 元数据
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
| `CLEANUP_ENABLED` | `true` | 是否启动生命周期清理 worker；关闭时仍可手工运行脚本 |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | 启动清理一次后的周期秒数 |
| `TEMP_RETENTION_HOURS` | `24` | 已知程序临时文件和孤儿导出 ZIP 的保留时间 |
| `JOB_RETENTION_DAYS` | `30` | 无结果路径的 terminal 后台任务日志保留天数 |

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

### YOLO 检测结果导入与 track 修正

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/projects/{project_id}/video-import-batches` | 创建三文件导入批次（原始视频 + `tracks.jsonl` + `metadata.json`） |
| `PUT` | `/api/projects/{project_id}/video-import-batches/{batch_id}/files/{role}` | 独立上传 `video` / `tracks` / `metadata` |
| `POST` | `/api/projects/{project_id}/video-import-batches/{batch_id}/complete` | 完成配对校验并创建视频/DetectionImport |
| `GET` | `/api/projects/{project_id}/video-import-batches/{batch_id}` | 查询槽位、校验错误和导入状态 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/detection-imports` | 为已有视频补传或确认替换 tracks/metadata |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/detection-imports/current` | 当前导入、统计和修订 |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/detections` | 按帧区间读取有效检测及 import/identity revision |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/corrected-tracks` | 查询修正后 track 摘要、搜索和当前帧可见性 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/identity-edits/check` | 预检 Split/Merge 冲突及影响范围 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/identity-edits` | 提交 Split/Merge 操作 |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/identity-edits/history` | 查询当前 draft 的 LIFO undo 栈（不是永久审计） |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/identity-edits/{edit_id}/revert` | 仅撤销栈顶 Split/Merge；非栈顶或类型不匹配返回 409 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/detection-suppressions` | 以 sparse override 整轨抑制当前未抑制 detection；不写旧 suppression 表 |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/detection-suppressions` | 将当前 draft 栈中的 `suppress_track` edit 映射为兼容 suppression 列表 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/detection-suppressions/{suppression_id}/revert` | 仅在该 suppression edit 为栈顶时撤销，否则返回 409 |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/detections/export` | legacy 兼容的修正后 track JSONL/manifest 接口；不属于正式项目 ZIP |

`metadata.json` 接受规范 `frame_count`，并兼容真实样本的 `processed_frames` / `declared_frame_count`；模型、校验和、tracker、推理参数和骨架同时接受实际字段 `model`、`model_sha256`、`tracker`、`parameters`、`skeleton_edges_0based`。`source_relative` 按 basename 与视频文件名匹配。新视频成功导入时同步 FPS、宽、高和 `duration=frame_count/fps`；已有视频替换时校验 source basename、FPS、宽、高和当前导入 `frame_count`。预览及失败均清理候选文件，只有 `confirm=true` 成功才保留。原始 YOLO 文件与 RawDetection 保持不可变。

Split、Merge、整轨 suppression 与 LIFO undo 以 `DetectionImport.edit_version` 为 authority，并同步投影到 `Video/Annotation.identity_revision`；每次操作都会按 SQL effective detection 重校验 Annotation。当前 `Submission` authority 处于 `submitted` 时锁定编辑并要求先 withdraw；`approved/rejected` 的 `Video` 兼容投影在新编辑后回到 draft，但不会修改已冻结的 Submission/DetectionSnapshot。撤销严格限栈顶，cursor 不回退、display ID 不复用。

Detection edit、Annotation create/update/delete、submit 和 detection replacement 统一先执行 Video no-op UPDATE 获取 SQLite 写门禁，再在锁内重读 active import、detection/edit/annotation revision 与 submitted 状态；锁竞争的 busy/locked 统一返回可重试 409。submitted 对 Annotation 与 replacement 同样是硬锁，不再隐式退回 draft。当前 corrected export 只接受 active import 的当前 `edit_version`，历史 import/revision 明确返回 409；legacy JSONL 按 frame/detection/raw ID 稳定排序并以 `yield_per(500)` + `StringIO` 有界读取构造，正式项目 ZIP 内每个 `SubmissionAnnotation` 的 `tracks.json` 已实现按帧直接流式写入 staging 文件。

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

这是视频 `workflow_status` 的工作流，对应界面“草稿/待审核/已通过/已退回”。单条行为标注的 `Annotation.review_status` 是独立的 `pending/approved/rejected`，不含 `draft/submitted`；提交视频时标注置为 `pending`，裁决后再置为 `approved` 或 `rejected`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/projects/{project_id}/videos/{video_id}/submit` | 冻结 Submission/DetectionSnapshot 后提交 → `VideoOut` |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/withdraw` | 撤回尚未裁决的 Submission → `VideoOut` |
| `GET` | `/api/projects/{project_id}/reviews/queue` | 审核队列 → `VideoOut[]`（仅 `submitted`） |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/reviews` | 审核历史 → `ReviewOut[]`（含所有修订） |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/review` | 审核裁决 → `ReviewOut` |

- **submit**：仅 `owner/admin/annotator`；至少 1 条标注；仅 `draft/rejected` 可提交
  （`submitted/approved` 拒绝 400）。`_revalidate_annotations` 只校验并返回问题与待同步修订，不写
  Annotation；任一语义无效项返回 400，数据库不变。全部语义校验通过后，才在同一成功事务内
  将已验证 Annotation 置为 `mouse_id_status=valid`、推进 `detection_import_revision`/
  `identity_revision`，再把视频置为 `workflow_status=submitted`、更新 `submitted_at`，并将本修订
  全部标注置为 `review_status=pending`、`reviewer_id=null`。因此修订 stale 但语义仍有效的标注可在
  成功提交时推进，而不会仅因已存修订过期被拒绝。
- **queue**：仅 `owner/admin/reviewer`；只返回 `submitted` 视频（跨项目隔离，按 `submitted_at` 倒序）。
- **reviews（历史）**：项目成员均可读该视频完整审核历史，跨修订累积，不因失效删除。
- **review**：仅 `owner/admin/reviewer`；仅 `submitted` 可裁决（其余状态 400，重复裁决 400）；
  approval 前再次调用纯校验 `_revalidate_annotations`，无效时返回 409 且数据库不变；通过时在
  同一成功事务内将已验证 Annotation 置为 `valid` 并同步 detection import/track 修正修订；
  追加一条 `Review`（`annotation_revision` = 裁决时视频修订号）并同步：
  `approved` → 视频 `approved/approved_at/approved_by`、标注 `approved/reviewer_id`；
  `rejected` → 视频 `rejected`、清空 approved 字段、标注 `rejected/reviewer_id`。

Phase 3 authority：`Submission + DetectionSnapshot + SubmissionAnnotation` 是新提交与裁决的唯一
权威数据；`Video.workflow_status`、`Annotation.review_status` 仅作 UI 兼容投影。submit 由服务端计算
受控源媒体 SHA-256；withdraw 允许 owner/admin/annotator 或原 submitter，且仅限无 Review 的 submitted
attempt。approve 同事务写 Review、supersede、queued job 和 SubmissionAnnotation-only Clip，commit 后调度。

0011 在 Submission 冻结 source size/mtime_ns/device/inode；Windows 在 Python stat identity 不可用时通过 Win32 file ID 获取 volume/file index。SQLite trigger 在数据库层冻结已引用 snapshot/state、Submission authority、SubmissionAnnotation 与 raw baseline，同时保留未引用 snapshot 的 child-first cleanup。ffmpeg 时间参数采用 9 位小数，避免 25/30/60 FPS 边界被两位小数量化。Phase 4 已实现并完成合并，真实 ffmpeg/ffprobe 的 25/30/60 FPS 媒体验收已通过。

Gate 3 remediation：submit 在 Video write gate 外解析受控 storage key、记录 size/mtime_ns
并全量计算 SHA-256；短 gate 内只重验 DB identity 与 stat identity，Submission 冻结该 hash，
worker 渲染前仍全量复核。DetectionSnapshot 同时冻结 raw/state/metadata 三个确定性 digest，
复用和审核均做 count+digest 完整性检查，metadata 文件按导入时 SHA-256 复核。

Submission 媒体统一采用 `submission_media_plan`：Annotation 的 `start_frame/end_frame` 都是
inclusive，渲染区间为 `[start_frame/fps, (end_frame+1)/fps)`；时间字段允许一帧舍入误差。
clip 与 thumbnail 使用同一整数 crop。已入队的 approved Submission 即使后来 superseded
仍可恢复完成，默认片段库只展示 current approved；有 new authority 历史的视频不回退 legacy。
0009 仅把 Clip legacy authority 列改为 nullable；旧行和旧表均保留。

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
  out-of-bounds}`）并记入应用日志，**不阻断业务请求、绝不无声**；批次 7 清理任务据此补偿。

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
  并 `attempts+1`，否则判 `failed`（重试上限耗尽）。仅对本次确认中断并重排的 media/export
  job，将其关联且仍属视频当前 revision 的 `processing` Clip 通过
  `id + status + updated_at` CAS 重置为 `pending`；普通等待超时不会重置或夺取活跃 claim。
  应用先完成两类 job/Clip 恢复，再调度全部 `queued` 任务，避免 worker 启动顺序互相破坏。
- **部署边界**：Clip claim 目前没有持久化 owner token/heartbeat/lease，本恢复语义仅适用于当前
  app 内单进程 executor 部署。不得使用 `uvicorn --workers N` 或让多个应用进程共享该任务库；
  如需多进程，必须先增加 claim owner + heartbeat + stale lease CAS 回收，不能依赖本启动恢复。
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

### Phase 4：Submission authority 独立四文件项目 ZIP 导出

项目 ZIP 只从入队瞬间的 current approved `Submission` 解析并冻结具体 category ID、
`Submission` ID、`SubmissionAnnotation` ID、DetectionSnapshot 与 source 引用；worker 不重新
选择 current 状态，也不读取 current Annotation/Category.name、draft override、Video workflow
projection 或用户身份作为导出内容。已入队后 Submission 若被新批准版本 supersede，冻结内容
因数据库不可变保护仍允许完成，但不得混入新 approved Submission。

每个 `SubmissionAnnotation` 输出一个独立目录和固定 `clip.mp4`、`annotation.json`、
`tracks.json`、`metadata.json` 四文件，不再输出集中 annotations/manifest/corrected_tracks。
最终类别数为 1 时片段目录平铺 ZIP 根；大于 1 时仅增加 category_name 快照目录（忽略 group）。
轨迹按帧直接流式写文件并保留空帧；坐标转换到 crop 后 clip pixels：检测框与 crop 无交集时
排除，相交时 clamp；关键点平移并 clamp，crop 外关键点置信度置 0，不改变 display track ID。
每个 clip 必须通过 ffprobe 的帧数/FPS/尺寸检查及四文件 schema/业务校验。全部 staging 完成且
ZIP 完整性检查通过后，才在发布前短事务中复核冻结引用与 job claim/payload，并以同文件系统
`os.replace` 原子发布；任一失败不暴露 partial ZIP。

单视频 `GET .../annotations/export` 仍是 legacy JSON 兼容 API，不是正式项目 ZIP 契约。

接口均要求项目 `owner/admin`；任务详情沿用通用 job 路由，但 export 类型同样执行该角色限制。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/projects/{project_id}/export` | 新建导出任务，可选 `category_ids`；同项目 queued/running 时 409 |
| `GET` | `/api/projects/{project_id}/export/status` | 最近任务及 `exportable_count/ready_count/missing_count/missing_clips` |
| `GET` | `/api/projects/{project_id}/jobs/{job_id}` | 项目隔离的任务详情；export 仅 owner/admin |
| `GET` | `/api/projects/{project_id}/export/download` | 下载最近成功、未过期且实体路径安全的 ZIP |

**任务与文件语义**：
- 每次导出新建一条 `BackgroundJob(job_type=export)` 和唯一 dedupe key，保留历史任务、结果与
  过期时间；项目 active 导出通过 queued/running 查询排他。
- 状态统计以最近任务冻结的类别范围为准；ready 必须满足 Submission Clip `status=ready`、
  `clip_path` 位于 `DATA_DIR/clips` 内且实体文件存在，否则列入 missing。
- 项目导出 worker 对 missing Submission Clip 复用注入的 `MediaProcessor` 在后台任务内补生成；
  Export 页面/API 请求线程本身不直接同步调用 ffmpeg。任一片段失败则任务失败且不发布 ZIP。
- ZIP 先在 `DATA_DIR/exports` 生成任务专属临时 archive，再原子替换为
  `export_project_{project_id}_{job_id}.zip`；每次结果文件唯一。下载同时校验成功状态、
  `EXPORT_RETENTION_DAYS`（默认 7 天）、路径边界和实体存在性。

### 批次 7：生命周期清理

应用启动时立即清理一次，此后由单线程周期 worker 执行；每次真实运行写入一条
`BackgroundJob(job_type=cleanup)` 成功或失败记录，并以进程内非阻塞锁防止重叠。该 worker 与
媒体/导出 executor 一样只支持当前**单应用进程**部署，不可让多个进程共享任务库和数据目录。
原视频永不自动删除；有效 DB 引用的 Clip/缩略图长期保留；导出 ZIP 仅在对应 export job
到期（`expires_at <= now`）后删除。程序已知的 `.part`、export staging/tmp 及无有效未过期
export job 引用的程序命名孤儿 ZIP 保留 24h，无结果路径的 terminal job 保留 30d。
`DATA_DIR` 是可由管理员配置并解析的可信 anchor，但 `videos/clips/thumbnails/exports` 子根及其
anchor 后的任何组件只要是 symlink，该 lane 就整体拒绝；删除前还会复核实体类型和 lstat 身份。

过期 ZIP 使用 file-first、逐 job 短事务：文件删除后立即清空该 job 的 `result_path` 并提交；若
提交失败，DB 引用与审计记录仍保留，下轮会把“文件已不存在”视为可自愈并再次清空引用。
terminal 非 export、失败 export、非法/越界结果路径不会永久保护 ZIP：先安全清空脏引用，满
30 天后再删除任务。清理异常 JSONL 只是审计与重试线索，不能授权删除；补偿删除只接受正式
`clip_{annotation_id}_rev{revision}.mp4/.jpg`，并再次校验可信根、Clip 无引用及当前视频修订。

手工检查或执行同一套规则：

```bash
.venv\Scripts\python scripts\cleanup_retention.py --dry-run
.venv\Scripts\python scripts\cleanup_retention.py
```

`--dry-run` 对 ZIP、临时文件、`result_path`、异常日志和后台任务均为零副作用；脚本仍会先运行
`ensure_schema`，因此数据库迁移/建表不属于 dry-run 保证范围。

### 标注

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/projects/{project_id}/videos/{video_id}/annotations` | 视频标注列表 |
| `POST` | `/api/projects/{project_id}/videos/{video_id}/annotations` | 新建标注（标注者须为项目成员） |
| `GET` | `/api/projects/{project_id}/videos/{video_id}/annotations/export` | 完整 ExportEvent JSON 列表；含 `annotation_id`、`mouse_ids`、修订和 `clip_file`（可为 null） |
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
- `DetectionImport` / `RawDetection`：保存不可变导入与逐帧原始检测；当前修正状态由 sparse override 表达。
- `DetectionStateOverride`：当前 draft 相对 RawDetection baseline 的稀疏 display/suppressed 状态。
- `DraftIdentityEdit` / `DraftDetectionChange`：当前 draft 的紧凑 LIFO undo 栈与受影响 detection before/after；不是永久审计。
- 旧 `CorrectedTrack` / CDA / `IdentityEdit` / suppression 表仅保留迁移兼容，当前运行时不再写入。
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
  已审核标注被修改后按新修订生成新 clip，旧 clip 由已实现的批次 7 生命周期清理规则处理。
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

`GET .../annotations/export` 返回下列完整 ExportEvent 列表，是保留的单视频 legacy JSON
兼容 API；其中 `clip_file` 在没有 ready legacy Clip 时可为 `null`。正式项目 ZIP 不复用该格式，
其契约为本 README 的 Phase 4 独立四文件结构，且没有集中式 `annotations.json`：

```json
{
  "annotation_id": 123,
  "video_id": "video_1",
  "start_time": 12.4,
  "end_time": 14.84,
  "start_frame": 310,
  "end_frame": 371,
  "behavior": "攻击行为",
  "mouse_ids": [8, 20],
  "detection_import_revision": 1,
  "identity_revision": 7,
  "clip_file": null,
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

当前最终全量结果：`397 passed, 3 skipped`。前端 `npm run build` 通过。真实 ffmpeg/ffprobe 的 25/30/60 FPS 验收也已通过；这些证据不替代公网入口、备份恢复或持续的浏览器回归验收。

覆盖：登录、创建项目（owner + 12 类）、跨项目访问拒绝、有效/无效标注、更新/删除、导出字段与类别名，
三文件导入批次/替换与真实 metadata 别名、source basename 和视频元数据同步/替换兼容校验、候选文件清理、逐帧查询、无导入 `needs_mouse_ids` 草稿、`mouse_ids` 数量与覆盖校验、Split/Merge、active suppression 列表与刷新恢复、旧 import 撤销 409、全部 Annotation 重校验、并发修订冲突、三类审核修订失效、保留空帧且可 round-trip 的修正后 track 结果、legacy 单视频 ExportEvent，以及每个 `SubmissionAnnotation` 独立四文件 ZIP 的完整性，
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
ClipItem 字段完整性、review_status 仅允许 approved），批次 6 项目导出（首次入队、项目 active
排他、owner/admin 权限、项目/category/job 隔离、类别筛选与 scoped status、ready 实体安全校验、
missing 自动补生成与失败不发布、独立四文件 ZIP、下载过期/越界/缺文件、重跑保留历史），
以及迁移验收（全新库建至 head 0011 / P1 旧库数据保留并新增列默认正确 / 空 alembic_version 表缺陷回归 /
已版本化旧库的代表路径（0002、0003、0004、0006、0007、0009、0010）升级至 head 0011 /
0008 sparse state 回填与严格预检 / 0009 Clip nullable 过渡 / 0010 digest 回填、损坏 authority 原子拒绝及降级重升 /
0011 SQLite trigger 安装、降级移除与重升恢复 / 未知版本与非预期表安全报错 / 重复迁移幂等 / 启动自动迁移 /
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
- 批次 5 片段库只读接口、批次 6 项目分类 ZIP 导出与批次 7 生命周期清理已实现；
  类别停用/删除管理界面留待后续批次。
