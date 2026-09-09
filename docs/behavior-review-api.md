# 行为级审核 API 契约

本文记录当前已实现的行为级审核接口。路径前缀均为 `/api`，所有接口沿用项目成员认证与项目隔离规则。

## 数据语义

- 每次视频提交生成不可变 `Submission` 与不可变行为快照；行为裁决另存为只追加审计记录，最大 `sequence` 为当前裁决。
- 行为状态为 `pending | approved | rejected`。写入 `rejected` 时 `feedback` 必须为非空白字符串。
- `decision_revision` 是一次提交内的并发版本。每次裁决（包括携带和重新打开产生的撤销记录）递增；写接口版本不匹配返回 409。
- 普通行为字段为类别、帧区间、置信度、裁剪区域、参与小鼠及角色。轨迹拆分、合并、抑制产生的投影变化不属于普通行为修改，不撤销已通过状态，也不能解决退回意见。
- 检测基线替换属于实质变化；旧退回行为可据此视为 `modified`。普通编辑后恢复到提交值记为 `reverted`，仍不能重新提交。
- 当前最新审核尝试中已通过的实时标注 ID 出现在 `locked_annotation_ids`，任何角色的普通 PATCH/DELETE 均返回 409。最终通过后必须先显式重新打开才能撤销。
- 最终通过后，普通新增/修改/删除行为以及确认检测基线替换均返回 409；必须先显式 `reopen`，不得通过普通写入把视频降为 draft。最终通过权威是最新尝试的 approved Submission，不能只凭 `Video.workflow_status` 投影判定；track-only 投影操作仍可执行且保留最终通过状态。
- 删除证据同时保留历史 `source_annotation_id` 和不可变基线；关联外键置空后，即使 SQLite 复用实时标注 ID，也始终判定旧条目为 `deleted`，不得关联或裁决新标注。无法恢复来源 ID 的 legacy 快照对外返回 `null`。

## 查询审核状态

`GET /projects/{project_id}/videos/{video_id}/review-state`

响应 `BehaviorReviewStateOut`：

```json
{
  "submission_id": 12,
  "attempt_no": 2,
  "submission_status": "submitted",
  "decision_revision": 3,
  "counts": {"pending": 1, "approved": 2, "rejected": 0},
  "can_finalize_approval": false,
  "annotations": [{
    "id": 31,
    "source_annotation_id": 8,
    "category_id": 2,
    "category_name": "追逐",
    "category_group": "社交行为",
    "category_participant_mode": "unordered",
    "role_definitions": [],
    "participant_roles": {},
    "mouse_ids": [1, 2],
    "start_time": 0.0,
    "end_time": 1.0,
    "start_frame": 0,
    "end_frame": 25,
    "confidence": "certain",
    "crop_region": null,
    "decision": {
      "status": "approved",
      "feedback": null,
      "decision_id": 44,
      "sequence": 2,
      "reviewer_id": 3,
      "reviewer": "reviewer1",
      "decided_at": "2026-09-09T10:00:00",
      "origin": "manual",
      "carried_from_decision_id": null
    }
  }],
  "feedback_items": [],
  "locked_annotation_ids": [8],
  "can_reopen": false
}
```

`feedback_items` 仅列出最近一次视频退回中明确被逐条拒绝的行为，含不可变 `baseline`、当前实时 `current`（已删除时为 `null`）、审核人、时间和 `comparison`：`unchanged | modified | deleted | reverted`。

## 写入单条行为裁决

`PUT /projects/{project_id}/videos/{video_id}/submissions/{submission_id}/annotations/{snapshot_id}/decision`

请求：

```json
{"status":"rejected","feedback":"起止帧需要调整","expected_decision_revision":3}
```

成功直接持久化并返回完整 `BehaviorReviewStateOut`。仅有审核权限的成员可写；当前 `submitted` 尝试可写三种状态。最新尝试为 `rejected/withdrawn` 时仅允许写 `pending` 显式撤销，解除单条锁定或退回要求，旧审计仍保留；旧轮次和最终通过尝试禁止直接撤销，最终通过必须先 `reopen`。所有撤销仍须携带准确的 `expected_decision_revision`。

## 最终视频裁决

`POST /projects/{project_id}/videos/{video_id}/review`

请求在原字段上增加并发上下文：

```json
{"result":"approved","comment":"通过","expected_submission_id":12,"expected_decision_revision":4}
```

- `approved`：必须所有快照当前均为 `approved`，否则 409 `behavior_decisions_incomplete`。
- `rejected`：允许仍有 `pending`；视频级退回不会把未逐条裁决的历史行为伪造为单条拒绝。
- 两个 `expected_*` 字段均必填且不可为 null：`expected_submission_id > 0`、`expected_decision_revision >= 0`。缺失/非法上下文返回 422；提交 ID 或裁决修订过期返回 409 `stale_submission` / `stale_decision_revision`，不写入 Review、裁决或视频状态。通过与退回使用同一门禁。

## 重新打开最终通过

`POST /projects/{project_id}/videos/{video_id}/submissions/{submission_id}/reopen`

请求：`{"reason":"抽查复核"}`。仅审核角色可重新打开最新的最终通过尝试。成功后旧提交变为 `superseded`，视频回到 `draft`，追加重新打开审计及单条 `pending` 撤销记录，旧快照、裁决和 Review 历史全部保留。

## 退回后重新提交

`POST /projects/{project_id}/videos/{video_id}/submit` 可携带：

```json
{"expected_submission_id":12,"expected_decision_revision":4}
```

后端同时校验上述提交 ID、裁决修订和视频写门禁捕获的实时视频修订；上下文过期返回结构化 409。逐条拒绝若为 `unchanged` 或 `reverted`，返回：

```json
{
  "detail": {
    "code": "rejected_annotations_not_addressed",
    "message": "Rejected annotations must be materially modified or deleted before resubmission",
    "items": [{"submission_annotation_id":31,"source_annotation_id":8,"comparison":"unchanged"}]
  }
}
```

`modified` 或 `deleted` 可重新提交；未拒绝的 pending/new 无需修改。上一尝试中已通过且检测基线与普通材料均未变化的行为会复制为新快照并以 `origin=carried` 携带通过，其他新快照为 pending。

提交门禁追溯跨轮次未解决的逐条退回意见，不只检查最新尝试。`退回 A → 修改 A → 提交第 2 次 → 撤回 → 恢复 A → 提交第 3 次` 仍返回 409，恢复后的 `reverted` 不算处理完成。材料仍有实质差异或已删除才可继续；后续逐条通过会解决同一行为此前的退回，即使该通过之后被撤销，也不复活旧退回。显式 pending 撤销某条退回只解除该快照的退回要求，不盲目清除其他未解决历史。历史 `feedback_items` 展示范围不等于提交门禁范围。

## 旧数据兼容

旧版只有视频级 `Review`，不能证明每条行为都被拒绝。迁移不会把旧视频退回盲目展开为逐条拒绝；因此旧退回没有 `feedback_items`，也不会触发逐条“未处理”阻断。`annotations.review_status` 仅作旧界面投影，审核权威始终是不可变提交快照及最新行为裁决。

`0017` 仅为已有不可变快照且绑定旧版通过 Review 的行为补 `origin=legacy` 通过记录；不根据当前实时标注猜测缺失历史。若同一视频在旧版通过后又产生了更新的尝试，旧 approved Submission 会规范化为 `superseded`，Review 与逐条 legacy 审计仍完整保留；只有最新尝试的最终通过继续构成写入、重新提交和 purge 门禁。迁移可重复运行，保留 Review、快照及现有外键约束。

SQLite batch DDL 不视为可由单个 SQL 事务完整回滚。受支持的 `app.migration` / `scripts/migrate.py` 入口会使用数据库旁的 `*.pre-migration` 一致镜像：失败时恢复，中断后下次调用先恢复再重试。生产 L3 发布仍须先停唯一写进程并制作、校验独立 SQLite 备份；迁移或专项验证失败时保持停服并恢复该独立备份，不得依赖 SQLite DDL rollback。

## 视频硬删除

视频硬删除仍要求视频为 draft/rejected，且当前尝试不得为 submitted/approved；即使视频投影误降级，也不得 purge 当前最终通过权威。旧版已被更新尝试取代的通过记录不再单独阻断 rejected 最新稿。授权 purge 在冻结图中纳入行为裁决和重新打开审计，按子表优先清理并恢复不可变触发器。任何跨视频 `carried_from_decision_id` 入向或出向引用均拒绝删除，不允许通过外键置空影响其他视频审计。普通标注删除不删除审核证据。

## 前端接线

- `annotations[].id` 是快照 ID，供单条裁决 URL 使用；`source_annotation_id` 是历史实时 ID，可为 `null`；实时编辑锁只取 `locked_annotation_ids`，不要根据历史 ID 或旧 `Annotation.review_status` 猜锁。
- `feedback_items[].comparison` 的 `unchanged/reverted` 均未处理；`deleted` 的 `current=null`，展示仍使用 `baseline`。历史反馈展示与当前提交门禁应区分，写接口的结构化 409 为最终判断。
- `frontend/src/api/types.ts` 已直接使用 OpenAPI 生成的行为审核 DTO；快照包含 `confidence/crop_region`，实时来源 ID 按可空值处理。
- 审核页仅在 `submitted` 开放新通过/退回，在最新 `rejected/withdrawn` 尝试中单独开放撤销裁决；写成功及 409 后刷新 review-state，并保留未提交文字。审核页的视频选择器可再次进入已通过视频执行重新打开。

## 契约再生成

在仓库内执行：

```powershell
Set-Location backend
python scripts/export_openapi.py
Set-Location ../frontend
npm run api:generate
```

本次后端新增 DTO 已进入 OpenAPI，前端生成文件已通过 `npm run api:generate` 更新并作为行为审核类型来源。

## 2026-09-09 回归证据

本轮修复最终通过降级/purge 绕过、跨轮次撤回后恢复原值绕过退回要求，以及最终裁决省略并发上下文的问题。新增回归位于 `backend/tests/test_behavior_review.py`，覆盖普通新增/修改/删除及基线替换 409、reopen 后可写、视频/Submission 一致、错误投影下 purge 仍拒绝、track-only 保留通过、撤回后恢复材料的准确序列、后续通过/显式撤销解决退回、缺失/null 上下文 422 及过期上下文 409 无状态变化。

后端在 `backend/` 执行以下聚焦命令：合计 **285 passed**；仅有既有 Starlette/httpx deprecation warning，无跳过项。

```powershell
python -m pytest tests/test_authority_trigger_equivalence.py tests/test_migrations.py -q
python -m pytest tests/test_behavior_review.py -q
python -m pytest tests/test_reviews.py tests/test_submission_authority.py tests/test_video_delete_db.py -q
python -m pytest tests/test_annotation_invalidation.py -q
python -m pytest tests/test_detection_imports.py tests/test_identity_edits.py tests/test_detection_state_reconciliation.py -q
python scripts/export_openapi.py --check
```

前端仅机械执行 `npm run api:generate` 更新 `src/api/generated/schema.d.ts`；`npm run api:check`、`npm run typecheck` 和以下既有定向测试通过（**5 files / 21 tests**），未手写修改前端。

```powershell
npm test -- --run src/api/behaviorReviewApi.test.ts src/pages/behaviorReview.test.ts src/pages/reviewAnnotationView.test.ts src/pages/ReviewPage.layout.test.ts src/pages/annotationExportEntryPoints.test.ts
```

`git diff --check` 通过。本轮未执行全量后端、真实浏览器或服务器验证，未提交、未部署；既有其他改动保留。
