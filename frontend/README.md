# 标注网站前端（frontend）

多小鼠社会行为标注网站的前端实现：Vite + React 18 + TypeScript + React Router。
对应后端 `../backend`（FastAPI，接口见 `../backend/README.md`）。

## 功能

- **登录** `/login`：demo/demo123 开发账号提示、表单校验、错误态；登录后 token 与用户信息存入 localStorage。
- **项目列表** `/projects`：展示当前用户成员项目及项目内角色，支持创建项目（自动初始化 12 类行为类别）。
- **视频库** `/projects/:projectId/videos`：搜索 + 状态筛选；以 JSON 创建 Mock 视频元数据（filename / duration / fps / width / height / storage_path / status）。
- **标注工作台** `/projects/:projectId/annotate/:videoId`：
  - 视频流播放（Bearer 认证，blob 拉取）；无文件时空态提示。
  - OverlayLayer 透明叠加层（P1 空层，预留 YOLO 框/空间标注）。
  - 行为类别按 `group` 动态分组展示（绝不硬编码类别），类别颜色仅用于区分。
  - 选择类别后按 **S** / 按钮设起点，**D** / 按钮设终点并 POST 保存。
  - **Space** 播放/暂停，**←/→** 步进一帧（输入框聚焦时不触发）。
  - 时间轴按 duration 显示彩色标注区间，点击可跳转。
  - 标注段列表支持 PATCH 类别/时间、DELETE 删除。
  - 导出 GET `.../annotations/export` 并下载统一事件 JSON。
  - 保存中 / 已保存 / 失败状态提示。
- **鉴权**：ProtectedRoute 路由守卫；任一 API 返回 401 自动清除登录态并回到登录页。

## 技术要点

- 无额外状态管理库与 UI 框架，仅 React 内置能力。
- API 封装与类型集中在 `src/api/`（`client.ts` 统一 fetch + Bearer + 401 处理，`types.ts` 与后端 Pydantic schema 对齐）。
- 认证上下文在 `src/auth/`（AuthContext / ProtectedRoute / storage / 401 事件）。
- 深浅中性色 + 紧凑桌面布局，窄屏自动堆叠；类别颜色只用于行为区分。

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
    ├── api/            # client.ts（fetch 封装）+ index.ts（接口）+ types.ts（类型）
    ├── auth/           # AuthContext / ProtectedRoute / storage / 401 事件
    ├── components/     # AppLayout / ui.tsx（徽标、空态、卡片等）
    ├── pages/          # LoginPage / ProjectsPage / VideosPage / AnnotatePage
    ├── styles/global.css
    └── utils/format.ts # 时间/帧格式化
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_API_BASE` | `http://localhost:8000/api` | 后端 API 根地址 |
