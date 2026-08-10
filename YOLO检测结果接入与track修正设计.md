# YOLO 检测结果接入与 track 修正设计

> 状态：已实现，待真实视频端到端人工验收与合并
> 日期：2026-08-07
> 分支：`feature/spatial-annotation`
> 对应工作项：WI-20260805-22
> 术语基线：`项目术语表.md`

本文冻结 YOLO 检测结果接入、参与对象标注、track 修正、审核和导出的产品与技术契约。实现如需改变本文已确定语义，应先更新需求和本文，再修改代码。

## 1. 范围与正式决策

### 1.1 目标

标注网站在原视频上动态显示 YOLO Pose/Tracker 的检测框、track ID、关键点和骨架。每条正式行为标注除标注区间和行为类别外，必须记录参与对象；标注者可以修复 track ID 的断裂、丢失和交换，并抑制误检。

### 1.2 track ID 范围

- 只使用**修正后的 track ID**，不采用 canonical mouse 或跨视频生物身份层。一条 track 有一个 track ID；ID 只是 track 的标识符。
- 一个修正后 track ID 只在一个视频的一次检测结果导入（`DetectionImport`）修订内有效。
- `Split` / `Merge` 修改的是检测结果的 track 归属；`tracks.corrected.jsonl` 是下游使用的修正后 track 结果。
- 原始视频、`tracks.jsonl`、`metadata.json` 和导入后的 `RawDetection` 保持不可变，用于审计和重建。
- 数据库可以使用稳定内部主键维护引用，但导出的身份仍是整数 `track_id`，不得把内部主键包装成跨视频身份。

### 1.3 不在本阶段范围内

- 跨视频识别同一生物个体；
- 自动推断 actor/target；
- 在网站内训练 YOLO 或执行行为自动识别；
- 修改检测框和关键点坐标。框/关键点人工校正可作为后续独立能力。

## 2. 术语与修订

| 术语 | 含义 |
|---|---|
| 原始 tracker ID | YOLO Tracker 在上传 `tracks.jsonl` 中产生的 `track_id` |
| 修正后 track | 在一个视频和检测结果导入修订内，由原始检测经 `Split`/`Merge` 得到的 track |
| 显示 ID | `CorrectedTrack.display_track_id`；播放器、列表和导出统一显示的整数 ID |
| 检测结果导入修订 | `detection_import_revision`；替换 tracks/metadata 时递增 |
| track 修正修订 | `identity_revision`；`Split`、`Merge`、抑制或撤销后递增 |
| 行为标注修订 | `annotation_revision`；标注区间、类别或内嵌 `mouse_ids` 变化后递增 |
| 媒体修订 | `media_revision`；仅在源视频、标注区间或裁剪区域改变时递增，决定视频片段是否需要重编码 |

所有读取响应返回相关修订号；所有写入携带客户端基于的修订号。基础修订不一致时返回 `409 Conflict`，不得静默覆盖。

## 3. 实施前基线与迁移原则

本节描述实现前的历史基线，不代表 `feature/spatial-annotation` 当前状态。当时 `main` 的 `Annotation` 仅保存视频、行为类别、时间/帧区间、置信度、可选 `crop_region` 和审核状态，单视频 JSON 与项目 ZIP 的 `annotations.json` 尚无参与对象 `mouse_ids`。

v0.6 实施后：

1. 旧 `Annotation` 数据库记录继续保留，不物理丢弃；
2. 旧记录迁移为 `mouse_ids=[]`，`mouse_id_status` 设为 `needs_mouse_ids`；
3. 旧审核结果失效，记录退出正式片段库和项目导出；
4. 标注者补齐有效参与对象后重新提交审核；
5. 原实体 Clip 如源视频、时间和 `crop_region` 未变，可作为媒体缓存保留，不因只缺 `mouse_ids` 而重新编码。

## 4. 用户流程

1. 上传原始视频；可以同时拖入 `tracks.jsonl` 和 `metadata.json`，也可以稍后补传；
2. 视频校验成功后即可播放和暂存行为区间；
3. tracks 与 metadata 配对校验成功后启用检测叠加、参与对象选择和 track 修正；
4. 标注者标记行为起止区间和类别；
5. 在清晰帧点击检测框或对象列表，形成该事件的 `mouse_ids`；
6. 如 track ID 错误，切换到 track 修正模式执行 `Split`、`Merge`；确认整条 track 均为误检时用“忽略整个 track”处理，属于整轨检测抑制，原始检测保持不可变；
7. 返回行为标注模式，确认 ID 数量符合该行为类别的规则；
8. 保存标注。`mouse_ids` 与起止帧、行为类别等事件字段在同一事务写入 Annotation；
9. 提交审核时，服务端重新校验 `mouse_ids` 及三类语义修订；
10. 审核通过后，事件和修正后 track 进入正式导出。

缺少有效 YOLO 数据时，用户可以播放视频并创建或调整 `needs_mouse_ids` 草稿；前端省略 `mouse_ids`、`detection_import_revision` 和 `identity_revision`，后端保存空列表及 revision 0。此类草稿不能提交审核或进入正式导出；检测结果导入成功后必须补选参与对象。

## 5. 行为标注内嵌参与对象契约

### 5.1 数量约束

行为事件直接保存 `mouse_ids: number[]`。`mouse_ids`、`mouse_id_status` 与 `mouse_count_min/max` 是历史兼容名称，其语义与目标种类无关，分别表示参与对象列表、参与对象状态与对象数量范围。最小训练标注语义为：

```text
(mouse_ids, start_frame, end_frame, behavior)
```

`mouse_ids` 必须去重并按数值升序规范化；顺序不表达 actor/target。行为类别通过数量范围约束列表长度。

| 行为类别 | mouse_count_min | mouse_count_max |
|---|---:|---:|
| 奔跑、行走、静止 | 1 | 1 |
| 一起、接近、追逐、回避、攻击行为、鼻头接触、鼻尾接触 | 2 | 2 |
| 扎堆行为 | 2 | null（无固定上限） |
| 孤立行为 | 1 | 1 |

### 5.2 选择与校验

- 点击检测框或列表项切换 ID 是否位于当前选择集合；保存时把集合规范化为 Annotation 的 `mouse_ids`。
- 界面实时显示当前数量以及类别允许的最小值和最大值；不满足规则时不得提交审核。
- `mouse_ids` 中每个值必须属于标注视频当前有效 DetectionImport 下的活动 CorrectedTrack。
- 允许遮挡和漏检，不要求每个 ID 在行为区间的每一帧都存在；但每个 ID 在该区间内至少有一帧未被抑制的有效检测。
- ID 只覆盖部分区间时，界面显示覆盖帧数和缺口；不满足“至少一帧”时保存或提交返回明确错误。
- `crop_region` 是媒体裁剪区域，与 `mouse_ids` 或检测框引用相互独立。

## 6. 视频库与检测结果导入

### 6.1 上传体验

默认上传区提供三文件导入批次，允许一次拖入：

| 文件角色 | 示例 | 必需时点 |
|---|---|---|
| 原始视频 | `社交-攻击1.mov` | 播放与创建视频必需 |
| 逐帧检测 | `tracks.jsonl` | 提交参与对象标注前必需 |
| 检测元数据 | `metadata.json` | 与 tracks 一起启用身份功能前必需 |

三个文件分别上传、展示进度并独立重试。视频不等待另外两个文件即可进入视频库；只有 tracks 与 metadata 完成配对校验后，检测叠加和参与对象标注才进入 `ready`。

已有视频提供“补传/替换检测结果”入口。替换总是创建新的检测结果导入修订；系统先展示受影响的 track ID 修正、内嵌 `mouse_ids` 和审核数量，不静默迁移旧 track ID。

### 6.2 配对校验

- 识别 `schema_version`；
- 核对 `video_id`、`source_relative`、宽高、FPS 和帧数；
- `frame_index` 为 0-based，帧记录不得重复，范围必须合法；
- 核对 JSONL 帧覆盖、`detection_count`、字段类型和数值范围；
- 框坐标、关键点数量和置信度合法；
- 记录模型、权重 SHA256、tracker 和推理参数；
- 差异必须显示具体字段，不能只返回“校验失败”。

`source_relative` 使用同时兼容 `/` 和 `\` 的 basename 与视频文件名精确匹配。三文件批次首次成功导入时把 metadata 的 FPS、宽、高同步到新视频，并设置 `duration=frame_count/fps`。已有视频替换检测结果时校验 source basename、FPS、宽、高及当前 active import 的 `frame_count`；替换预览（`confirm=false`）和任一失败路径均清理候选文件，只有 `confirm=true` 且数据库事务成功才保留新 tracks/metadata。

视频库的“状态筛选”按视频 `workflow_status` 执行，不使用媒体 `status`，也不聚合单条 `Annotation.review_status`。

### 6.3 已验证样本

样本位于 `行为识别/data/yolo-track-samples/社交-攻击1/`：

- 156 帧，`frame_index` 连续覆盖 0～155；
- 1877 个检测，全部带原始 tracker ID；
- 平均每帧约 12 个检测，但共有 33 个 tracker ID；
- 11 个 ID 只出现 1 帧，9 个 ID 存在内部中断。

该事实说明 Split/Merge 是正式数据生产流程的一部分，而不是可选调试工具。

## 7. 播放器与参与对象界面

### 7.1 叠加层

- 检测框：默认开启；
- 修正后 ID：默认开启；
- 关键点：默认关闭；
- 骨架：默认关闭，与关键点开关独立；
- 低置信度点可以按阈值隐藏，但不删除原始数据；
- 当前选择和事件中已保存的 `mouse_ids` 使用不同视觉状态。

前端按帧块请求当前及相邻区间，并使用有限窗口缓存。查询失败时不使用上一帧框冒充当前帧；当前帧无检测、超出覆盖范围和请求失败必须分别提示。

视频源坐标映射必须考虑播放器缩放和黑边。帧索引以 metadata 和 JSONL 时间戳为准；恒定帧率视频可用 `floor(currentTime × fps)` 作为初始映射，并限制到合法范围。

### 7.2 重叠框

点击命中多个框时，界面提供候选列表或循环切换，不固定选择最上层框。命中测试使用源视频坐标。

### 7.3 track 修正模式

行为标注和 track 修正为互斥模式。进入 track 修正模式后，行为编辑和参与对象列表保存暂停生效，但播放、暂停和逐帧仍可用。

右侧对象列表提供：

```text
[当前帧 12] [全部 33] [搜索 ID /]
```

- 默认只列当前帧可见 ID；
- 已选 ID 固定在顶部，即使播放后暂时不可见；
- “全部”返回 ID 摘要，不全量加载逐帧检测；
- `/` 聚焦搜索，`Enter` 选择，`Esc` 关闭；输入框聚焦时不拦截快捷键；
- 视频框与列表共享同一组多选状态；
- 0 个选择时禁用 Split/Merge；1 个选择时只启用 Split；2 个及以上时只启用 Merge。

## 8. 正式 track 修正规则

### 8.1 `Split`

在当前帧 `F` 单选一个修正后 track：

- 帧 `< F` 的有效检测保持原显示 ID；
- 帧 `>= F` 的有效检测分配给新 `CorrectedTrack`；
- 新显示 ID 为当前有效最大 `display_track_id + 1`；
- 框、关键点、置信度和原始 detection ID 不变；
- 只有 `F` 前后都至少存在一条该 track 的有效检测时才允许操作。

确认框显示原 ID、Split 帧、新 ID、影响检测数和受影响标注。Split 后默认选择新 ID。

事件 ID 处理：完全位于 `F` 前且仍满足覆盖的 Annotation 可继续保留原 ID；`mouse_ids` 包含原 ID 且行为区间涉及 `F` 或位于 `F` 后的事件进入 `needs_mouse_ids`，不得猜测应保留原 ID还是改为新 ID。

### 8.2 `Merge`

多选两个及以上修正后 track，作用于其当前修订下的全部有效检测：

1. 首次出现帧最早的显示 ID作为保留 ID；
2. 首次出现帧相同则保留数值较小者；
3. 其他 track 的检测映射到保留 `CorrectedTrack`；
4. 被并入实体保留 `merged_into_id` 和审计历史；
5. 所有受影响 Annotation 的 `mouse_ids` 在同一事务把被合并 ID 替换为保留 ID并去重。

若所选 track 在同一帧同时有未抑制检测，Merge 会使一个 ID 同帧对应多个框，服务端必须阻止并返回冲突帧。用户应检查 track 归属：整条 track 均为误检时忽略整个 track；否则通过 Split 隔离不重叠片段后只 Merge 无冲突片段。若仍无法得到一帧一框的有效 track，则保持分离并修正上游检测结果后创建新的检测结果导入修订，不得强行 Merge。

若替换和去重后 `mouse_ids` 数量不符合行为类别规则，系统将该事件置为 `needs_mouse_ids`、使审核失效并要求补选；不得静默保留数量非法的事件。

### 8.3 ID 交换

ID 交换不设专用操作：在交换帧分别对两个 ID 执行 Split，再对属于同一对象的交换前后 track 分别执行 Merge。完成后检查交换点前后连续性。

### 8.4 误检的整轨检测抑制

界面必须使用“忽略整个 track”，不得使用“删除”；整轨检测抑制不会物理删除 `RawDetection`，原始检测保持不可变：

- 提交时按当前 `identity_revision` 解析该 `CorrectedTrack` 的全部当前未抑制检测，并冻结具体 detection ID 集合；
- 后续 Split/Merge 不改变历史抑制操作的作用集合；
- 渲染、统计、冲突检查和 corrected export 默认排除被抑制检测；
- 抑制后若某事件的一个 `mouse_id` 在行为区间内不再有任何有效检测，该事件进入 `needs_mouse_ids`；仍有有效检测的受影响事件保留 ID，但审核失效；
- 撤销生成新修订，不删除历史记录。
- `GET .../detection-suppressions` 只返回当前 active import 中尚未撤销的 suppression。页面刷新后可据此恢复整轨忽略的撤销入口；替换导入后旧 import suppression 不再列出，尝试撤销返回 409。
- 当前产品只允许整轨 `corrected_track` scope，不提供单框创建/抑制能力；历史 `scope=detection` 记录仅为数据兼容，不构成当前 UI/API 创建能力。

## 9. 输入数据契约

`tracks.jsonl` 每行对应一帧：

```json
{
 "schema_version": "1.0",
 "video_id": "社交-攻击1",
 "frame_index": 0,
 "timestamp_sec": 0.0,
 "detection_count": 1,
 "detections": [
 {
 "track_id": 1,
 "box_xyxy_px": [100.0, 200.0, 180.0, 310.0],
 "box_xywhn": [0.25, 0.35, 0.04, 0.10],
 "area_n": 0.004,
 "detection_confidence": 0.82,
 "class_id": 0,
 "keypoints": [
 {"x_px": 150.0, "y_px": 210.0, "confidence": 0.99}
 ]
 }
 ]
}
```

`metadata.json` 至少保存 schema version、视频标识、宽高、FPS、声明与处理帧数、模型与权重 SHA256、tracker、关键点名称、骨架边和推理参数。当前导入兼容规范字段 `frame_count`，并兼容真实样本别名 `processed_frames` / `declared_frame_count`；同时接受 `model`、`model_sha256`、`tracker`、`parameters`、`skeleton_edges_0based`（分别映射到内部模型、权重校验和、tracker、推理参数和骨架边字段）。

## 10. 存储模型

### 10.1 `BehaviorCategory`

新增：

- `mouse_count_min INTEGER NOT NULL`；
- `mouse_count_max INTEGER NULL`，`NULL` 表示无固定上限；
- 约束：`mouse_count_min >= 1`，且 max 为空或 `mouse_count_max >= mouse_count_min`。

### 10.2 `DetectionImport`

关键字段：`id`、`video_id`、`revision`、`schema_version`、tracks/metadata 相对路径及 SHA256、模型/tracker 信息、宽高/FPS/帧数、覆盖范围、检测数、状态、错误、创建者和时间。

约束：`UNIQUE(video_id, revision)`；一个视频只有一个 `active` 导入。文件路径必须位于配置的数据根目录内。

### 10.3 `RawDetection`

关键字段：`id`、`detection_import_id`、`frame_index`、帧内序号、`raw_track_id`、框、关键点、检测置信度和 class ID。

约束：`UNIQUE(detection_import_id, frame_index, frame_detection_index)`；导入成功后不可更新或删除。

### 10.4 `CorrectedTrack`

关键字段：内部 `id`、`detection_import_id`、`display_track_id`、首次/末次出现帧、创建 identity revision、`active`、`merged_into_id`。

约束：当前活动 track 满足 `UNIQUE(detection_import_id, display_track_id)`。它只表示修正 YOLO track，不表示真实生物身份。

### 10.5 `CorrectedDetectionAssignment`

保存当前物化视图：`raw_detection_id`、`corrected_track_id`、`identity_revision`。同一有效 RawDetection 在同一修订只能归属于一个 CorrectedTrack。完整历史由 IdentityEdit 重建，物化表用于按帧查询。

### 10.6 `IdentityEdit`

关键字段：`id`、`video_id`、`detection_import_id`、`operation`（split/merge/revert）、`base_identity_revision`、`result_identity_revision`、参数 JSON、影响检测和标注摘要、操作者、时间、被撤销操作。

### 10.7 `DetectionSuppression`

关键字段：`id`、`video_id`、`detection_import_id`、基础/结果 identity revision、`corrected_track` scope、冻结 detection ID 集合或关联表、操作者、时间、撤销关系。

### 10.8 `Annotation`、`Video` 与 `Review`

- Annotation 增加 `mouse_ids JSON NOT NULL`，保存去重、数值升序的修正后整数 ID 数组；不建立独立参与对象表；
- Annotation 同时保存 `detection_import_revision`、`identity_revision` 和 `mouse_id_status`（`needs_mouse_ids`/`valid`）；
- 应用层和数据库可用约束分别保证 JSON 合法、元素为整数、无重复，并符合 BehaviorCategory 的 `mouse_count_min`/`mouse_count_max`；
- Video 或独立 revision 表保存当前 detection import、identity、annotation 和 media revision；对应中文分别为检测结果导入修订、track 修正修订、行为标注修订和媒体修订；
- Review 快照锁定 `annotation_revision`、`detection_import_revision`、`identity_revision`；`media_revision` 单独管理，不属于审核锁定修订；
- Clip 绑定 `media_revision`，不再用所有语义变化共用的粗粒度修订判断是否重编码。

## 11. 正式 API 契约

所有路径均校验项目成员关系、视频归属和对象归属。owner/admin/annotator 可以创建行为和选择 `mouse_ids`；track 修正初始开放给 owner/admin/annotator；reviewer 只读查看并执行审核。后续如需收紧 track 修正权限，通过项目配置实现。

| 方法与路径 | 请求核心字段 | 响应/语义 |
|---|---|---|
| `POST /api/projects/{pid}/video-import-batches` | 文件角色与视频信息 | 创建上传批次和独立文件槽位 |
| `PUT /api/projects/{pid}/video-import-batches/{bid}/files/{role}` | `role=video/tracks/metadata`、文件 | 流式上传，可独立重试 |
| `POST /api/projects/{pid}/video-import-batches/{bid}/complete` | 已上传槽位 | 校验视频；tracks+metadata 齐全时创建 DetectionImport |
| `GET /api/projects/{pid}/video-import-batches/{bid}` | — | 各槽位进度、校验差异、视频和导入状态 |
| `POST /api/projects/{pid}/videos/{vid}/detection-imports` | tracks、metadata、替换确认 | 新建 revision；返回受影响摘要 |
| `GET /api/projects/{pid}/videos/{vid}/detection-imports/current` | — | 当前导入、统计、revision |
| `GET /api/projects/{pid}/videos/{vid}/detections` | `start_frame`、`end_frame` | 有效检测、raw/display ID、框/点、import/identity revision |
| `GET /api/projects/{pid}/videos/{vid}/corrected-tracks` | 当前帧、搜索、分页 | ID 摘要、首次/末次帧、有效检测数、当前帧可见性 |
| `POST /api/projects/{pid}/videos/{vid}/identity-edits/check` | operation、IDs、frame、base revisions | 新/保留 ID、冲突帧、影响检测/标注 |
| `POST /api/projects/{pid}/videos/{vid}/identity-edits` | check 参数及 base revisions | 事务提交，返回新 identity revision；过期返回 409 |
| `POST /api/projects/{pid}/videos/{vid}/identity-edits/{eid}/revert` | base revision | 生成恢复修订，不删除历史 |
| `POST /api/projects/{pid}/videos/{vid}/detection-suppressions` | corrected track scope、track ID、base revision | 冻结整条 track 当前未抑制 detection 集合并生成新修订 |
| `GET /api/projects/{pid}/videos/{vid}/detection-suppressions` | — | 仅列当前 active import 中尚未撤销的整轨 suppression，供刷新后恢复撤销入口 |
| `POST /api/projects/{pid}/videos/{vid}/detection-suppressions/{sid}/revert` | base revision | 恢复检测并生成新修订 |
| `POST/PATCH /api/projects/{pid}/videos/{vid}/annotations[...]` | `mouse_ids`、行为字段、base revisions | ID 列表直接内嵌到 Annotation，与其他事件字段同事务保存 |
| `GET /api/projects/{pid}/videos/{vid}/annotations/export` | — | 完整 ExportEvent 列表；含 `annotation_id`、`mouse_ids`、检测结果导入/track 修正修订等，`clip_file` 可为 `null` |
| `GET /api/projects/{pid}/videos/{vid}/detections/export` | 固定 import/identity revision | 下载 `tracks.corrected.jsonl` 和 manifest |
| `POST /api/projects/{pid}/export` | `category_ids` 等导出范围 | 创建项目 ZIP；集中生成一个 `annotations.json`，每条事件写入 `clip_file` |
| `GET /api/projects/{pid}/export/download` | 最近成功导出 | 下载固定修订快照的项目 ZIP |

Annotation 写入格式：

```json
{
 "mouse_ids": [8, 20],
 "start_frame": 310,
 "end_frame": 371,
 "category_id": 5,
 "detection_import_revision": 1,
 "identity_revision": 7,
 "annotation_revision": 4
}
```

服务端不能只信任客户端提交的整数：必须把每个 `mouse_id` 在指定 DetectionImport 中解析到活动 CorrectedTrack，并重新执行数量范围、去重、归属和区间覆盖校验；保存前统一按数值升序规范化。

## 12. 行为事件导出契约

行为事件 JSON 与修正后 track 结果是两个不同契约。行为事件引用修正后 track ID；修正后 track 结果提供逐帧框、点和同一 track ID 的完整结果。独立标注 JSON API 与 ZIP `annotations.json` 均返回完整 ExportEvent 字段；独立 API 的 `clip_file` 可为 `null`，ZIP 中则强制为安全非空路径并与实际 MP4 一一对应。

`annotations.json` 是**本次导出包的集中片段索引**，而不是单个视频或单个片段固定附带的 sidecar 文件：

- 一个 ZIP 根目录只生成一个 `annotations.json`，顶层为 JSON 数组；
- 数组中一条事件记录对应 ZIP 中一个实际导出的 MP4；
- `clip_file` 是必填的 ZIP 内相对路径，直接定位对应 MP4；
- 项目级导出包含本次筛选范围内跨视频的全部片段，通过 `video_id` 区分来源；
- 单视频导出只包含该视频本次导出的全部片段；
- 不为每个片段重复生成独立 JSON；
- 必须保证双向一一对应：每条 `clip_file` 存在且唯一，每个导出 MP4 恰有一条事件记录。

```json
{
 "annotation_id": 123,
 "video_id": "video_01",
 "clip_file": "clips/社交行为/攻击行为/clip_123.mp4",
 "start_time": 12.4,
 "end_time": 14.84,
 "start_frame": 310,
 "end_frame": 371,
 "behavior": "attack",
 "mouse_ids": [8, 20],
 "detection_import_revision": 1,
 "identity_revision": 7,
 "crop_region": null,
 "confidence": "certain",
 "annotator": "annotator_01",
 "reviewer": "reviewer_01",
 "review_status": "approved"
}
```

项目 ZIP 至少包含：

```text
annotations.json
clips/<分组>/<类别>/*.mp4
corrected_tracks/manifest.json
corrected_tracks/video_<id>/import_<revision>/identity_<revision>/tracks.corrected.jsonl
```

`tracks.corrected.jsonl` 按帧组织并严格写出 `0..frame_count-1` 的每一帧；没有未忽略有效检测的帧写 `detection_count=0`、`detections=[]`。manifest 中的 metadata 与该 JSONL 契约匹配，修正后结果可以 round-trip 作为新的检测结果导入。

`manifest.json` 记录视频 ID、文件路径、schema version、DetectionImport revision、identity revision、源文件 SHA256 和 corrected 文件 SHA256。导出任务固定快照；生成期间任一相关修订变化时，中止发布而不是输出混合版本。

`clip_file` 由打包阶段根据最终 ZIP 目标路径生成，不从用户输入或数据库中的任意路径直接复制。统一使用 `/` 作为分隔符，禁止绝对路径、`..`、反斜杠逃逸和指向 ZIP 外部的路径。若文件名因冲突追加后缀，必须先确定最终文件名，再写入事件记录。

## 13. 审核、失效与 Clip 生命周期

审核人必须看到行为区间、事件内嵌 `mouse_ids`、修正后检测框和三类语义修订。审核记录锁定：

```text
annotation_revision
detection_import_revision
identity_revision
```

以上分别是行为标注修订、检测结果导入修订和 track 修正修订。媒体修订 `media_revision` 独立决定视频片段/缩略图重编码，不在审核锁定的三类修订中。

视频工作流 `workflow_status` 使用草稿/待审核/已通过/已退回（`draft/submitted/approved/rejected`）；单条行为标注 `Annotation.review_status` 独立使用 `pending/approved/rejected`。提交视频会把本修订标注置为 `pending`，审核裁决再置为 `approved` 或 `rejected`，不得把两套状态混写。

Split、Merge、对应撤销、整轨检测抑制及其撤销后，服务端按新 track 修正修订重校验视频内全部 Annotation：仍合法的项更新 `detection_import_revision`/`identity_revision` 并保持 `valid`，不合法项进入 `needs_mouse_ids`。仅实际受影响且原为 `approved` 的单条 Annotation 改为 `pending` 并清 reviewer；视频仅在当前为 `submitted/approved` 时退回 `draft`，当前为 `rejected` 时不笼统改回 `draft`。旧审核记录保留审计。

当前页面会话内可按实际完成顺序统一撤销具有可靠操作 ID 的 Split、Merge 或整轨忽略；刷新后不恢复统一历史。整轨 suppression 可从持久 active 列表恢复记录旁的单独撤销入口，旧 import 项不进入该列表。

| 变化 | 重新审核 | 更新事件/corrected 导出 | 重编码 Clip/缩略图 |
|---|:---:|:---:|:---:|
| 起止时间/帧 | 是 | 是 | 是 |
| `crop_region` | 是 | 是 | 是 |
| 源视频内容 | 是 | 是 | 是 |
| 行为类别 | 是 | 是 | 否 |
| `mouse_ids` | 是 | 是 | 否 |
| Split/Merge | 受影响标注是 | 是 | 否 |
| 整轨检测抑制 | 受影响标注是 | 是 | 否，除非未来媒体显式烧录检测层 |

实现已拆分语义修订与 `media_revision`：仅 `mouse_ids` 或 track 修正变化不会触发像素未变化 Clip 的无谓 ffmpeg 重编码。

## 14. 替换检测结果导入

替换 tracks/metadata 时：

1. 新建 DetectionImport revision，不覆盖旧文件和旧 RawDetection；
2. 新导入独立建立 CorrectedTrack 初始视图；
3. 旧 IdentityEdit、Suppression 和 Annotation 历史中的 `mouse_ids` 保留在旧 revision 供审计；
4. 不按相同整数 track ID 静默迁移，因为新 tracker 结果中的同号 ID 不保证是同一对象；
5. 全部相关 Annotation 进入 `needs_mouse_ids`；原为 approved 的单条标注失效，视频仅在 submitted/approved 时退回 draft，并退出正式导出；
6. 用户在新导入上重新确认并保存 `mouse_ids` 后再提交。

## 15. 并发、事务与审计

- Split、Merge 和整轨检测抑制提交在事务中重新检查基础检测导入/track 修正修订；
- 行为字段与完整 `mouse_ids` 列表同事务保存；
- Merge、事件 `mouse_ids` 替换、失效标记和修订递增处于同一事务；
- check 结果只用于确认展示，不能代替提交时校验；
- 导出固定 annotation/import/identity/media 快照，发布前再次校验；
- 每次操作记录操作者、时间、原因、基础/结果修订、影响检测、影响 track 和影响标注；
- 撤销通过新操作恢复，不删除历史；
- 原始文件、RawDetection 和已发布审计记录不可原地改写。

## 16. 实施阶段

1. ✅ **迁移与模型**：类别 ID 数量范围、DetectionImport、RawDetection、CorrectedTrack、物化映射、编辑/抑制、Annotation 内嵌 `mouse_ids`、四类修订；
2. ✅ **导入与查询**：视频独立上传、tracks/metadata 补传、配对校验、按帧读取和 ID 摘要；
3. ✅ **只读叠加**：框、ID、关键点、骨架、重叠命中和缓存；
4. ✅ **事件 ID**：视频框/列表同步多选、数量范围提示、内嵌保存和提交校验；
5. ✅ **track 修正**：Split、Merge、冲突检查、整轨检测抑制、撤销、历史和引用迁移；
6. ✅ **审核与媒体修订**：三类审核修订快照、受影响审核失效、语义/媒体修订拆分、旧标注迁移；
7. ✅ **导出**：新版集中 `annotations.json`、`clip_file`、`tracks.corrected.jsonl`、manifest 和项目 ZIP 固定快照，并校验事件与 MP4 双向一一对应；
8. ⏳ **人工验收与部署**：真实长视频、浏览器完整流程、真实 ffmpeg 和生产部署仍待验证；自动化测试已完成。

当前自动化证据：后端全量 `298 passed, 3 skipped, 1 warning`；前端 `npm run build` 通过。

## 17. 验收标准

- 视频可单独上传播放，缺少 YOLO 时不能提交正式参与对象标注；
- 三文件配对错误能指出具体差异，失败结构化文件可独立重试；
- 当前帧框、显示 ID、选择列表和事件 `mouse_ids` 一致；
- Split 严格从当前帧起生成最大 ID+1，Merge 严格保留首次出现最早的 ID；
- 同帧双框冲突会阻止 Merge，抑制后可重新校验；
- 个体、双鼠、扎堆和孤立类别分别执行正确数量约束；
- 遮挡允许，但 `mouse_ids` 中每个 ID 在事件区间至少有一帧有效检测；
- 并发过期写入返回 409，不能覆盖他人修正；
- Split/Merge 后事件 ID 替换或 `needs_mouse_ids` 状态符合本文规则；
- Split/Merge/撤销/suppression/撤销后全部 Annotation 均按新修订重校验，有效项推进修订，无效项进入 `needs_mouse_ids`；
- 页面刷新后可恢复当前 active import 的整轨 suppression 撤销入口，旧 import suppression 撤销返回 409；Split/Merge 撤销和统一按时间撤销边界与本文一致；
- corrected export 保留所有帧及空帧，并可 round-trip 重新导入；
- 身份或 `mouse_ids` 变化使审核和导出失效，但不会重编码像素未变化的 Clip；
- 替换 YOLO 不复用旧同号 ID，旧标注必须重新确认 `mouse_ids`；
- 项目 ZIP 中事件、修正后 track 结果、manifest 和 Clip 属于同一固定修订快照；
- `annotations.json` 对本次导出的所有 MP4 建立完整且唯一的 `clip_file` 索引，单视频、多视频、类别筛选和重名文件场景均通过测试；
- 从原始文件和审计操作可以重建任一已导出的修正结果。

## 18. 剩余验收与已知边界

原“待修改事项”（`clip_file` 生成与路径安全、集中索引、事件/MP4 双向校验、前端预览、后端测试及迁移说明）均已完成。当前仅剩：

1. 使用正式原始视频 `社交-攻击1.mov` 完成长视频、浏览器完整流程与真实 ffmpeg 端到端人工验收；已上传的 `社交-攻击1_all_ids.mp4` 是烧录调试视频，不作为正式输入；
2. 完成生产部署验收，并在人工验收后决定提交、推送及合并；
3. 非阻塞中风险仍包括真实长视频下的浏览器缓存/渲染性能、多人并发压力及单进程媒体任务边界，需在验收和部署阶段确认。
