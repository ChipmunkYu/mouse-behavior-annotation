# 标注网站前端（frontend）

多小鼠社会行为标注网站的前端实现：Vite + React 18 + TypeScript + React Router。
对应后端 `../backend`（FastAPI，接口见 `../backend/README.md`）。

支持两种运行模式：

| 模式 | 命令 | 数据来源 |
|---|---|---|
| 真实模式（默认） | `npm run dev` / `npm run build` | 后端 FastAPI（`VITE_API_BASE`） |
| **演示模式** | `npm run demo` / `npm run build:demo` | 本地模拟数据（`src/demo/**`，无需后端） |

## 演示模式（Demo Mode）

`npm run demo` 读取 `.env.demo`（`VITE_DEMO_MODE=true`），前端完全离线运行，适合向师兄展示完整流程：
不请求后端、不执行真实 ffmpeg、不产生真实文件。

- 登录：**demo / demo123**（页面顶部与登录页均有「演示模式」标识）。
- 数据层集中在 `src/demo/**`：`fixtures.ts`（种子数据）+ `store.ts`（localStorage 持久化、导出任务进度模拟）+ `api.ts`（与真实 API 同签名的模拟实现）。页面不散落 if/fixture。
- 种子数据包含：1 个项目（owner = 当前用户 demo）、北医 12 类行为、覆盖 draft / submitted / approved / rejected 的 6 个视频、跨视频标注、审核历史、审核通过片段、1 个进行中 + 1 个已完成的导出任务。
- 顶栏常驻「演示模式」徽标与**「重置演示数据」**按钮（清空 localStorage 并重新种子）。
- 标注 / 审核工作台无真实视频时，用 Canvas 绘制「顶视小鼠演示画面」占位（时间轴、S/D 标注、Space/←→ 均可交互），画面上明确标注「演示画面」。
- 上传视频为本地模拟进度 / 取消 / 成功；导出任务为本地定时器推进的进度 / 完成 / 失败。
- 片段库缩略图为 SVG 占位（data URI，不引入任何二进制资源）。

## 功能

- **登录** `/login`：demo/demo123 账号提示、表单校验、错误态；登录后 token 与用户信息存入 localStorage。
- **项目列表** `/projects`：展示当前用户成员项目及项目内角色，支持创建项目（自动初始化 12 类行为类别）。
- **视频库** `/projects/:projectId/videos`：
  - **上传视频**（主操作）：真实模式走 `POST /api/projects/:projectId/videos/upload`（multipart field `file`，Bearer，XHR 进度 + 取消 + 507 文案）；演示模式为本地模拟进度 / 取消 / 成功。
  - 搜索 + 状态筛选；卡片显示时长 / 帧率 / 分辨率 / 状态徽标与审核工作流状态（`workflow_status` + 修订号 + 提交/通过时间）。
  - **审核入口**：owner / admin / reviewer 角色显示「✓ 审核工作台」入口。
  - **开发用**：页面底部折叠区可录入 Mock 视频元数据（仅本地调试）。
- **标注工作台** `/projects/:projectId/annotate/:videoId`：
  - 真实模式：视频流播放（Bearer blob 拉取）；演示模式：Canvas「顶视小鼠演示画面」占位播放器。
  - 行为类别按 `group` 动态分组（绝不硬编码类别），类别颜色仅用于区分。
  - 选择类别后按 **S** 设起点、**D** 设终点保存；**Space** 播放/暂停，**←/→** 步进一帧。
  - 时间轴显示彩色标注区间，点击 / 键盘可跳转；标注段列表支持编辑 / 删除。
  - 导出 GET `.../annotations/export` 并下载统一事件 JSON。
  - **批次 3 审核工作流**：顶部清晰显示工作流状态与修订号；draft / rejected 可「提交审核」（至少一条标注），submitted 显示「等待审核」，approved 显示「已通过」；对已锁定视频修改标注前有明确确认（退回草稿、审核失效）。
- **审核工作台** `/projects/:projectId/review`：
  - 待审队列（仅元数据）；选中后按需加载标注 / 类别 / 审核历史 / 播放（演示模式为共享演示画面）。
  - 共享播放器 + 时间轴 + 只读标注列表；审核历史（结果、意见、修订号、审核人、时间）。
  - 意见输入 + 「通过 / 退回」（退回须填意见，均有确认）。通过文案仅说明审核已通过，不声称片段已生成。
- **片段库** `/projects/:projectId/clips`（演示模式）：
  - 由「审核通过标注」派生的跨视频行为片段：类别计数 chips、类别 / 源视频 / 搜索筛选、分页。
  - 缩略图为 SVG 占位；点击卡片在顶部**共享预览区**播放（一次仅渲染一个，不批量加载视频），可「跳到源标注位置」（带 `?t=` 参数）。
  - 后端接口尚未接入：真实模式下访问会提示「演示功能」。
- **导出中心** `/projects/:projectId/export`（演示模式）：
  - 选择范围（全部已通过片段 / 按行为类别），预览将生成的目录结构与 annotations.json 摘要。
  - 模拟后台任务：进度 → 完成 / 失败（约 7 成成功）；任务保留 **7 天**提示。
  - 「下载演示包」明确禁用（不声称真实 ffmpeg 已执行）。后端尚未接入。
- **项目管理** `/projects/:projectId/admin`（演示模式）：
  - 成员与项目内角色（增 / 删 / 改）、视频标注任务分配、行为类别列表与启停、存储概览。
  - 明确标注「均为演示交互，后端接口尚未接入」；不含快捷键配置。
- **项目内导航**：视频库 / 审核 / 片段库 / 导出 / 项目管理，按项目角色显示合理入口（owner/admin/reviewer 可见审核；owner/admin 可见导出与项目管理）。
- **鉴权**：ProtectedRoute 路由守卫；任一 API 返回 401 自动清除登录态并回到登录页。

## 技术要点

- 无额外状态管理库与 UI 框架，仅 React 内置能力。
- API 封装与类型集中在 `src/api/`（`client.ts` 统一 fetch + Bearer + 401 处理，`types.ts` 与后端 Pydantic schema 对齐）。
- **演示模式分发**：`src/api/index.ts` 顶部读取 `DEMO_MODE`，各函数签名不变、内部转发到 `src/demo/api.ts`；演示独有接口（片段 / 导出 / 管理）在真实模式下返回明确「演示功能」提示。
- 演示数据层：`src/demo/fixtures.ts`（种子）→ `src/demo/store.ts`（localStorage + 派生片段 + 导出任务进度模拟）→ `src/demo/api.ts`（模拟 API）。
- 演示播放器：`src/demo/DemoMouseStage.tsx`（Canvas 顶视小鼠示意画面，标注 / 审核 / 片段预览共用）。
- 确认对话框：`src/components/ConfirmDialog.tsx` 的 `useConfirm()`。
- 时间轴：共享组件 `src/components/Timeline.tsx`（标注 / 审核共用，含键盘 ←/→）。
- 深浅中性色 + 紧凑桌面布局（1366×768 标注工作台固定高度不滚动），窄屏自动堆叠；类别颜色只用于行为区分。

## 快速开始

```bash
cd frontend
npm install

# 真实模式（默认连后端 http://localhost:8000/api）
npm run dev

# 演示模式（无需后端，离线数据）
npm run demo
# → http://localhost:5173 ，登录 demo / demo123

# 生产构建
npm run build          # 输出 dist/
npm run build:demo     # 演示模式构建，输出 dist-demo/
npm run preview        # 预览 dist/
npm run preview:demo   # 预览 dist-demo/
```

按需覆盖后端地址：复制 `.env.example` 为 `.env` 修改 `VITE_API_BASE`。

## 目录结构

```
frontend/
├── index.html
├── package.json / package-lock.json
├── vite.config.ts          # mode=demo 时输出 dist-demo
├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
├── .env.example / .env.demo / .gitignore
├── README.md
└── src/
    ├── main.tsx / App.tsx / vite-env.d.ts
    ├── api/            # client.ts（fetch + XHR 上传封装）+ index.ts（接口 + demo 分发）+ types.ts（类型）
    ├── auth/           # AuthContext / ProtectedRoute / storage / 401 事件
    ├── components/     # AppLayout（顶栏 + 项目导航 + 演示徽标）/ ui.tsx / VideoUploadPanel / Timeline / ConfirmDialog
    ├── demo/           # 演示模式数据层：mode / fixtures / store / api / DemoMouseStage / types
    ├── pages/          # LoginPage / ProjectsPage / VideosPage / AnnotatePage / ReviewPage / ClipsPage / ExportPage / AdminPage
    ├── styles/global.css
    └── utils/format.ts # 时间/帧/文件大小格式化
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_API_BASE` | `http://localhost:8000/api` | 后端 API 根地址（真实模式） |
| `VITE_DEMO_MODE` | `false` | 演示模式开关；仅 `.env.demo`（`npm run demo`）读取，置 `true` |

> 普通 `npm run dev` / `npm run build` 不读取 `.env.demo`，因此默认仍走真实 API，两个模式互不影响。
