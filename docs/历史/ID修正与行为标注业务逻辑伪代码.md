# track 修正与行为标注业务逻辑伪代码
> **历史伪代码，非当前权威。** 本文保留旧实现调用链与回滚脉络。`CorrectedTrack`、`CorrectedDetectionAssignment`（CDA）、`IdentityEdit`、`DetectionSuppression` 和 `SuppressionDetection` 仅是迁移兼容背景，不代表当前运行时 authority。当前权威见[现行架构设计](../设计/检测状态、提交审核与独立行为视频片段导出设计.md)。
>
> 历史依据：[旧 YOLO 设计](YOLO检测结果接入与track修正设计.md)。代码中保留的兼容分支不构成当前产品能力。
## 1. 目的、范围与术语
本文覆盖：逐帧检测叠加、行为事件增删改、审核、项目 ZIP 导出，以及 Split、Merge、检测抑制和撤销。
不覆盖：YOLO 训练、跨视频真实身份、框/关键点坐标编辑、自动 actor/target 推断。
| 术语 | 当前业务含义 |
|---|---|
| raw detection（原始检测） | `RawDetection`；来自当前 `DetectionImport`，保存原 tracker ID、帧、框、关键点和置信度。 |
| corrected track / display ID | `CorrectedTrack.display_track_id`；界面、事件 `mouse_ids`、修正后 track 导出使用的整数 ID。 |
| `mouse_ids` | 历史兼容字段名；语义与目标种类无关，表示行为事件参与对象的 track ID 数组，无 actor/target 顺序语义。 |
| `detection_import_revision` | 检测导入修订；替换 tracks/metadata 时递增。 |
| `identity_revision` | 历史兼容字段名；表示 track 修正修订，Split、Merge、抑制、撤销均递增。 |
| `annotation_revision` | 行为语义审核修订；标注写入使非 draft 视频失效时递增。 |
| `media_revision` | 像素产物修订；非 draft 状态下发生时间、帧、裁剪或删除等媒体变化时递增。 |
| suppression（抑制） | 当前产品能力仅为“忽略整个 track”：冻结该 track 当前未抑制 detection 集合，不物理删除 `RawDetection`。 |
| active import | 视频唯一 `active == true` 的 `DetectionImport`。 |
| materialized snapshot | 指定 `identity_revision` 下完整的 `CorrectedDetectionAssignment`（CDA）映射。 |
当前实现说明：本文伪代码、函数名、端点和状态变化均按当前源码；RawDetection/原始文件不可变，语义修订与媒体修订分离，审核与导出锁定修订。单视频 `/annotations/export` 与项目 ZIP 共用 `_event_record` 的完整 ExportEvent 字段，独立 API 的 `clip_file` 可为 `null`，ZIP 中必须安全非空。整轨 suppression 可从 active 列表恢复撤销入口；Split/Merge 撤销仍限当前页面会话；统一按时间撤销仍是计划能力。
## 2. 总体调用链
```text
用户点击按钮/按快捷键
 → React 页面函数（校验本地 state、弹确认框）
 → 更新 busy/saving/error 等 state
 → frontend/src/api/index.ts API wrapper
 → apiFetch/apiRaw 自动加 /api 前缀与认证
 → FastAPI HTTP endpoint
 → backend router endpoint function
 → 权限/视频/活动导入/修订/业务规则校验
 → SQLAlchemy Session 写模型；db.commit() 形成事务边界
 → JSON/204/文件响应
 → wrapper 解析响应或抛 ApiError
 → React 更新 revision/selection/hint
 → loadAnnotations、loadQueue、loadStatus 或 refreshKey++
 → DetectionOverlay 清缓存并重新查询，列表与画面同步
```
## 3. 公共守卫与不变量伪代码
```pseudo
GUARD project_access(project_id):
 验证登录用户是项目成员
 各 router 再按操作检查 role
 视频必须存在且 video.project_id == project_id，否则 404
GUARD active_import(video_id):
 imp = DetectionImport where video_id and active == true
 身份写入无 imp → 400
 检测查询无 imp → 空 detections/tracks；current import 端点 → 404
GUARD identity_write_revision(body, video):
 if body.base_detection_import_revision != video.detection_import_revision: 409
 if body.base_identity_revision != video.identity_revision: 409
 check 只是预览；commit 必须重新执行校验
INVARIANT RawDetection:
 导入后不改 frame/raw_track_id/box/keypoints/confidence，不做物理删除
 Split/Merge 只改新 revision 的 CDA 归属
 suppression 只写 DetectionSuppression + SuppressionDetection
NORMALIZE_AND_VALIDATE mouse_ids:
 canonical = sort(unique(input))
 后端要求 canonical == sort(input)；创建/更新入口先传 canonical
 count 满足 BehaviorCategory.mouse_count_min/max
 每个 ID 对应当前 import 的 active CorrectedTrack
 每个 ID 在 [start_frame, end_frame] 至少有一条当前 revision、未抑制检测
 成功 → mouse_id_status = "valid"，保存 import/identity revision
 失败 → 400；某些 track 修正变化路径则把既有事件标为 "needs_mouse_ids"
INVALIDATE review:
 重校验视频内全部 Annotation：valid/needs_mouse_ids，并把 import/identity revisions 推进到新快照
 实际受影响且 approved 的 Annotation → review_status="pending", reviewer_id=null
 video 当前为 submitted/approved → workflow_status="draft"，清提交/批准字段；rejected 不因此回 draft
 Review 历史行不删除
SEMANTIC_VS_MEDIA revision（当前 annotations.py）:
 if video 已是 draft: _invalidate_video 不递增 annotation/media revision
 else:
 所有实际标注写入 → annotation_revision += 1，回 draft
 时间/帧/crop/delete/create → media_revision += 1，删 Clip 行并计划清实体文件
 仅类别/mouse_ids/revision 字段 → 不增 media_revision，不删 Clip
 Split/Merge/撤销/suppression/撤销只增 identity_revision，不增 annotation/media revision
```
事务异常原则：endpoint 未捕获异常时请求失败，Session 生命周期负责回滚；显式批处理/导出路径调用 `db.rollback()`。
标注的 Clip 实体清理由 DB commit 后执行，删除失败写 cleanup issue，不回滚已提交 DB。
## 4. 检测加载与 Overlay 流程
### 4.1 用户操作 3 步
1. 打开标注页或审核页，视频和 `DetectionOverlay` 挂载。
2. 播放、拖动或逐帧，使 `video.currentTime` 变化。
3. 查看框/ID；标注页点击框可同步切换 `selectedMouseIds`，重叠框连续点击循环候选。
### 4.2 前端调用 6 段
```pseudo
1. useEffect(projectId, videoId, refreshKey):
 clear cache/pending; genRef++
 getCurrentDetectionImport()
2. frame = floor((video.currentTime or currentTime) * fps)
 fps = import.fps || fallbackFps || 30
3. cache miss:
 start=max(0, frame-15); end=frame+15
 getDetections(projectId, videoId, start, end)
 先为 ±15 每帧写空数组，再按 frame_index 分桶
 total >= 500 → 标记截断
4. requestAnimationFrame loop:
 每轮按 currentTime 计算帧；帧变化才 draw()
 ResizeObserver 变化也 draw()
5. draw/hit test:
 geometry() 处理 source 尺寸、等比缩放和黑边 ox/oy
 draw box/ID/keypoints/skeleton
 点击坐标反算到源视频；命中 candidates，重叠时循环
6. onFrameData(frame, detections, import):
 AnnotatePage 写 currentFrame/currentDetections；identityRevision 取 Video.identity_revision
 getCorrectedTracks(current_frame, search, page_size=200)
 当前帧列表、选择 chips、overlay 共用 selectedMouseIds
```
### 4.3 后端事务 4 步（只读查询，无写事务）
```pseudo
1. get_detections 校验 project/video，取 active import 和 video.identity_revision
2. _get_suppressed_detection_ids 计算该 revision 仍生效的 raw IDs
3. join RawDetection → CDA(current revision) → active CorrectedTrack
4. 排除 suppression，按帧/帧内序号排序，limit 500，返回 raw/display ID 与几何数据
```
调用表：
| 前端函数 | API wrapper | HTTP endpoint | 后端函数 | 读取模型 |
|---|---|---|---|---|
| Overlay import effect | `getCurrentDetectionImport` | `GET /api/projects/{pid}/videos/{vid}/detection-imports/current` | `get_current_detection_import` | `Video`、`DetectionImport` |
| Overlay cache effect | `getDetections` | `GET /api/projects/{pid}/videos/{vid}/detections?start_frame=&end_frame=` | `get_detections` | `RawDetection`、CDA、`CorrectedTrack`、suppression models |
| Annotate track effect | `getCorrectedTracks` | `GET /api/projects/{pid}/videos/{vid}/corrected-tracks` | `get_corrected_tracks` | 同上，聚合摘要 |
注意：后端已过滤 suppression，前端不再二次过滤；空帧缓存为空数组，不沿用上一帧框。
## 5. 行为标注操作
### 5.1 创建事件：类别 + 可选 mouse IDs + S/D
**用户操作 5 步**
1. 在“行为标注”模式选择类别。
2. 有检测结果导入时点击检测框或 track 列表，得到如 `[8, 20]` 的有序唯一 ID；没有导入时跳过参与对象选择。
3. 播放头到起点，按 S，触发 `markStart`。
4. 播放头到终点，按 D，触发 `markEnd`。
5. 非 draft 时确认打回草稿；成功后查看事件列表。
**前端调用 4 段**
```pseudo
markStart():
 time = video.currentTime
 frame = timeToFrame(time, video.fps)
 setStartPoint({time, frame})
markEnd():
 require activeCategory and startPoint and end > start
 if detectionImport exists: require mouseIdsValid(category, selectedMouseIds)，提交 mouse_ids/revisions
 else: 省略 mouse_ids/revisions，创建 needs_mouse_ids 草稿
 await guardMutation("新增行为标注")
 await createAnnotation(pid, vid, event fields + 条件展开的 mouse_ids/revisions)
 reset startPoint; await loadAnnotations(); locked 时 await refreshVideo()
```
**后端事务 7 步**
```pseudo
create_annotation:
1. _require_editor(owner/admin/annotator)，校验 video/category/active category/confidence/interval
2. 取 active import；传 mouse_ids 但无 import → 400；未传则允许创建 needs_mouse_ids
3. 传了 mouse_ids 时 canonical=sort(unique(mouse_ids)); _validate_mouse_ids(category/count/track/coverage)
4. _invalidate_video(increment_media=true)：仅非 draft 时增 annotation/media revision 并删 Clip 行
5. INSERT Annotation(review_status="pending")；有导入且 IDs 有效时置 valid/current revisions，否则置 needs_mouse_ids、mouse_ids=[]、import revision=0
6. db.commit(); commit 后 _cleanup_files(plan)
7. db.refresh(annotation)，返回 AnnotationOut
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `markStart` / `markEnd` | `createAnnotation` | `POST /api/projects/{pid}/videos/{vid}/annotations` | `create_annotation`、`_validate_mouse_ids`、`_invalidate_video` | `Annotation`、可能 `Video`/删除 `Clip` |
### 5.2 更新类别、时间、mouse_ids
**用户操作 3 步**
1. 在事件行点击“编辑”，修改类别、起止秒和 ID 文本。
2. 前端把秒转帧，把 ID 去重、排序后提交。
3. 非 draft 时确认失效；保存后列表刷新。
**前端调用 3 段**
```pseudo
AnnotationEditForm.submit:
 validate finite, nonnegative, end > start
 patch.mouse_ids = parse → integer → unique → ascending
 patch 带 ann.detection_import_revision / ann.identity_revision
handleEditSave → guardMutation → updateAnnotation → loadAnnotations/refreshVideo
```
**后端事务 8 步**
```pseudo
update_annotation:
1. editor + video + annotation ownership（本人或 owner/admin）
2. 客户端 revisions 与 annotation 已存快照不一致 → 409
3. 校验新 category/confidence
4. media_changed = 时间/帧/crop 是否出现；_invalidate_video(increment_media=media_changed)
5. 应用 category/time/frame/crop；重新校验 interval
6. 若提供 mouse_ids：按当前 video.identity_revision 完整校验并置 valid、刷新 revisions
7. 若只改 category/frame 且保留旧 IDs：校验失败时不拒绝，改为 needs_mouse_ids
8. db.commit → cleanup → refresh → response
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `AnnotationEditForm.submit` / `handleEditSave` | `updateAnnotation` | `PATCH /api/projects/{pid}/videos/{vid}/annotations/{annotation_id}` | `update_annotation` | `Annotation`、可能 `Video`/`Clip` |
### 5.3 删除事件
**用户操作 2 步**
1. 点击事件“删除”并确认；锁定状态提示会打回草稿并删除旧片段。
2. 成功后事件列表刷新。
**前端调用 2 段**
```pseudo
handleDelete(ann): confirm → deleteAnnotation(pid, vid, ann.id)
then loadAnnotations(); locked 时 refreshVideo()
```
**后端事务 5 步**
```pseudo
delete_annotation:
1. editor、video、annotation ownership guards
2. _invalidate_video(increment_media=true)
3. db.delete(annotation)
4. db.commit
5. _cleanup_files(plan)，返回 204
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `handleDelete` | `deleteAnnotation` | `DELETE /api/projects/{pid}/videos/{vid}/annotations/{annotation_id}` | `delete_annotation` | 删除 `Annotation`，可能更新 `Video`/删除 `Clip` |
### 5.4 提交审核
**用户操作 2 步**
1. 点击“提交审核”，前端先检查有事件、有 detection import、无 `needs_mouse_ids`。
2. 确认后等待视频进入 submitted。
**前端调用 2 段**
```pseudo
handleSubmitReview:
 local guards → confirm → submitVideoForReview(pid, vid)
 setVideo(response); hint="已提交审核"
```
**后端事务 8 步**
```pseudo
submit_video:
1. role in owner/admin/annotator；状态不能 submitted/approved
2. 至少一条 Annotation；detection_import_revision != 0；active import 存在
3. _revalidate_annotations 纯校验数量、active track、区间内未抑制覆盖，只返回 issues 与待同步 revisions，不写 Annotation
4. 任一 invalid/needs_mouse_ids → 400；事务回滚，Annotation 状态和数据库不变
5. 已存 revisions stale 但当前语义有效 → 允许继续，不因 stale 本身返回 400
6. 全部语义有效后，在同一成功事务内将已验证 Annotation 置 mouse_id_status="valid"，推进 detection_import_revision/identity_revision
7. video.workflow_status="submitted"；所有事件 review_status="pending", reviewer_id=null
8. db.commit + refresh video
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `handleSubmitReview` | `submitVideoForReview` | `POST /api/projects/{pid}/videos/{vid}/submit` | `submit_video`、`_revalidate_annotations` | `Video`、`Annotation` |
### 5.5 审核人通过/退回
**用户操作 3 步**
1. 审核页选择 submitted 视频，查看 overlay、事件、ID 和 revision。
2. 通过可选意见；退回必须由当前前端填写意见。
3. 点击“通过/退回”并确认。
**前端调用 3 段**
```pseudo
handleReview(result):
 rejected and empty comment → local error
 confirm → createVideoReview(pid, vid, {result, comment})
 approved: 保留详情并显示媒体排队；rejected: 清选择
 await loadQueue()
```
**后端事务 8 步**
```pseudo
create_review:
1. role owner/admin/reviewer；video 必须 submitted
2. 构造 Review，快照 annotation/detection_import/identity revisions
3. approved 时再次确认状态，取 active import，调用纯校验 _revalidate_annotations；失败 → 409，Annotation 状态和数据库不变
4. approved 且全部语义有效时，在同一成功事务内将已验证 Annotation 置 valid，并同步 import/identity revisions（stale 但语义有效可推进）
5. approved → video approved + approved_at/by
6. rejected → video rejected + 清 approved 字段
7. 所有 Annotation.review_status=result、reviewer_id=当前审核人；db.add(review); db.commit
8. approved 后尝试 enqueue_media_job 并 schedule；调度异常只记日志，不回滚审核
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `handleReview` | `createVideoReview` | `POST /api/projects/{pid}/videos/{vid}/review` | `create_review`、`_revalidate_annotations` | `Review`、`Video`、`Annotation`，随后媒体 job/`Clip` |
### 5.6 单视频事件导出与项目 ZIP
**用户操作 3 步**
1. 标注页“导出 JSON”可直接下载该视频全部事件的完整 ExportEvent JSON。
2. 项目 ExportPage 选择类别或全部，点击“开始导出 ZIP”。
3. 每 4 秒轮询；成功后以 Bearer 请求下载 ZIP。
**前端调用 4 段**
```pseudo
AnnotatePage.handleExport → exportAnnotations → 浏览器 Blob 下载
ExportPage.handleExport → createExport(category_ids)
busy 时 getExportStatus 每 4000ms
ExportPage.handleDownload → fetchExportDownload → Blob 下载
```
**后端事务 9 步**
```pseudo
create_export / enqueue_export_job:
1. owner/admin；校验类别归属；active dedupe key 冲突 → 409
2. approved_rows 只选 Annotation approved + mouse valid + Video approved
3. BackgroundJob.payload 冻结 annotation_ids 和各 video.annotation_revision
4. worker claim queued→running；缺 Clip 时 _fill_clip
5. _event_record 写 mouse_ids/import/identity revision/clip_file
6. _package 写单一 annotations.json、clips/、corrected_tracks/
7. generate_corrected_tracks 排除 suppression，按事件 revision 生成 JSONL/manifest
8. 校验事件与 MP4 双向一致，发布前再次校验 snapshot
9. BEGIN IMMEDIATE + os.replace + job succeeded/result_path/expires_at + commit；异常 rollback/failed
```
| React | wrapper | endpoint | backend/internal | 关键模型 |
|---|---|---|---|---|
| `handleExport`（标注页） | `exportAnnotations` | `GET /api/projects/{pid}/videos/{vid}/annotations/export` | `export_annotations` | `Annotation` |
| `handleExport`（导出页） | `createExport` | `POST /api/projects/{pid}/export` | `create_export` → `enqueue_export_job` → `ExportWorker` | `BackgroundJob`、`Annotation`、`Video`、`Clip` |
| polling | `getExportStatus` | `GET /api/projects/{pid}/export/status` | `export_status`、`approved_rows` | 同上 |
| `handleDownload` | `fetchExportDownload` | `GET /api/projects/{pid}/export/download` | `download_export` | `BackgroundJob` |
单视频 `export_annotations` 也调用 `_event_record`，返回 `annotation_id`、`mouse_ids`、`detection_import_revision`、`identity_revision`、`clip_file` 等完整字段；没有 ready Clip 时 `clip_file=null`。项目 ZIP 中 `clip_file` 必须是安全非空 ZIP 相对路径。
## 6. ID 修正操作
### 6.1 Split：check → confirm → commit
**用户操作 4 步**
1. 切到 track 修正模式，在当前帧 F 单选一个 ID。
2. 点击“从当前帧 Split”。
3. 查看前后检测数、受影响事件和新 ID 说明。
4. 确认提交；成功后默认选择新 ID。
**前端调用 3 段**
```pseudo
runIdentityEdit("split"):
 request={track_ids:[old], frame:F, base revisions}
 check=checkIdentityEdit(...)
 confirm(check summary)
 result=commitIdentityEdit(...)
 setIdentityRevision; select new_display_track_id
 remember lastIdentityEditId; refreshKey++; loadAnnotations()
```
**后端事务 11 步**
```pseudo
check_identity_edit → _validate_split（不写库）:
1. 恰好一个 active track；F in (first_frame, last_frame]
2. 排除 suppression 后，F 前和 F 起均至少一条检测
3. new display ID = max(active display_track_id) + 1
4. affected event = old ID in mouse_ids and ann.end_frame >= F
commit_identity_edit → _commit_split:
5. 重查 base revisions 并再次 _validate_split
6. old_rev=current；new_rev=old+1；INSERT new CorrectedTrack(first_frame=F)
7. _materialize_cda_snapshot(old_rev → new_rev)
8. new revision 中 frame < F 仍归 old；frame >= F 的 CDA 改归 new track
9. 重算两 track first/last/count
10. `_revalidate_video_annotations(..., force_needs_mouse_ids=affected)` 重校验全部 Annotation：受 Split 影响项强制 needs_mouse_ids，其余有效项推进 revisions
11. 对实际受影响/无效项执行审核失效；Video.identity_revision=new；INSERT IdentityEdit(snapshot/affected IDs)；db.commit
```
| React | wrappers | endpoints | backend/internal | 写入模型 |
|---|---|---|---|---|
| `runIdentityEdit("split")` | `checkIdentityEdit` / `commitIdentityEdit` | `POST /api/projects/{pid}/videos/{vid}/identity-edits/check` / `POST /api/projects/{pid}/videos/{vid}/identity-edits` | `check_identity_edit`、`commit_identity_edit`、`_validate_split`、`_commit_split` | `CorrectedTrack`、CDA、`Annotation`、`IdentityEdit`、`Video` |
### 6.2 Merge：冲突检查 → confirm → commit
**用户操作 4 步**
1. 多选至少两个 ID，点击“Merge”。
2. 若返回冲突帧，检查 track 归属：整轨误检则忽略整个 track；否则用 Split 隔离不重叠片段，只 Merge 无冲突片段；无法消除冲突时保持分离并重新导入修正后的上游检测。
3. 无冲突时查看保留 ID、影响检测/事件数。
4. 确认 Merge；成功后选择保留 ID。
**前端调用 3 段**
```pseudo
runIdentityEdit("merge"):
 checkIdentityEdit(track_ids, base revisions)
 if conflict_frames.length: show error and stop
 confirm → commitIdentityEdit
 select retained_display_track_id; remember edit_id; refreshKey++; reload annotations
```
**后端事务 12 步**
```pseudo
_validate_merge:
1. 至少 2 个、均为 active CorrectedTrack
2. 按 (first_frame，display_track_id) 升序；首项 retained
3. 两两检查同帧未抑制 detection 数 > 1，返回 conflict_frames
_commit_merge:
4. commit 再校验；仍有 conflict → 400，不写库
5. new_rev=old+1；物化 CDA snapshot
6. 保存各 merged display ID 的 old-revision raw IDs，供撤销
7. 新 revision 中 merged tracks 的 CDA 全改归 retained
8. 重算 retained 范围/计数；merged track active=false、merged_into_id=retained.id
9. 每个事件：merged display ID → retained display ID，再 set 去重并升序
10. `_revalidate_video_annotations` 重校验全部 Annotation；去重后数量或覆盖不合法项 → needs_mouse_ids，其余项推进 revisions
11. mouse_ids 被改或重校验无效的事件审核失效；Video.identity_revision++
12. INSERT IdentityEdit(original_assignment_map + annotation snapshots)；db.commit
```
| React | wrappers | endpoints | backend/internal | 写入模型 |
|---|---|---|---|---|
| `runIdentityEdit("merge")` | `checkIdentityEdit` / `commitIdentityEdit` | `POST /api/projects/{pid}/videos/{vid}/identity-edits/check` / `POST /api/projects/{pid}/videos/{vid}/identity-edits` | `_validate_merge`、`_commit_merge` | `CorrectedTrack`、CDA、`Annotation`、`IdentityEdit`、`Video` |
### 6.3 忽略整个 track：冻结当前所有未抑制检测
**用户操作 3 步**
1. 单选一个 active ID，点击“忽略整个 track”。
2. 确认该操作按当前 revision 冻结具体检测集合。
3. 成功后 ID 从检测和 track 摘要中消失。
**前端调用 2 段**
```pseudo
suppressTrack():
 createSuppression(scope="corrected_track", track_id=selectedId, base revisions)
 save lastSuppressionId; clear selection; refreshKey++; reload annotations
```
**后端事务 8 步**
```pseudo
create_suppression track scope:
1. guards；display ID 必须对应 active track
2. 查询 old revision 下该 corrected_track 的全部 CDA raw IDs
3. 排除已经 suppressed 的 raw IDs，形成冻结 detection_ids
4. 空集合表示整轨已抑制 → 409 "Track is already fully suppressed"
5. materialize snapshot；INSERT 一个 DetectionSuppression
6. 为冻结集合逐个 INSERT SuppressionDetection
7. `_revalidate_video_annotations` 重校验全部 Annotation，合法项推进 revisions、无效项标 needs_mouse_ids；identity_revision++；实际受影响审核失效
8. db.commit；响应 frozen_detection_count/affected_track_ids
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| `suppressTrack` | `createSuppression` | `POST /api/projects/{pid}/videos/{vid}/detection-suppressions` | `create_suppression` | `DetectionSuppression`、多个 `SuppressionDetection`、CDA、`Annotation`、`Video` |
“冻结”表示后续 Split/Merge 不改变这次 suppression 的 raw detection 集合；不是按未来 track 动态扩展。
### 6.4 列出并撤销当前 active import 的整轨忽略
**用户操作 2 步**
1. 页面加载/刷新时读取 active suppression 列表，点击任一当前可撤销的“忽略记录”。
2. overlay 和事件状态刷新；再次撤销同一记录会被后端拒绝。
**前端调用 2 段**
```pseudo
loadActiveSuppressions(): listDetectionSuppressions() → 仅当前 active import 未撤销项
revertLastSuppression(id):
 revertSuppression(id, base revisions)
 从 activeSuppressions 移除；set identity revision; refreshKey++; loadAnnotations()
```
**后端事务 8 步**
```pseudo
revert_suppression:
1. guards；原 suppression 属于该视频且必须属于当前 active import，否则 409
2. 已存在 reverted_suppression_id 指向它 → 409
3. materialize CDA old→new
4. 读取原 suppression 的 SuppressionDetection，收集 affected tracks
5. DELETE 这些关联行（RawDetection 不删）
6. INSERT 新 DetectionSuppression(operation-like audit, reverted_suppression_id=原 ID)
7. identity_revision++；重校验全部 Annotation，合法项推进 revisions、无效项 needs_mouse_ids，并使实际受影响审核失效
8. db.commit
```
| React | wrapper | endpoint | backend | 写入模型 |
|---|---|---|---|---|
| 页面加载 / `revertLastSuppression` | `listDetectionSuppressions` / `revertSuppression` | `GET /api/projects/{pid}/videos/{vid}/detection-suppressions` / `POST .../{sid}/revert` | `list_active_suppressions` / `revert_suppression` | suppression audit、`SuppressionDetection`、CDA、`Annotation`、`Video` |
### 6.5 撤销最后一次 Split/Merge
**用户操作 2 步**
1. 同一页面会话内点击当前实现的 Split/Merge 专用撤销；计划中的统一文案为“撤销上一次 track 修正”。
2. 查看恢复后的 track、事件 ID 和审核状态。
**前端调用 2 段**
```pseudo
revertLastIdentity():
 require lastIdentityEditId in React state
 revertIdentityEdit(editId, base revisions)
 clear lastIdentityEditId; set revision; refreshKey++; loadAnnotations()
```
**后端事务 10 步**
```pseudo
revert_identity_edit:
1. guards；IdentityEdit 必须属于 video 且 operation in split/merge
2. 已有 IdentityEdit.reverted_edit_id 指向原 edit → 409
3. materialize old→new CDA snapshot
4. split revert：新 track CDA 归回旧 track；重算旧 track；新 track inactive
5. merge revert：merged tracks 重新 active
6. 按 original_assignment_map 把 raw IDs 从 retained 分回原 tracks
7. 重算各 track 范围/计数
8. `_restore_annotation_snapshots` 后由 `_revalidate_video_annotations` 重校验全部 Annotation，推进有效项 revisions、标记无效项
9. INSERT operation="revert" IdentityEdit；identity_revision++；失效实际受影响审核
10. db.commit
```
| React | wrapper | endpoint | backend/internal | 写入模型 |
|---|---|---|---|---|
| `revertLastIdentity` | `revertIdentityEdit` | `POST /api/projects/{pid}/videos/{vid}/identity-edits/{eid}/revert` | `revert_identity_edit`、`_revert_split`、`_revert_merge` | `IdentityEdit`、`CorrectedTrack`、CDA、`Annotation`、`Video` |
**当前撤销限制**：整轨 suppression 通过 active 列表持久恢复；列表只返回当前 active import 中尚未撤销的项，旧 import 项不列出且撤销返回 409。`lastIdentityEditId` 仍只在 `AnnotatePage` React 内存中，刷新/离开页面后不能撤销该次 Split/Merge。统一按实际时间撤销三类操作仍未实现。
### 6.6 suppression 后检测查询与修正后 track 摘要
**用户操作 2 步**
1. 完成忽略或撤销。
2. 前端 `refreshKey++`，观察 overlay 和 track 列表重新加载。
**前端调用 2 段**
```pseudo
refreshKey 改变 → DetectionOverlay 清 cache/pending，重取 current import/detections
identityRevision 改变 → AnnotatePage 重新 getCorrectedTracks
```
**后端事务 4 步（只读）**
```pseudo
1. _get_suppressed_detection_ids 计算 active_unreverted suppression 集合
2. /detections 排除这些 RawDetection IDs
3. /corrected-tracks 聚合前同样排除；无剩余检测的 track 不产生 group row
4. current_frame visible 查询也排除；corrected export 则保留所有 `0..frame_count-1` 帧，空帧输出空数组
```
| wrapper | endpoint | backend | 结果 |
|---|---|---|---|
| `getDetections` | `GET /api/projects/{pid}/videos/{vid}/detections` | `get_detections` | overlay 无 suppressed 框 |
| `getCorrectedTracks` | `GET /api/projects/{pid}/videos/{vid}/corrected-tracks` | `get_corrected_tracks` | 全抑制 ID 不在摘要中 |
| `getCorrectedTracksExport` | `GET /api/projects/{pid}/videos/{vid}/detections/export` | `export_corrected_detections` → `generate_corrected_tracks` | corrected JSONL 排除 suppression |
| `listDetectionSuppressions` | `GET /api/projects/{pid}/videos/{vid}/detection-suppressions` | `list_active_suppressions` | 当前 active import 未撤销项 |
## 7. 失败、回滚与刷新分支
```pseudo
400 validation: 非法区间/类别/数量/覆盖/active track/Split 边界/import/空标注 → 不 commit，前端设置 errorMsg
409 stale revision:
 identity/suppression/revert 的 base import/identity 过期，或 PATCH revision 与 Annotation 快照不符
 → 刷新当前数据，不用旧请求重放
409 duplicate/already reverted: 整轨无剩余未抑制检测、edit/suppression 已撤销 → 保留现状
Merge conflict: check 返回 conflict_frames 并停止；commit 重检仍冲突 → 400 {message, conflict_frames}
needs_mouse_ids: Split 涉及 F 后事件、Merge 去重数量非法、suppression 后零覆盖、替换 import
 → 禁止 submit，编辑事件重新选择并保存
DB failure: 未 commit 变化回滚；导入/导出显式 db.rollback()；identity/suppression 末尾单次 commit
cache refresh: 成功后 refreshKey++、loadAnnotations；genRef 丢弃旧响应并清 ±15 缓存；identityRevision 重取摘要
```
补充边界：`get_detections` 每次最多返回 500 行；Overlay 以 `total >= 500` 标截断，长密集窗口可能需缩小块或分页（当前未实现）。
## 8. 端到端示例：视频“社交-攻击1”
### 8.1 创建攻击事件 `[8,20]`
```text
假设：当前 active import revision=1，identity_revision=0；攻击类别要求恰好 2 只。
用户：选择“攻击行为” → 点击 ID 8、20 → F=60 按 S → F=120 按 D。
前端：markStart 保存起点；markEnd 调 createAnnotation(mouse_ids=[8,20])。
后端确认两个 ID 均 active 且区间内有未抑制检测；结果为 mouse_id_status=valid、review_status=pending。
```
### 8.2 在 F=100 Split ID 8
```text
用户：切到 track 修正，帧 100 单选 ID 8，点击 Split 并确认。
check：要求 ID 8 在 <100 与 >=100 两侧都有未抑制检测。
new ID：当前 active 最大 display ID + 1；若最大为 38，则新 ID=39。
commit：<100 的 CDA 继续归 ID 8；>=100 的 CDA 改归 ID 39。
事件因 end_frame=120 >=100 且含 ID 8 变为 needs_mouse_ids；用户据实改为 [8,20]、[20,39] 或拆成两个事件。
```
### 8.3 忽略整个 track ID 39
```text
用户：单选 ID 39 → “忽略整个 track” → 确认。
后端：在当前 identity revision 下解析 ID 39 的 CDA，排除已抑制项，冻结剩余 raw IDs。
写入：一个 DetectionSuppression + 多个 SuppressionDetection；RawDetection 保留。
查询：/detections 排除这些 raw IDs，所以 overlay 不再画 ID 39。
摘要：/corrected-tracks 聚合前排除同一集合，ID 39 无剩余 row；导出也不含这些检测。
撤销：刷新后由 active suppression 列表恢复入口；替换 import 后旧 suppression 不再列出且不能撤销。
```
## 9. 按源文件快速索引
| 源文件 | 查阅重点 |
|---|---|
| `backend/app/routers/identity_edits.py` | `_validate_split`、`_validate_merge`、`_commit_split`、`_commit_merge`、`_revalidate_video_annotations`、撤销、CDA 快照、审核失效。 |
| `backend/app/routers/suppressions.py` | `list_active_suppressions`、`create_suppression`、`revert_suppression`、整轨冻结集合、旧 import/重复撤销 409。 |
| `backend/app/routers/detection_imports.py` | `_validate_source_filename`、`_validate_replacement_compatibility`、`complete_import_batch`、`replace_detection_import`、`get_detections`、`get_corrected_tracks`、`_get_suppressed_detection_ids`、`generate_corrected_tracks`。 |
| `backend/app/routers/annotations.py` | `create_annotation`、`update_annotation`、`delete_annotation`、`_validate_mouse_ids`、语义/媒体失效。 |
| `backend/app/routers/reviews.py` | `submit_video`、`create_review`、`_revalidate_annotations`、三修订审核快照。 |
| `backend/app/export_jobs.py` | `approved_rows`、`enqueue_export_job`、`_event_record`、`ExportWorker`、ZIP 快照与发布。 |
| `frontend/src/pages/AnnotatePage.tsx` | S/D、无导入草稿、事件 CRUD、`runIdentityEdit`、整轨 `suppressTrack`、active suppression 恢复、Split/Merge 会话撤销 ID、`overlayRefresh`。 |
| `frontend/src/components/DetectionOverlay.tsx` | FPS 帧计算、±15 缓存、rAF 绘制、坐标映射、重叠 hit cycle。 |
| `frontend/src/pages/ReviewPage.tsx` | `handleReview`、只读事件/修订、审核后媒体状态。 |
| `frontend/src/pages/ExportPage.tsx` | `handleExport`、4 秒轮询、`handleDownload`、导出范围。 |
| `frontend/src/api/index.ts` | 所有 wrapper 与 HTTP 路径映射。 |
| [旧 YOLO 设计](YOLO检测结果接入与track修正设计.md) | 被替换方案的历史契约、实现边界与回滚背景。 |
维护时先沿“React 函数 → wrapper → endpoint → internal → model → refresh”链路定位；不要把 display ID 当作跨视频真实身份，也不要通过修改或删除 `RawDetection` 实现 ID 修正。
