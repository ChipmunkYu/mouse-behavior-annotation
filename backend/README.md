# 标注网站后端（第一阶段 / P1）

模块化单体的最小后端，服务于多人在线行为标注网站（对应 `../需求文档.md`）。

- 技术栈：Python 3.11、FastAPI、SQLite、SQLAlchemy 2.x、Pydantic v2
- 范围：真实数据模型 + CRUD；Mock/seed 仅用于账号、项目、视频元数据
- 全部 API 位于 `/api` 前缀下，认证使用 JWT Bearer 令牌

## 目录结构

```
backend/
├── app/
│   ├── main.py          # 应用工厂（建表、种子、CORS、路由注册）
│   ├── config.py        # 环境变量配置
│   ├── database.py      # SQLAlchemy 引擎 / Session
│   ├── models.py        # User / Project / ProjectMembership / BehaviorCategory / Video / Annotation
│   ├── schemas.py       # Pydantic 请求/响应模型
│   ├── auth.py          # 密码哈希（PBKDF2）+ JWT
│   ├── seed.py          # 北医 12 类初始化 + demo 账号
│   ├── deps.py          # 项目成员权限依赖
│   └── routers/         # health / auth / projects / categories / videos / annotations
├── scripts/             # 本地工具脚本（仅开发，不注册到应用）
│   └── seed_demo.py     # 幂等演示数据脚本（第一阶段本地演示）
├── tests/               # pytest 聚焦测试
├── data/                # 运行时数据（数据库/视频/导出，已 gitignore）
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

数据库、上传视频、导出目录均从环境变量配置，默认位于 `backend/data/` 下（已被 gitignore）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 数据根目录（含数据库） |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/annotation.db` | 数据库连接串 |
| `SECRET_KEY` | 开发用弱密钥 | JWT 密钥，生产必须覆盖 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` | 令牌有效期（分钟） |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `demo` / `demo123` | 种子开发账号 |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 允许的前端来源（本地 Vite） |

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
| `POST` | `/api/projects/{project_id}/videos` | JSON 创建视频元数据（P1 Mock 上传） |
| `GET` | `/api/videos/{video_id}/stream` | 若 `storage_path` 解析到配置视频目录内且文件存在则 `FileResponse`，否则 404 |

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
- `Annotation`：视频级标注（起止时间、起止帧、confidence、review_status、crop_region 可空）。
- 类别被标注引用时不可物理删除（外键约束）；P1 仅提供类别读取。

## 校验规则

- `end_time > start_time`、`end_frame > start_frame`，且均 ≥ 0。
- 类别与视频必须属于同一项目。
- 创建标注的标注者（当前登录用户）必须是项目成员。
- 更新/删除标注：仅标注者本人或项目 `owner/admin`。
- `confidence ∈ {certain, uncertain, occluded}`；`review_status ∈ {pending, approved, rejected}`。

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

覆盖：登录、创建项目（owner + 12 类）、跨项目访问拒绝、有效/无效标注、更新/删除、导出字段与类别名。

## 已知边界（P1）

- 未实现用户注册接口（通过 `data/` 内联管理或后续 P2 补充）。
- 视频上传保持简单：`POST .../videos` 接受 JSON 元数据；`storage_path` 为绝对路径或相对 `data/videos/` 的相对路径。
- 视频流安全边界：`GET .../stream` 只服务解析后位于配置视频目录（`DATA_DIR/videos`）**内部**的文件；绝对路径或含 `../` 的路径逃出该目录一律 404，避免任意读取项目外敏感文件。
- 审核流程（reviewer / review_status 流转）留待 P2，导出中 `reviewer` 固定为 `null`。
- 类别停用/删除管理界面留待 P2。
