# 视频标注工具 — 需求说明与进度管理

> **外部 / 历史参考，非当前项目规范**：本文描述另一套 Azure Blob 视频标注工具，其术语、角色、数据格式、版本和工作流均不对当前多小鼠标注网站具有约束力。当前项目的权威术语与工作流以根目录 `项目术语表.md`、`需求文档.md` 及当前设计文档为准；不得据本文推断当前实现。

## 1. 项目概述

基于 Web 的视频逐帧标注工具。标注员可对视频片段标记起始帧（start）、结束帧（end）及对应 instruction，标注数据以 JSON 格式实时保存至 Azure Blob。管理员负责任务分配、进度监控、标注 review 及快照版本管理。

---

## 2. 数据源

### 2.1 存储位置

- **Azure Blob Storage**
  - Account: `eaia2ddata.blob.core.windows.net`
  - Container: `result`
  - 认证方式: **Managed Identity**（当前 VM 已绑定）

### 2.2 原始数据目录结构

```
result/                                          ← 容器
├── 20260410_cogact_supermarket/                  ← 第一级：数据集（一组录制数据）
│   ├── 14_18_A2D0015AC00547_164/                ← 第二级：子任务（≈3150 个文件）
│   │   ├── 14_18_A2D0015AC00547_164.mp4         ← 待标注视频（≈4-5MB，I 帧密集）
│   │   ├── .complete                            ← 处理完成标记
│   │   ├── aligned_data/                        ← 对齐后的传感器数据（机械臂、末端等）
│   │   └── images/                              ← 逐帧图片（训练输入）
│   │       ├── hand_left_color/  (1042 张)
│   │       ├── hand_right_color/ (1042 张)
│   │       └── head_color/       (1042 张)
│   ├── 14_18_A2D0015AC00547_165/
│   │   └── ... (同上结构)
│   └── ... (共 15 个子目录)
├── 20260313_ruicheng_force/
│   └── ...
└── ... (共 11 个数据集)
```

- 每个数据集下有若干子目录，每个子目录内恰好有一个同名 `.mp4` 文件用于标注。
- 每个子目录约 3150 个文件（1 MP4 + 3 相机 × ~1042 帧图片 + 传感器数据）。
- MP4 为高密度 I 帧编码，支持快速逐帧 seek。
- `images/` 目录下的逐帧图片是后续训练的输入。
- 原始数据目录保持纯净，不存放标注工具产生的任何文件。

### 2.3 Blob 访问效率要点

- **必须使用 `walk_blobs(name_starts_with=..., delimiter="/")` 做分层遍历**，避免 `list_blobs` 扫描全量文件导致的性能问题。
- 按需逐级加载：先列第一级数据集目录 → 再列某个数据集下的子目录 → 再找到对应 MP4。

---

## 3. 角色与权限

### 3.1 角色类型

| 角色 | 说明 |
|------|------|
| **管理员（admin）** | 管理用户、分配/转移任务、review 标注、查看进度、创建快照 |
| **标注员（annotator）** | 执行标注，仅能看到分配给自己的任务 |

### 3.2 权限系统（简化版）

- **无登录流程**，用户输入一个字符串作为用户名即可进入系统。
- 系统根据该字符串查找已注册用户列表，验证身份并确定角色。
- 未注册的字符串无法进入系统。

### 3.3 用户管理

- 管理员可创建新用户：指定用户名字符串 + 角色（管理员/标注员）。
- 至少存在一个初始管理员账号（`eaiadmin`）。

---

## 4. 管理员功能

### 4.1 查看数据集

- 展示 `result` 容器下的所有第一级数据集目录。
- 点击某个数据集后，列出其下所有子目录及对应 MP4 文件数量。

### 4.2 任务分配

- 选择一个数据集，查看其中的 MP4 列表。
- 将 MP4 分配给已有标注员（支持手动选择、均分等分配方式）。
- 一个 MP4 分配给一个标注员（不重复）。

### 4.3 任务转移

- 管理员可将已分配给某标注员的任务，转移给另一个标注员。
- 使用场景：标注员 A 无法完成任务，需将其部分或全部任务转交给 B。
- 转移后，原标注员看不到该任务，新标注员可看到并继续标注。
- 如果该任务已有部分标注数据，转移时保留已有标注（新标注员可在此基础上继续）。

### 4.4 查看标注进度

- 查看每个数据集的整体标注进度（已标注 / 总数）。
- 查看每个标注员的任务完成情况。

### 4.5 标注 Review

- 管理员可查看所有已完成的标注结果。
- Review 界面与标注员界面基本一致（视频播放 + 标注可视化）。
- 管理员可直接修改标注（调整 start/end、修改 instruction）。
- 支持快速在不同标注任务之间切换浏览。

### 4.6 预设 Instruction 管理

- 管理员可预先添加常用 instruction 文本。
- 标注时，标注员可从预设列表中快速选择。

### 4.7 数据集标注快照

- 管理员点击"生成标注快照"按钮。
- 系统将 `_drafts/<数据集名>/` 下所有 `.annotation.json` 复制一份到 `_snapshots/<数据集名>/<时间戳>/` 目录。
- 以当前时间戳（`yyyyMMdd_HHmmss`）命名。
- **快照即为标注的正式版本**：训练时从快照目录取最新时间戳的标注文件，而非从草稿取。

---

## 5. 标注员功能

### 5.1 任务列表（左侧面板）

- 显示分配给当前用户的所有任务。
- 按数据集分组，数据集名作为标题，下面列出该用户需标注的 MP4。
- 每条任务显示标注状态（未标注 / 已标注）。
- 点击某条 MP4 即在右侧加载标注界面。

### 5.2 标注界面（右侧面板）

#### 视频播放区

- 视频播放器，加载当前 MP4。
- 支持逐帧前进/后退（参考 `seek.html` 的实现方式）。
- 播放/暂停控制。
- **倍速播放**：支持 0.25x / 0.5x / 1x / 2x 等倍速切换。

#### 进度条/时间线

- 视频下方显示一个可视化进度条。
- 已标注的片段以色块形式在进度条上显示（start → end 区域高亮）。
- 可点击进度条任意位置跳转。
- **点击某个标注色块**：跳转到该片段起始位置，并仅播放该 start→end 区间（片段回放），方便快速检查标注质量。

#### 标注操作区

- **设置起始帧**按钮：将当前帧设为标注的 start。
- **设置结束帧**按钮：将当前帧设为标注的 end。
- **Instruction 输入**：文本输入框，用于描述该片段的 instruction。
- **预设 Instruction 下拉**：可快速选择管理员预设的 instruction。
- 支持对同一视频添加**多段标注**。

#### 保存

- **Ctrl+S** 快捷键实时保存当前标注至 Azure Blob。
- 保存后刷新页面不丢失数据。

---

## 6. 标注数据格式

每个 MP4 对应一个 JSON 文件，结构如下：

```json
{
  "video_path": "20260410_cogact_supermarket/14_18_A2D0015AC00547_164/14_18_A2D0015AC00547_164.mp4",
  "video_tags": ["废弃"],
  "annotations": [
    {
      "start": 12,
      "end": 36,
      "instruction": "拿起货架上的牛奶",
      "segment_tags": ["grasp", "red_object"]
    },
    {
      "start": 50,
      "end": 78,
      "instruction": "将牛奶放入购物车",
      "segment_tags": ["place"]
    }
  ]
}
```

- `start` / `end`：帧编号（整数）。
- `instruction`：文本描述。
- `video_tags`：视频级别标签数组（可为空/缺失，标记整段视频的状态，如"废弃""画面模糊"）。
- `segment_tags`：片段级别标签数组（可为空，针对单条标注片段，由管理员 per-dataset 配置）。
- 每次 Ctrl+S 覆盖写入该 JSON 文件。

---

## 7. 存储方案

### 7.1 设计原则

- `result` 容器下现有的都是数据集目录（日期命名开头）。
- 系统元数据使用 `_` 前缀目录，与数据集目录天然区分。
- 代码中列数据集时只需过滤掉 `_` 前缀目录即可。
- **原始数据目录保持纯净**：标注工具产生的所有文件（草稿、快照、元数据）全部存放在 `_` 前缀目录下，不污染数据集目录。

### 7.2 标注 JSON 工作副本 — `_drafts/` 目录

**路径**: `result/_drafts/<数据集名>/<子目录名>.annotation.json`

```
result/_drafts/
└── 20260410_cogact_supermarket/
    ├── 14_18_A2D0015AC00547_164.annotation.json
    ├── 14_18_A2D0015AC00547_165.annotation.json
    └── ...
```

- 标注员每次 Ctrl+S 直接写入此文件，这是标注的**工作草稿**，实时更新。
- 与原始数据目录分离，避免下载数据的人误将草稿当作最终标注。
- 任务转移时，草稿不动，新标注员可在已有草稿基础上继续。

### 7.3 系统元数据 — `_meta/` 目录

**路径**: `result/_meta/`

```
result/_meta/
├── users.json              ← 用户列表（用户名、角色）
├── assignments.json        ← 任务分配（数据集 → 子任务 → 标注员）
├── archived_datasets.json  ← 已归档数据集列表
├── browser_access.json     ← 浏览器访问权限（数据集 → 可浏览的标注员列表）
├── instruction_presets.json ← 全局预设 instruction（fallback）
├── presets/                 ← per-dataset 预设 instruction
│   └── <数据集名>.json
└── tags/                    ← per-dataset 标注 tags
    └── <数据集名>.json     ← {"segment_tags": [...], "video_tags": [...]}
```

### 7.4 标注快照 — `_snapshots/` 目录（正式版本）

**路径**: `result/_snapshots/<数据集名>/<时间戳>/`

```
result/_snapshots/
└── 20260410_cogact_supermarket/
    ├── 20260412_143000/
    │   ├── 14_18_A2D0015AC00547_164.annotation.json
    │   ├── 14_18_A2D0015AC00547_165.annotation.json
    │   └── ...
    └── 20260413_091500/
        └── ...
```

- 管理员 review 后点击"生成快照"，系统将 `_drafts/<数据集名>/` 下所有 `.annotation.json` 复制到此处。
- **快照即为标注的正式版本**。

下游训练 pipeline 的取数逻辑：
1. 进入 `_snapshots/<数据集名>/`。
2. 取最新时间戳的子目录。
3. 从中读取各 `.annotation.json`，再结合原始数据子目录下的 `images/` 和 `aligned_data/` 进行训练。

不打 zip，直接平铺 JSON 文件，便于 diff 比对和单独读取。

### 7.5 完整存储目录全览

```
result/                                          ← 容器
│
├── _meta/                                       ← 系统元数据
│   ├── users.json
│   ├── assignments.json
│   ├── archived_datasets.json
│   ├── browser_access.json
│   ├── instruction_presets.json
│   ├── presets/
│   │   └── <数据集名>.json
│   └── tags/
│       └── <数据集名>.json                     ← {"segment_tags": [...], "video_tags": [...]}
│
├── _drafts/                                     ← 标注工作草稿
│   └── <数据集名>/
│       └── <子目录名>.annotation.json
│
├── _snapshots/                                  ← 标注快照（正式版本）
│   └── <数据集名>/
│       └── <yyyyMMdd_HHmmss>/
│           └── <子目录名>.annotation.json
│
├── 20260410_cogact_supermarket/                  ← 数据集（纯净，不含标注文件）
│   ├── 14_18_A2D0015AC00547_164/
│   │   ├── 14_18_A2D0015AC00547_164.mp4
│   │   ├── .complete
│   │   ├── images/
│   │   └── aligned_data/
│   └── ...
├── 20260313_ruicheng_force/
│   └── ...
└── ...
```

**列数据集时的过滤规则**：遍历第一级目录，跳过 `_` 前缀的目录（`_meta/`、`_drafts/`、`_snapshots/`），剩余即为数据集。

---

## 8. 技术方案

| 层 | 技术选型 |
|----|----------|
| 前端 | 单页 HTML + JavaScript |
| 后端 | Python FastAPI + uvicorn（`--reload` 开发模式） |
| 存储 | Azure Blob Storage（Managed Identity 认证） |
| Blob SDK | `azure-storage-blob` + `azure-identity` (DefaultAzureCredential) |
| 部署 | 当前 VM 直接运行，手动启动 uvicorn |

前端通过后端 API 获取视频流和数据，后端负责与 Azure Blob 交互。

### 8.1 视频播放架构（Phase 1 关键决策）

#### 问题背景

标注工具的核心体验要求：
- 逐帧 seek 必须丝滑（←/→ 箭头键）
- 4x 倍速播放不卡顿
- 点击进度条任意跳转即时响应
- 切换视频时前一个视频的所有网络活动必须立即终止

视频参数：1920×368，30fps，H.264，关键帧间隔 15 帧，单文件 55-73MB。

#### 尝试过的方案与问题

| 方案 | 问题 |
|------|------|
| 后端代理 Blob 流式转发（StreamingResponse） | 每次 seek 都走 Blob 网络请求，延迟高 |
| SAS URL 让浏览器直连 Blob | 仍然每次 seek 走 Azure 网络 |
| 后端本地缓存 + FileResponse Range | `async def` 路由中调同步函数阻塞事件循环，整个服务卡死 |
| 后端本地缓存 + StaticFiles Range 流式播放 | 低带宽（VPN ~0.7MB/s）下浏览器缓冲跟不上播放速度，4x 直接卡死 |
| Range 流式播放 + 后台下载切换 Blob URL | 两者竞争 HTTP 连接导致 stalled |
| 并行分块下载（6 路） | VPN 总带宽瓶颈，无加速效果 |

#### 最终方案：全量下载 + Blob URL（与 seek.html 同原理）

```
点击视频 → POST /ensure（后端确认本地缓存）
        → fetch 全量下载到浏览器内存（带进度条）
        → URL.createObjectURL(blob) → 赋给 <video>
        → 之后所有操作纯内存，零网络
```

**为什么这样做：**
- `seek.html` 验证了本地文件 + `createObjectURL` 可以做到完全丝滑的逐帧 seek
- 低带宽环境下流式 Range 请求无法支撑连续播放和快速 seek
- 一次性下载虽然需要等待（~70s @ 0.7MB/s），但下载完后体验与本地文件完全一致
- Loading overlay 显示实时下载进度（百分比 + MB），用户知道在等什么

**关键实现细节：**
- `AbortController` 管理每个 clip 的生命周期，切换视频时立即 abort 所有 fetch + 停止 `<video>` + 释放 Blob URL
- `video.play().catch(() => {})` 吞掉快速 play/pause 产生的 AbortError
- 后端路由使用 `def`（非 `async def`），让同步的 Azure SDK 调用跑在线程池中，不阻塞事件循环
- 视频缓存在后端本地磁盘（`.cache/videos/`），首次从 Blob 下载时自动 `ffmpeg -movflags +faststart` 处理 moov atom
- 视频生成 pipeline（`pipeline_processor.py`）也已加入 `-movflags +faststart`，新生成的视频 moov atom 直接在文件头

### 8.2 后端启动方式

开发阶段手动启动，可直接在终端看日志：

```bash
cd /home/eai/DataHub-G1/annotation_tool
uvicorn app:app --host 0.0.0.0 --port 8080 --log-level info --reload
```

`--reload` 监听 `annotation_tool/` 目录下的 `.py` 文件变化自动重启。静态文件（HTML/JS/CSS）变更不需要重启后端，浏览器 `Ctrl+Shift+R` 强刷即可。

已配置 systemd user service（`~/.config/systemd/user/annotation-tool.service`）用于生产部署，目前 disabled。

### 8.3 视频本地缓存

| 配置 | 值 |
|------|-----|
| 缓存目录 | `annotation_tool/.cache/videos/` |
| 目录结构 | `<dataset>/<clip>.mp4` |
| 过期策略 | 3 天未访问（基于 mtime）自动清理 |
| faststart | 下载后用 ffmpeg 将 moov atom 移至文件头 |

后端启动时自动清理过期缓存。

---

## 9. 实现计划

按以下顺序逐步实现，每个阶段交付一个可用的增量：

### Phase 1：能看到视频、能逐帧操作

**目标**：搭建最小可运行骨架，打通 Blob → 后端 → 前端视频播放的完整链路。

| 步骤 | 内容 |
|------|------|
| 1.1 | 后端项目结构搭建（FastAPI），Azure Blob 连接（Managed Identity） |
| 1.2 | API：列出数据集（第一级目录）、列出子目录、获取 MP4 文件流 |
| 1.3 | 前端页面骨架：左右分栏布局 |
| 1.4 | 左侧：数据集列表 → 点击展开子目录 → 点击加载视频 |
| 1.5 | 右侧：视频播放器 + 逐帧前进/后退 + 倍速播放 |

**交付物**：打开页面能浏览数据集、选择视频、逐帧查看。

### Phase 2：能标注、能保存

**目标**：实现核心标注功能，标注数据可持久化。

| 步骤 | 内容 |
|------|------|
| 2.1 | 标注操作区：设置 start、设置 end、instruction 输入框 |
| 2.2 | 支持同一视频多段标注（标注列表管理：增加/删除/切换） |
| 2.3 | 进度条可视化：标注色块显示 |
| 2.4 | 点击标注色块 → 跳转并播放该片段（片段回放） |
| 2.5 | API：保存标注 JSON 到 `_drafts/` / 读取已有草稿 |
| 2.6 | Ctrl+S 保存 + 切换视频时自动加载已有标注 |

**交付物**：标注员可以完成完整的标注工作流。

### Phase 3：用户系统 + 任务分配

**目标**：多人协作，管理员可分配任务。

| 步骤 | 内容 |
|------|------|
| 3.1 | 用户验证页（输入用户名字符串进入系统） |
| 3.2 | `_meta/users.json` 初始化（内置 admin 账号） |
| 3.3 | 管理员界面：用户管理（创建用户、指定角色） |
| 3.4 | 管理员界面：任务分配（选择数据集 → 选择标注员 → 均分/手动分配） |
| 3.5 | 标注员界面：只显示分配给自己的任务 |
| 3.6 | 任务转移：管理员可将任务从 A 转移给 B（保留已有标注） |

**交付物**：多人可各自登录使用，任务互不干扰。

### Phase 4：管理员 Review + 进度监控

**目标**：管理员可查看和修正标注、监控整体进度。

| 步骤 | 内容 |
|------|------|
| 4.1 | 管理员 review 界面（复用标注界面，支持编辑） |
| 4.2 | 管理员可浏览所有任务的标注结果，快速切换 |
| 4.3 | 标注进度看板：数据集整体进度、各标注员完成情况 |
| 4.4 | 预设 Instruction 管理（管理员添加，标注员下拉选择） |

**交付物**：管理员可完成质量把控和进度管理。

### Phase 5：快照 + 标注浏览器 + 交互优化

**目标**：版本管理、标注浏览搜索、交互体验打磨。

| 步骤 | 内容 |
|------|------|
| 5.1 | 标注快照功能：一键生成带时间戳的快照到 `_snapshots/` |
| 5.2 | 快照列表查看、快照元数据统计（总标注数、unique instruction、segment 长度统计） |
| 5.3 | 标注浏览器（Annotation Browser）：独立视图，多数据集关键词搜索 + 分页 + MP4 预览卡片 |
| 5.4 | 浏览器访问控制：管理员配置哪些标注员可以浏览哪些数据集 |
| 5.5 | 数据集归档：将不活跃数据集归档，进度看板分开显示 |
| 5.6 | 静态文件缓存禁用中间件，确保前端代码更新即时生效 |

**交付物**：具备版本管理能力，标注浏览搜索，交互效率达到生产可用水平。

### Phase 6：两层 Tag 系统 + Tag 命名重构

**目标**：支持视频级别标签（标记整段视频状态）和片段级别标签（标记单条标注属性），两套独立配置。命名无歧义：Video Tags / Segment Tags。

| 步骤 | 内容 |
|------|------|
| 6.1 | 后端 `load_tags`/`save_tags` 支持 `video_tags` 字段 |
| 6.2 | 管理员 Tag Presets 卡片拆分为两个 textarea（Video tags / Segment tags） |
| 6.3 | 标注界面新增 Video Tags 行（橙色按钮，位于标注区顶部） |
| 6.4 | Video tag 切换自动触发保存（视频级别状态即改即存） |
| 6.5 | 浏览器搜索结果中显示 video_tags badges |
| 6.6 | Tag key 重命名：`tags` → `segment_tags`，`clip_tags` → `video_tags`（后端 + 前端 + 已有 blob 数据迁移） |

**交付物**：标注员可对整段视频打标签（如「废弃」「画面模糊」），与 segment 级别标签互不干扰。

---

## 10. 进度追踪

| Phase | 内容 | 状态 |
|-------|------|------|
| 需求确认 | 本文档确认 | ✅ 完成 |
| Phase 1 | 骨架 + 视频播放 | ✅ 完成 |
| Phase 2 | 标注 + 保存 | ✅ 完成 |
| Phase 3 | 用户 + 任务分配 | ✅ 完成 |
| Phase 4 | Review + 进度 | ✅ 完成 |
| Phase 5 | 快照 + 浏览器 + 优化 | ✅ 完成 |
| Phase 6 | 两层 Tag 系统 + Tag 命名重构 | ✅ 完成 |
| 迭代优化 | Preset 高亮、ID 清理、速度保持 | ✅ 完成 |
| 迭代优化 | 键盘快捷键、撤销重做、片段播放、Preset 折叠 | ✅ 完成 |
| 迭代优化 | Q-mode 快速选择、自定义高亮颜色、管理员色板参考 | ✅ 完成 |
| 迭代优化 | 布局重构、Tag key 重命名迁移、进度 Segment 统计 | ✅ 完成 |
| Phase 7 | CogACT 数据集导出 | ✅ 完成 |

### Phase 1 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/app.py` | FastAPI 后端：数据集浏览 API + 视频缓存触发 + StaticFiles 静态服务 |
| `annotation_tool/blob_service.py` | Azure Blob 封装：列目录、下载缓存、faststart 处理、缓存清理 |
| `annotation_tool/static/index.html` | 前端页面：左右分栏、视频播放器、控制区、缓存状态指示 |
| `annotation_tool/static/app.js` | 前端逻辑：数据集树、全量下载 + Blob URL 播放、逐帧 seek、倍速、AbortController 生命周期管理 |
| `annotation_tool/static/style.css` | 深色主题样式 |
| `src/pipeline_processor.py` | 视频生成 pipeline 增加 `-movflags +faststart` |

### Phase 2 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/blob_service.py` | 新增：标注读写（`load_annotation`/`save_annotation`）、标注计数（`count_annotations_for_dataset`）、预设指令读写（`load_instruction_presets`/`save_instruction_presets`） |
| `annotation_tool/app.py` | 新增：标注 CRUD API（`GET/PUT /api/annotations/{dataset}/{clip}`）、预设 API（`GET/PUT /api/presets`）、clips API 增加 `annotation_counts` 字段 |
| `annotation_tool/static/index.html` | 标注 UI：Mark Start/End 按钮、预设下拉框、instruction 输入框、标注列表、save 状态 |
| `annotation_tool/static/app.js` | 标注 CRUD 逻辑、自动保存、时间轴色块、点击编辑、Ctrl+S、sidebar 计数 badge |
| `annotation_tool/static/style.css` | 标注区域、预设下拉框、时间轴色块、save 状态、sidebar badge 样式 |

### Phase 2 关键决策

#### 标注数据存储

- 存储路径：`_drafts/{dataset}/{clip}.json`（与需求文档 7.2 一致，但文件名简化为 `{clip}.json` 而非 `{clip}.annotation.json`，因为目录已隔离）
- 每条标注包含 `id`（8 位随机字符串）、`start_frame`、`end_frame`、`instruction`
- `save_annotation()` 同时将 annotation count 写入 blob metadata，`count_annotations_for_dataset()` 仅读 metadata 不下载内容，避免展开数据集时逐个下载 JSON 导致卡顿

#### 自动保存策略

最初设计为手动 Ctrl+S 保存，标注员反馈频繁手动保存麻烦。改为：
- **Add** 一条标注 → 自动 save 到 blob
- **Update** 一条标注 → 自动 save 到 blob
- **Delete** 一条标注 → 自动 save 到 blob
- **Ctrl+S** 的行为：如果当前正在编辑某条（instruction 输入框有内容、start/end 已标记），先提交编辑再 save；否则直接 save 当前状态
- 不再有 "Unsaved changes" 状态和切换视频时的未保存提醒

#### 标注交互设计

- **Mark Start (S) / Mark End (D)**：快捷键在视频区域按下标记当前帧，输入框聚焦时 S/D 作为普通字符输入
- **Enter 键**：在 instruction 输入框按 Enter 等同于点击 Add/Update
- **点击标注列表项** → 进入编辑模式（跳转到 start 帧 + 填充 instruction + 按钮变 "Update"）
- **点击时间轴色块** → 同样进入编辑模式
- **时间轴色块颜色**：蓝色（`rgba(52,152,219)`），与红色进度光标区分；播放中变绿色
- 标注色块可点击进入编辑模式，而非播放区间（编辑模式下可通过列表中的 ▶ 按钮播放区间）
- Save 按钮已移除（所有操作自动保存），仅保留 save 状态提示（Saving... / ✓ Saved / Error）

#### 预设指令（Instruction Presets）

- 存储位置：`_meta/instruction_presets.json`（与需求文档 7.3 一致）
- 前端页面加载时 `GET /api/presets` 拉取预设列表，填充下拉框
- 下拉框单独一行（全宽），因为预设文字可能很长
- 选择预设后自动填充到 instruction 输入框，下拉框复位
- 管理员可通过 `PUT /api/presets` 修改预设列表，所有人刷新即可看到更新
- 初始预设 7 条（英文，后续由管理员按需修改）

#### Sidebar 标注计数

- 展开数据集时，每个 clip 名称旁显示标注段数 badge
- 计数从 blob metadata 读取（`annotation_count` 字段），不下载文件内容
- 无标注的 clip badge 显示为灰色 "0"
- Save 成功后增量更新当前 clip 的 badge

#### `crypto.randomUUID()` 不可用

浏览器通过 HTTP（非 HTTPS）访问时，`crypto.randomUUID()` 不可用（要求 secure context）。改用 `Math.random().toString(36).slice(2, 10)` 生成 8 位随机 ID。

### Phase 3 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/blob_service.py` | 新增：用户读写（`load_users`/`save_users`）、任务分配读写（`load_assignments`/`save_assignments`）、默认管理员 `eaiadmin` |
| `annotation_tool/app.py` | 新增：登录 API（`POST /api/login`）、用户 CRUD（`GET/POST/DELETE /api/users`）、任务分配（`GET/PUT /api/assignments`）、clips API 增加 `?user=xxx` 过滤 |
| `annotation_tool/static/index.html` | 三视图结构：登录页 / 管理员面板 / 标注界面；管理员面板含用户管理卡片 + 任务分配卡片（含标注员勾选 + 均分 + 清空 + 保存） |
| `annotation_tool/static/app.js` | 登录流程（sessionStorage 持久化）、视图切换、管理员用户 CRUD、任务分配（标注员选择器 + 均分 + 清空）、标注员过滤（只显示有分配的 datasets） |
| `annotation_tool/static/style.css` | 登录页、顶栏、管理面板卡片、用户列表、分配表格、标注员选择器样式 |

### Phase 3 关键决策

#### 单页视图切换

采用单页 HTML 三视图方案：`#loginView`、`#adminView`、`#annotationView`，同时只显示一个。管理员登录后先进管理面板，可点击 "Enter Annotation View" 进入标注界面（带 "Back to Admin" 按钮）。标注员登录后直接进入标注界面。

#### 用户系统

- **无密码登录**：用户输入用户名字符串，后端验证 `_meta/users.json` 中是否存在该用户
- 登录状态存 `sessionStorage`，刷新页面自动恢复，关闭标签页清除
- 默认管理员账号 `eaiadmin`（首次启动时自动创建 `_meta/users.json`）
- 防止删除最后一个 admin 用户

#### 任务分配

- 分配数据存储：`_meta/assignments.json`，结构为 `{dataset: {clip: username_or_null}}`
- 每个 clip 只能分配给一个标注员
- 管理员在分配表中可逐个下拉选择，或使用 Auto Assign 均分
- **标注员选择器**：Auto Assign 前可勾选参与的标注员子集（如只选 2 人做某个数据集），支持 Select All / Deselect All
- **Clear All Assignments**：一键清空当前数据集的所有分配
- 分配统计摘要：实时显示 "X/Y assigned, Z unassigned"
- 任务转移：管理员直接在下拉框中改选即可，已有标注数据保留在 `_drafts/` 不受影响

#### 标注员过滤

- 标注员进入标注界面后，sidebar 只显示有分配给自己的 datasets（通过 `GET /api/assignments` 前端过滤）
- 展开 dataset 时请求 `GET /api/datasets/{dataset}/clips?user=xxx`，后端只返回分配给该用户的 clips
- 管理员进入标注界面时看到所有 datasets 和 clips，不受过滤影响

### Phase 4 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/blob_service.py` | 新增：per-dataset 预设（`_meta/presets/{dataset}.json`）、tag 读写（`load_tags`/`save_tags`，`_meta/tags/{dataset}.json`） |
| `annotation_tool/app.py` | 新增：progress API（`GET /api/progress`）、tags API（`GET/PUT /api/tags?dataset=xxx`）、presets API 增加 `?dataset=xxx` 查询参数 |
| `annotation_tool/static/index.html` | 管理员面板新增：进度看板卡片、Tag Presets 管理卡片；标注界面新增：tag 多选行 |
| `annotation_tool/static/app.js` | 进度看板（`loadProgress`/`renderProgress`/`enterAnnotationViewForDataset`）、tag 管理（`loadAdminTagDatasets`/`onTagDatasetChange`/`saveAdminTags`）、标注 tag 集成（`loadTags`/`renderTagToggles`/`clearSelectedTags`/`restoreSelectedTags`）、assigned-to 显示 |
| `annotation_tool/static/style.css` | 进度表格 + 进度条样式、tag toggle 按钮样式、tag badge 样式、assigned-to badge 样式、preset textarea 样式 |

### Phase 4 关键决策

#### 进度看板

- **单 API 聚合**：`GET /api/progress` 在一个 `def` 路由中调用 `list_datasets()`、`list_clips()`、`count_annotations_for_dataset()`、`load_assignments()`，聚合为每个数据集的进度统计（total/assigned/annotated + per-annotator breakdown）
- **可展开行**：点击数据集行的展开箭头可查看各标注员的分配/完成情况
- **Review 跳转**：点击数据集名（红色链接）直接跳转到标注界面并自动展开该数据集
- **进度条颜色**：<40% 红色（low）、40-80% 橙色（mid）、>80% 绿色（high）

#### Per-dataset 预设指令

- 从全局 `_meta/instruction_presets.json` 改为 per-dataset `_meta/presets/{dataset}.json`
- 加载顺序：先尝试 per-dataset，不存在时 fallback 到全局
- 管理员面板使用多行 textarea 编辑（每行一条 preset），自动 trim 和过滤空行
- 标注界面在切换 dataset 时自动加载对应 presets

#### 标注 Tags（Segment-level，Phase 4 版本）

- **存储**：per-dataset tag 定义在 `_meta/tags/{dataset}.json`，格式 `{"segment_tags": ["tag1", "tag2"], "video_tags": ["tag1", ...]}`
- **标注数据**：每条 annotation 增加 `segment_tags: ["tag1", "tag2"]` 字段（数组，可为空）
- **管理员配置**：与 presets 同模式的管理卡片（dataset 下拉 + textarea + Save 按钮）
- **标注界面**：instruction 输入行下方显示 tag toggle 按钮（pill-shaped），点击选中/取消
- **编辑恢复**：编辑 annotation 时自动恢复已选 tags
- **列表显示**：annotation list 中每条显示 tag badges（红色小标签）
- **无 tag 时隐藏**：如果 dataset 没有配置 tags，tag 行不显示

#### Assigned-to 显示

- 选择 clip 后，clip 标题旁显示蓝色 "Assigned to: xxx" badge
- 数据来源：`GET /api/assignments/{dataset}`
- 未分配的 clip 不显示 badge
- 获取失败时静默忽略（非关键功能）

### Phase 5 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/blob_service.py` | 新增：快照创建/列表（`create_snapshot`/`list_snapshots`）、MP4 预览生成（`generate_preview`）、标注搜索（`search_annotations`）、快照元数据统计（`get_snapshot_metadata`）、浏览器访问控制（`load_browser_access`/`save_browser_access`/`get_browser_datasets_for_user`）、数据集版本列表（`list_dataset_versions`）、归档数据集管理（`load_archived_datasets`/`save_archived_datasets`） |
| `annotation_tool/app.py` | 新增：快照 API（`GET/POST /api/snapshots/{dataset}`）、搜索 API（`GET /api/search`）、预览 API（`POST /api/preview/{dataset}/{clip}`）、快照元数据 API（`GET /api/snapshot-metadata/{dataset}`）、浏览器访问控制 API（`GET/PUT /api/browser-access`、`GET /api/browser-datasets`）、数据集版本 API（`GET /api/dataset-versions/{dataset}`）、归档 API（`GET/PUT /api/archived-datasets`）、`NoCacheStaticMiddleware` 禁用静态文件缓存 |
| `annotation_tool/static/index.html` | 新增第四视图 `browserView`（多数据集选择 + 版本下拉 + 搜索栏 + 结果区 + 分页）；管理面板新增快照管理卡片、浏览器访问控制卡片、归档进度卡片 |
| `annotation_tool/static/app.js` | 4 视图切换（`showView`）；完整浏览器视图实现（数据集加载、搜索、分页、IntersectionObserver 懒加载预览）；快照管理；浏览器访问配置；数据集归档管理；导航按钮线路（annotation ↔ browser ↔ admin） |
| `annotation_tool/static/style.css` | 浏览器视图样式（搜索栏、预览卡片网格 540px 宽、分页）、快照管理样式、浏览器访问配置样式 |

### Phase 5 关键决策

#### 四视图架构

从三视图（login/admin/annotation）扩展为四视图，新增独立的 Annotation Browser 视图。Browser 是一个独立入口（不是 admin 子页面），管理员和标注员都可以访问（受权限控制）。四个视图通过 `showView(viewId)` 切换，同时只显示一个。

导航路线：
- Admin → "Enter Annotation View" → Annotation View
- Admin → "Annotation Browser" → Browser View
- Annotation View → "Annotation Browser" → Browser View
- Browser View → "Back" → 返回上一个视图
- 各视图都有独立的 Logout 按钮

#### 快照系统

- **创建**：管理员选择 dataset → 点击 "Create Snapshot" → 后端将 `_drafts/{dataset}/` 下所有 JSON 复制到 `_snapshots/{dataset}/{yyyyMMdd_HHmmss}/`
- **列表**：newest first 展示，显示时间戳和文件数
- **元数据统计**：总标注数、unique instruction 列表、segment 长度统计（min/max/avg/median，帧+秒）
- **与训练对接**：下游取 `_snapshots/{dataset}/` 下最新时间戳目录

#### 标注搜索

- **多数据集**：用户勾选要搜索的 datasets，每个 dataset 可选版本（Latest Snapshot / Current Draft / 特定历史快照）
- **关键词匹配**：case-insensitive substring match on instruction text，空关键词返回全部
- **分页**：后端一次性加载所有匹配结果，前端分页（20/50 per page）
- **搜索结果**：每条包含 dataset、clip、instruction、segment_tags、video_tags、start/end frame、segment 帧长度

#### MP4 预览卡片

- **懒加载**：使用 `IntersectionObserver` 监控 `.preview-placeholder`，卡片滚入可视区域时才请求 `POST /api/preview/{dataset}/{clip}` 生成预览
- **生成方式**：ffmpeg 从源视频截取 start→end 片段，480px 宽、5fps、crf30、H.264 faststart
- **缓存**：`.cache/previews/{dataset}/{clip}_{start}_{end}.mp4`，文件名含帧范围，不同片段独立缓存，不会互相覆盖
- **性能**：每个预览 20-50KB，生成耗时 1-2s
- **不自动清理**：preview 文件会一直保留。体积很小，可手动删除 `.cache/previews/` 清理

#### 浏览器访问控制

- **存储**：`_meta/browser_access.json` → `{"access": {"dataset1": ["user1", "user2"], ...}}`
- **Admin 无限制**：管理员可浏览所有数据集
- **标注员受限**：只能浏览 admin 明确授权的数据集
- **管理界面**：admin 面板 "Browser Access" 卡片，选 dataset 后勾选允许的标注员

#### 数据集归档

- **进度看板分离**：管理员可将不活跃数据集标记为 "archived"，进度看板分为活跃和已归档两个区域
- **已归档区域折叠显示**，默认收起
- **归档 ≠ 删除**：归档数据集仍然可以在标注界面和浏览器中访问，只是进度看板中分开显示

#### 静态文件缓存（已移除）

- 曾添加 `NoCacheStaticMiddleware` 对 `.html`、`.js`、`.css` 设置 `Cache-Control: no-cache`，用于解决调试期间浏览器缓存旧 JS 的问题
- 功能稳定后已移除该中间件：禁用缓存在生产环境下无必要，且增加了每次页面加载的开销
- 开发阶段浏览器 F5 刷新即可获取最新代码

#### 视频本地缓存（补充）

| 配置 | 值 |
|------|-----|
| 视频缓存目录 | `annotation_tool/.cache/videos/` |
| 预览缓存目录 | `annotation_tool/.cache/previews/` |
| 视频过期策略 | 3 天未访问自动清理 |
| 预览过期策略 | 无（手动清理） |

### Phase 6 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/blob_service.py` | 改 `load_tags()` 返回 dict `{"segment_tags": [...], "video_tags": [...]}`；改 `save_tags()` 接收 `video_tags` 参数；改 `search_annotations()` 结果包含 `video_tags`/`segment_tags` |
| `annotation_tool/app.py` | 改 `GET/PUT /api/tags` 使用 `segment_tags`/`video_tags` 字段 |
| `annotation_tool/static/index.html` | Admin Tag Presets 卡片拆为两个 textarea（Video tags + Segment tags）；标注界面新增 video tag 行 `#clipTagRow` |
| `annotation_tool/static/app.js` | 新增状态变量 `availableClipTags`/`selectedClipTags`；`loadTags()` 解析新格式；`renderClipTagToggles()` 渲染橙色 video tag 按钮；`loadAnnotations()` 读取 `video_tags`；`saveAnnotations()` 写入 `video_tags`；admin tag 管理支持双 textarea；浏览器搜索结果显示 video_tags badges |
| `annotation_tool/static/style.css` | `.clip-tag-row`/`.clip-tag-check`/`.clip-tag-badge` 样式（橙色系 `#e8a838`，与 segment tag 的红色系区分） |

### Phase 6 关键决策

#### 两层 Tag 数据模型

- **Tag 定义**：`_meta/tags/{dataset}.json` 格式为 `{"segment_tags": ["tag1", ...], "video_tags": ["tag1", ...]}`
- **标注数据**：`_drafts/{dataset}/{clip}.json` 顶层 `"video_tags": ["废弃"]`，每条 annotation 内 `"segment_tags": [...]`
- **Key 命名重构**：原 `tags`/`clip_tags` 改为 `segment_tags`/`video_tags`，消除歧义。已有 blob 数据通过一次性迁移脚本完成重命名。

#### Video Tag vs Segment Tag 交互区分

| 特性 | Video Tag | Segment Tag |
|------|----------|-------------|
| 作用范围 | 整段视频（如"废弃""画面模糊"） | 单条标注片段（如"difficult""slow"） |
| UI 位置 | 标注区顶部独立一行 | 与 Mark Start/End 同行 |
| 颜色主题 | 橙色系（`#e8a838`） | 红色系（`#e74c3c`） |
| 保存时机 | 切换即保存（视频级别状态即改即存） | 随 annotation 一起保存 |
| 存储位置 | annotation JSON 顶层 `video_tags` | 每条 annotation 内 `segment_tags` |

#### Admin 配置

管理员在 "Tag Presets" 卡片中看到两个 textarea：
- "Video tags (applied to entire video, one per line)"
- "Segment tags (applied to individual annotations, one per line)"

两组 tag 独立配置，Save 一次性保存。

### 迭代优化

#### Preset 高亮展示

管理员在 Instruction Presets 中可用 `**词组**` 标记关键词（动作、物品名等），标注界面中自动高亮显示：

- **存储**：`**...**` 标记存储在 preset 文本中，后端原样存取，解析完全在前端
- **解析**：`extractHighlightTerms()` 从所有 presets 提取 `**...**` 内容，case-insensitive 去重
- **配色**：19 色调色板（`HIGHLIGHT_COLOR_MAP`），支持按名称指定颜色
- **手动指定颜色**：`**word|color**` 语法，如 `**Pick up|red**` → "Pick up" 固定红色高亮。`parseHighlightToken()` 用 `lastIndexOf('|')` 分离词和颜色名
- **自动分配**：未指定颜色的词按出现顺序从剩余调色板分配（`buildTermColorMap()` 两步策略：手动色优先，再自动分配）
- **管理员参考**：Admin Preset 编辑区显示颜色名 + 色块参考（`renderPresetColorRef()`），方便管理员选择颜色名
- **渲染**：原 `<select>` 下拉框替换为始终平铺的可点击列表（`.preset-list`），高亮词以彩色 `<span>` 渲染
- **填入**：点击某条 preset → instruction 输入框填入纯文本（`stripHighlightMarkers()` 去除 `**` 和 `|color` 标记）
- **向后兼容**：无 `**` 标记的 presets 正常显示为普通文字

示例 admin 输入：
```
**Pick up|red** the **green potato chip bag|blue** using the right arm.
**Place|red** the held **green potato chip bag|blue** into the felt bag on the table using the right arm.
```
→ `Pick up` 和 `Place` 都是红色，`green potato chip bag` 出现两次都是蓝色。

#### Annotation ID 字段清理

- 每条 annotation 原先存储 `id` 字段（8 位随机字符串），这是前端 CRUD 的内部主键
- 该字段对导出数据无意义，已从存储中移除：
  - **前端**：改用 `_id`（transient，`loadAnnotations()` 加载时临时生成，`saveAnnotations()` 保存时剔除）
  - **后端**：`search_annotations()` 结果移除 `annotation_id` 字段
  - **数据清理**：一次性脚本 `cleanup_annotation_ids.py` 已清理 `_drafts/` 和 `_snapshots/` 中所有 JSON 的 `id` 字段

#### 播放速度保持

- 默认播放速度从 1x 改为 **2x**（标注场景常需快速浏览）
- 切换视频时保持当前速度设置（`video.load()` 会重置 `playbackRate`，加载完成后从 `<select>` 重新应用）

#### 键盘快捷键体系

| 快捷键 | 功能 | 条件 |
|--------|------|------|
| `S` | Mark Start | 焦点不在输入框时 |
| `D` | Mark End | 焦点不在输入框时 |
| `←` / `→` | 逐帧前进/后退 | 焦点不在输入框时 |
| `Space` | 播放/暂停 | 焦点不在输入框时 |
| `Enter` | 提交 annotation | 在 instruction 输入框内 |
| `Q` | 进入 Q-mode 快速选择 | 焦点不在输入框时 |
| `Ctrl+S` | 手动保存 | 全局 |
| `Ctrl+X` | 清空 start/end 标记，退出编辑模式，清除高亮，焦点回到 Play 按钮 | 全局 |
| `Ctrl+Z` | 撤销（最多 50 步） | 全局 |
| `Ctrl+Y` | 重做 | 全局 |

#### 撤销/重做（Undo/Redo）

- **快照模式**：`snapshotState()` 序列化 `{annotations, selectedClipTags}` 为 JSON 字符串
- **触发时机**：每次执行 add/update/delete annotation 或 toggle clip tag 前调用 `pushUndo()`
- **栈深度**：`MAX_UNDO = 50`，超出时丢弃最旧记录
- **互斥**：执行新操作时清空 redo 栈
- **恢复**：`restoreSnapshot()` 反序列化后刷新 UI + 自动保存

#### 片段播放与当前 Instruction 展示

- **点击时间轴色块**：帧精确 seek 到 start 帧（使用 `seeked` 事件回调确保 seek 完成后才 `play()`，避免 I 帧偏移），播放到 end 帧自动停止，同时进入编辑模式
- **播放中显示**：controls 区域下方显示蓝色左边框的当前 instruction 文本（`showSegmentInstruction()`），播放结束或手动操作后清除
- **Hover 提示**：时间轴色块悬停显示 tooltip（instruction 文字），CSS 纯定位实现
- **`editAnnotation(id, opts)`**：增加 `{skipSeek: true}` 选项，时间轴色块点击时同时触发 edit + play 不产生双重 seek 冲突

#### Preset 折叠 + 全局状态

- Preset 列表支持折叠/展开，header 区域可点击，三角箭头 `▶` 旋转动画指示状态
- 折叠状态存储在全局变量 `_presetsExpanded`，**切换 clip/dataset 时保持状态**
- 默认展开（`_presetsExpanded = true`）

#### Q-mode 快速选择

键盘驱动的 preset 快速选择模式，减少鼠标操作：

1. 按 `Q` 进入 Q-mode → Preset header 显示闪烁提示 "Type number + Enter"
2. 输入数字（支持多位数，如 `12`）→ 对应序号的 preset 项高亮
3. 按 `Enter` 确认 → instruction 填入该 preset（去除 `**` 标记），焦点移到 instruction 输入框
4. 按 `Esc` 退出 Q-mode
5. **隔离机制**：Q-mode 激活时所有按键先进 Q-mode 处理器，不触发正常快捷键。仅当焦点不在输入框时才能按 Q 进入

- Preset 列表左侧显示序号 badge（`.preset-index`，22×22px 居中白色文字），方便视觉查找
- 提交 instruction 后（Enter 键），焦点自动回到 Play 按钮，避免后续快捷键被输入框吞掉

#### 标注区域布局重构

将纵向堆叠的标注区域重构为更紧凑的布局：

```
┌─────────────────────────────────────────────────┐
│ Video Tags: [废弃] [模糊]                       │  ← Row 1: 独占一行
├─────────────────────────────────────────────────┤
│ Mark Start(S) F120  Mark End(D) F180            │
│ Segment Tags: [difficult] [slow]                │  ← Row 2: 同行，flex-wrap
├─────────────────────────────────────────────────┤
│ [+Add] [instruction input...............] saved │  ← Row 3: instruction
├───────────────────────────┬─────────────────────┤
│ ▶ Presets (12)            │ Annotations         │  ← Row 4: 并排
│ col1         │ col2       │ #1 F120→F180 ...    │
│ 1 Pick up    │ 7 Place    │ #2 F200→F260 ...    │
│ 2 ...        │ 8 ...      │                     │
│ ...          │ ...        │                     │
├───────────────────────────┴─────────────────────┤
│ shortcuts hint                                  │
└─────────────────────────────────────────────────┘
```

- **Presets**：CSS `columns: 2` 自动分两列，不滚动，`break-inside: avoid` 防止条目跨列断裂
- **Presets vs Annotations**：`flex: 2` / `flex: 1` 比例并排，`align-items: stretch` 高度对齐
- **Annotations**：右侧独立滚动（`overflow-y: auto`），无固定 max-height，跟随 presets 高度

#### 进度看板 Segment 统计

- Annotation Progress 表格新增 **Segments** 列
- Dataset 行显示该数据集所有 clip 的 segment 总数（`total_segments`）
- 展开后 Annotator 行显示各自负责 clip 的 segment 数（`segments`）
- 后端 `GET /api/progress` 利用已有的 `count_annotations_for_dataset()` 求和，无额外 blob 请求

#### 滚动条与布局修复

- `.player-wrapper` 自定义窄滚动条（6px，半透明红色，webkit + Firefox 兼容）
- `.annotation-list` 自定义滚动条（5px，与面板背景融合）
- `.main-panel` padding 移至 `.video-container` 内部，解决滚动条遮挡右侧内容的问题
- 关键容器添加 `flex-shrink: 0` 防止 flex 压缩导致内容消失

### Phase 7 交付清单

| 文件 | 说明 |
|------|------|
| `annotation_tool/app.py` | 新增：export API（`POST /api/export/cogact`、`GET /api/export/cogact/status`、`POST /api/export/cogact/cancel`）；后台线程运行 4 步 pipeline |
| `annotation_tool/static/index.html` | 管理面板新增 CogACT Dataset Export 卡片 |
| `annotation_tool/static/app.js` | Export 交互：数据集/快照选择、启动/取消、轮询状态、步骤指示器、日志滚动 |
| `annotation_tool/static/style.css` | Export 步骤指示器、日志区域、按钮样式 |
| `annotation_tool/README.md` | 新增 export 依赖安装说明、Azure 存储依赖表 |

### Phase 7 关键决策

#### 一键导出流程

管理员在 admin 面板选择 cogact_* 数据集 + snapshot → 后台线程依次执行 4 步：

1. **Export**：`export_cogact_supermarket.py`（~10min，并行 3 episodes）
2. **Statistics**：`get_dataset_statistics.py`（~40s）
3. **LMDB**：`build_lmdb_from_videos.py`（~75s）
4. **Upload**（可选）：azcopy 上传到 `eaidatashare/liluo/dataset/msra_process/`

#### 自动参数推断

不硬编码 dataset 配置。从 blob 子目录名自动解析 `task_id/job_id/device_id`（如 `14_18_A2D0015AC00547_165`），output 目录名从 dataset_name + snapshot 日期自动生成。

#### Azure 存储依赖

| Storage Account | Container | 用途 |
|----------------|-----------|------|
| `eaia2ddata` | `result` | 源数据 + 标注（annotation tool 主存储） |
| `eaia2ddata` | `data` | 原始采集数据（auto pipeline） |
| `eaidatashare` | `liluo` | 共享数据集上传目标 |

三者均通过 VM Managed Identity 认证，无需密钥。

#### 并发控制

全局只允许一个 export 任务运行，使用 `threading.Lock` + 全局状态 dict。子进程使用 `os.setsid()` 创建进程组，Cancel 时 `os.killpg()` 终止整个进程树。
