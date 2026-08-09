# 标注网站前端（frontend）

多小鼠社会行为标注网站的前端实现：Vite + React 18 + TypeScript + React Router。
对应后端 `../backend`（FastAPI，接口见 `../backend/README.md`）。

> 当前界面与文档术语遵循 `../项目术语表.md`。

## 功能

- **登录** `/login`：demo/demo123 开发账号提示、表单校验、错误态；登录后 token 与用户信息存入 localStorage。
- **项目列表** `/projects`：展示当前用户成员项目及项目内角色，支持创建项目（自动初始化 12 类行为类别）。
- **视频库** `/projects/:projectId/videos`：
  - **三文件导入批次**（主操作）：同一批次分别上传原始视频、`tracks.jsonl` 和 `metadata.json`，显示各槽位状态并支持独立重试；视频可先完成入库，结构化数据配对通过后启用 track 功能。已有视频支持补传/替换检测结果。
  - 保留单视频上传 `POST /api/projects/:projectId/videos/upload`（multipart field `file`，Bearer）。
    - 支持 mp4 / mov / avi / mkv / webm / m4v / wmv / mpeg / mpg；不设前端大小上限，实际受服务器磁盘空间约束。
    - XMLHttpRequest 真实上传进度（百分比 + 已传/总大小）、可取消（`xhr.abort`）；401 行为与全局 client 一致。
    - 状态：待上传 → 上传中（进度条）→ 成功 / 失败 / 已取消；成功后自动刷新视频列表并可进入标注。
    - 失败友好提示：507 磁盘不足有专属文案，其余提取后端 `detail`（403/404/409/422 附带补充说明）。
    - 201 返回 Video；不兼容格式后端可能返回 `status = "needs_transcode"` —— 卡片明确标注「已上传，待转码，当前浏览器可能无法播放」，不提供进入标注，仅可查看元数据。
  - 搜索 + 视频工作流状态筛选；筛选字段严格为 `workflow_status`，不是媒体 `status` 或单条 `Annotation.review_status`。列表卡片显示时长 / 帧率 / 分辨率 / 媒体状态徽标与审核工作流状态（`workflow_status` + 修订号 + 提交/通过时间）。
  - **批次 4**：approved 卡片额外显示一行「片段生成概要」（就绪 x/y / 处理中 / 失败），仅挂载时拉取一次，不做每卡高频轮询；详情进入审核 / 标注页查看。
  - **审核入口**：owner / admin / reviewer 角色显示「✓ 审核工作台」入口（角色来自 `listProjects`）。
  - **开发用**：页面底部折叠区可录入 Mock 视频元数据（不经过真实上传，仅本地调试，不抢主操作）。
- **标注工作台** `/projects/:projectId/annotate/:videoId`：
  - 视频流播放（Bearer 认证，blob 拉取）；无文件时空态提示。
  - OverlayLayer 按当前帧显示 YOLO 检测框与修正后 track ID，并可切换关键点和骨架；叠加坐标随播放器缩放映射。
  - 点击检测框或 track ID 列表选择参与对象，按对象数量范围保存到行为标注 `mouse_ids`；没有 detection import 时仍可创建 `needs_mouse_ids` 草稿，前端省略 `mouse_ids` 和检测结果导入/track 修正修订，导入后补选，补齐前不能提交审核或进入正式导出。
  - track 修正模式支持 Split、Merge 和“忽略整个 track”。整轨 suppression 通过 `GET .../detection-suppressions` 加载当前 active import 未撤销项，刷新后仍可撤销；旧 import 项不展示且撤销返回 409。Split/Merge 撤销仍限当前页面会话；跨三类操作按时间统一撤销尚未实现。`mouse_ids` 是语义与目标种类无关的历史兼容字段名。整轨忽略属于检测抑制，原始检测保持不可变；当前不提供单框创建能力，历史 `scope=detection` 仅兼容。
  - 行为类别按 `group` 动态分组展示（绝不硬编码类别），类别颜色仅用于区分。
  - 选择类别后按 **S** / 按钮设起点，**D** / 按钮设终点并 POST 保存。
  - **Space** 播放/暂停，**←/→** 步进一帧（输入框聚焦时不触发）。
  - 时间轴按 duration 显示彩色标注区间，点击 / 键盘可跳转。
  - 行为标注列表支持 PATCH 类别/时间、DELETE 删除。
  - 导出 GET `.../annotations/export` 并下载完整 ExportEvent JSON，包含 `annotation_id`、`mouse_ids`、检测结果导入/track 修正修订和 `clip_file`；没有 ready Clip 时 `clip_file` 为 `null`。
  - 保存中 / 已保存 / 失败状态提示。
  - **批次 3 审核工作流**：顶部清晰显示视频工作流状态与行为标注版本（草稿 / 待审核 / 已通过 / 已退回，即 `draft/submitted/approved/rejected`，以及提交/通过时间）；`draft/rejected` 可「提交审核」（至少一条标注、有 detection import、无 `needs_mouse_ids`，有确认），`submitted` 显示「等待审核」，`approved` 显示「已通过」。单条行为标注的 `review_status` 是独立的 `pending/approved/rejected`，不得与视频工作流混用。标注 CRUD 的非草稿失效与 track 修正的细粒度失效范围不同：track 修正后全部 Annotation 被重校验，有效项推进修订，无效项 `needs_mouse_ids`；仅实际受影响的 approved 单条标注改 pending，视频仅在 submitted/approved 时退回 draft，不声称全部审核状态重置。
  - **批次 4 媒体状态**：approved 视频在侧栏只读展示「媒体片段生成」面板（总数/就绪/处理中/待处理/失败、aria 进度条、最近任务状态；无生成 / 重试按钮），仅在任务进行中轮询，任务落定或离开页面即停止。
- **审核工作台** `/projects/:projectId/review`：
  - 待审队列（仅元数据，避免一次加载全部视频的标注与流）；选中后按需加载标注 / 类别 / 审核历史 / 视频流。
  - 共享播放器 + 时间轴 + 只读标注列表；审核历史（结果、意见、修订号、审核人、时间）。
  - 意见输入 + 「通过 / 退回」（退回须填意见，均有确认）。通过后不再写「后续功能提供」：展示「审核已通过，片段生成已排队 / 处理中」，详情中的媒体状态面板开始轮询 `media-status` 直至任务落定；生成失败时显示错误摘要并提供「重试生成」，任务状态文案区分排队 / 处理中 / 已完成 / 失败 / 已取消，失败绝不误称完成。
  - 媒体面板仅 approved 显示统计，非 approved 显示「审核通过后将自动开始生成片段」；刷新 / 离开页面 / 切换视频即停止轮询；401 / 403 沿用全局 client 处理。
  - 键盘可用：Space 播放/暂停、←/→ 步进一帧（输入框聚焦时不触发）。
  - 审核时只读展示 `mouse_ids`、修正后叠加层及 annotation/detection import/identity 三类修订，避免审核旧身份结果。
- **片段库** `/projects/:projectId/clips`（**批次 5**，owner / admin / annotator / reviewer 均可见）：
  - 数据来自 `GET /api/projects/:pid/clips`（分页 + 类别/视频筛选 + 关键词搜索）与 `GET /api/projects/:pid/clips/categories`（类别计数 chips）；库内仅含「标注 approved 且视频 approved」的有效片段。
  - **类别计数 chips 筛选**（全部 + 各类别计数，颜色来自类别 API）+ **搜索框**（按文件名 / 类别名，服务端过滤，300ms 防抖，输入限长 128 与后端一致）+ **视频选择器**；分页默认 20 条/页（可切 50 / 100），筛选 / 搜索变化自动回到第 1 页，页码超出实际页数时自动回落。
  - **顶部共享预览区**：点击片段后按需拉取该视频源 blob（带 Bearer，与视频流同一封装），跳转到片段 `start_time`，播放范围限制在 `[start_time, end_time]`（到点自动暂停并提示）；一次只播放一个，切换片段撤销上一个 object URL，绝不批量预加载视频。范围条高亮片段区间，点击 / 键盘（←/→）自由跳转。
  - **「跳转到标注」**回 `/projects/:pid/annotate/:vid?t=start`，标注工作台读取 `?t=` 自动定位播放头。
  - 片段卡片：缩略图（`thumbnail_path` 非空时经 `/thumbnails/{name}` 以 Bearer 拉取 blob，失败 / 为空回退 SVG 占位，深色与透明背景均可读）、类别颜色、视频文件名、起止时间、时长、审核状态徽标、标注者、片段生成状态 chip（由 `clip_path` 推断：已生成 / 待生成）。
  - **轮询**：仅当当前页存在「待生成」片段时每 5s 静默刷新（不闪 loading），任务落定或离开页面 / 切换筛选即停止。
  - 空态（暂无片段 / 筛选无结果）、加载、错误态齐全；卡片为按钮（Enter / Space 选择预览），全部控件原生可聚焦。
- **导出** `/projects/:projectId/export`（**批次 6**，owner / admin 可见）：
  - **统计摘要**：可导出总数（审核通过行为标注）/ 就绪视频片段 / 缺失视频片段 + 就绪进度条（aria progressbar）；缺失明细显示类别、视频文件名和标注 ID。导出页面本身只创建并监控后台任务，不同步调用 ffmpeg；项目导出 worker 会在打包前尝试补生成缺失的已通过视频片段，任一补生成失败则不发布 ZIP。
  - **导出范围**：按类别多选 chips（复用 `GET .../clips/categories` 计数 + `GET .../categories` 取色；「全部」= 不传 `category_ids`）；「开始导出 ZIP」按钮仅 owner / admin 可见，非导出角色显示角色提示。
  - **导出内容预览**：待生成目录结构树（集中 `annotations.json`、`clips/<group>/<category>/...`、修正后 track 结果/manifest）及字段摘要；ZIP 行为事件展示 `annotation_id`、`mouse_ids`、三类审核修订和安全非空的必填 `clip_file`。修正后 track 结果保留 `0..frame_count-1` 全部帧，空帧为 `detection_count=0`、`detections=[]`，可重新导入。预览不声称 ffmpeg 已执行。
  - **导出任务**：`POST /api/projects/:pid/export`（body `{category_ids?:number[]}`）发起后轮询 `GET /api/projects/:pid/export/status`（与媒体面板同规则：仅任务进行中每 4s 轮询，落定 / 离开页面即停止）；处理中显示 Job 进度与状态（排队 / 处理中 / 已完成 / 失败 / 已取消），成功提供「下载导出 ZIP」+ 7 天保留提醒（`expires_at` 存在时显示具体保留截止时间），409 冲突提示「上一个导出仍在进行中」。
  - **下载**：`GET /api/projects/:pid/export/download` 与视频流同理用带 Bearer 的请求拉取 blob，文件名以 Content-Disposition 为准（缺失时回退 `project-{pid}-export.zip`），下载时提示文件名与有效期。
  - 1366×768 双列布局（统计 / 范围 / 任务 | 内容预览），窄屏自动堆叠为单列。
- **项目内导航**：视频库 / 片段库 / 审核 / 导出，按项目角色显示合理入口（owner/admin/reviewer 可见审核，owner/admin 可见导出，片段库全员可见）。
- **鉴权**：ProtectedRoute 路由守卫；任一 API 返回 401 自动清除登录态并回到登录页。

## 技术要点

- 无额外状态管理库与 UI 框架，仅 React 内置能力。
- API 封装与类型集中在 `src/api/`（`client.ts` 统一 fetch + Bearer + 401 处理 + 友好错误补充，`types.ts` 与后端 Pydantic schema 对齐，含审核工作流字段、Review、Job / MediaStatus 类型与任务状态文案；批次 4/5/6 字段以后端最终实现为准，核对时仅在 `types.ts` 修正）。片段列表过滤与分页参数类型化为 `ClipListParams`，仅发送已声明的查询参数；导出（批次 6）新增 `ExportRequestInput` / `MissingClip` / `ExportStatus` 类型、`createExport` / `getExportStatus` / `fetchExportDownload`（Bearer blob + Content-Disposition 文件名解析，与视频流同一模式）。
- 导出页面 `src/pages/ExportPage.tsx`：统计摘要 + 类别多选范围 + 待生成目录树 / annotations.json 字段预览 + 任务轮询（仅导出中轮询，组件卸载即清理）与下载（blob object URL 延迟回收）。
- 片段库页面 `src/pages/ClipsPage.tsx`：预览区加载源视频复用 `fetchVideoStreamUrl`，缩略图经 `fetchClipThumbnailUrl`（Bearer blob，失败回退占位）；轮询仅在有「待生成」片段时进行，组件卸载即清理。
- 媒体状态面板 `src/components/MediaStatusPanel.tsx`：完整面板（审核 / 标注工作台共用，`retryable` 控制是否可重试）与行内概要（视频库卡片，一次性拉取）；轮询仅在有未完成任务时进行，组件卸载即清理。
- 文件上传走 `client.ts` 的 `uploadFile`（XMLHttpRequest，支持进度/取消/507 文案），页面不散落上传逻辑。
- 认证上下文在 `src/auth/`（AuthContext / ProtectedRoute / storage / 401 事件）。
- 确认对话框：`src/components/ConfirmDialog.tsx` 的 `useConfirm()`（键盘可达，Esc / 遮罩取消，焦点归还）。
- 时间轴：共享组件 `src/components/Timeline.tsx`（标注 / 审核共用，含键盘 ←/→）。
- 深浅中性色 + 紧凑桌面布局，窄屏自动堆叠；类别颜色只用于行为区分。
- 上传面板与卡片元数据折叠区均使用原生可键盘操作元素（button / details / summary）。

## 快速开始

```bash
cd frontend
npm install

# 按需覆盖后端地址（默认 http://localhost:8000/api）
copy .env.example .env

# 开发（默认 http://localhost:5173，已在后端 CORS 白名单内）
npm run dev

# 生产构建
npm run build
npm run preview
```

当前验证：`npm run build` 通过；本地前端页面 HTTP 200。三文件导入批次的真实长视频、浏览器完整操作、真实 ffmpeg 和生产部署仍待人工验收。

## 目录结构

```
frontend/
├── index.html
├── package.json / package-lock.json
├── vite.config.ts
├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
├── .env.example / .gitignore
├── README.md
└── src/
    ├── main.tsx / App.tsx / vite-env.d.ts
 ├── api/ # client.ts（fetch + XHR 上传封装）+ index.ts（接口）+ types.ts（类型）
 ├── auth/ # AuthContext / ProtectedRoute / storage / 401 事件
 ├── components/ # AppLayout（顶栏 + 项目导航）/ ui.tsx（徽标、空态、卡片等）/ VideoUploadPanel / Timeline / ConfirmDialog / MediaStatusPanel
 ├── pages/ # LoginPage / ProjectsPage / VideosPage / AnnotatePage / ReviewPage / ClipsPage / ExportPage
    ├── styles/global.css
    └── utils/format.ts # 时间/帧/文件大小格式化
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_API_BASE` | `http://localhost:8000/api` | 后端 API 根地址 |
