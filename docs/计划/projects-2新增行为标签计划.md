# projects/2 新增四类行为标签计划

> **状态：已确认计划、尚未实施。**  
> **基线：生产 release `50be725743254c0fa55ae3b21de646d457211417`；SQLite schema `0016`。**  
> **范围：只新增、不删除、不改既有类别；不补标已提交或已审核数据。**  
> **操作边界：本轮没有服务器或代码操作，也没有修改数据库与生产状态。**

## 1. 直接结论

- `projects/2` 不需要前端代码改动：类别和颜色均由 API 动态读取数据库，颜色必须填写在新增的 `behavior_categories` 行中。
- 只有未来确认“所有新项目默认带这四类”时，才修改 `frontend/src/data/defaultCategoryScheme.ts`；本计划不修改默认新项目模板。
- demo seed 同理不改。本计划只为既有 `projects/2` 增加四类。

## 2. 最终类别表

四类均设置 `is_active=true`，并显式写入 `created_at`。排序以实施时 `projects/2` 现有类别的实际最大 `sort_order` 为基准；若生产现有 12 类且最大值为 11，则四类依次为 12、13、14、15。

| name | 分组 | color | sort_order | 参与模式 | 角色与数量 | 时间 | 人工标注定义与区分 |
|---|---|---|---|---|---|---|---|
| Following | 社交行为 | `#C34B8F` | 当前最大值 + 1 | `role_based` | Follower、Leader 各 `min=1,max=1`；总 `mouse_count=2..2` | 持续 ≥3s | 距离 5–30cm、同方向，且没有目标鼠逃跑；区别于 Chasing。 |
| Group locomotion | 群体行为 | `#058ACC` | 当前最大值 + 2 | `unordered` | `min=3,max=NULL` | 持续 ≥3s | 相邻个体距离 <30cm、方向相似。 |
| Social clustering | 群体行为 | `#817931` | 当前最大值 + 3 | `unordered` | `min=3,max=NULL` | 持续 ≥5s | 最近邻距离降低；区别于 Huddling。 |
| Dispersal | 群体行为 | `#FF2605` | 当前最大值 + 4 | `unordered` | `min=3,max=NULL` | 持续 ≥10s | 平均最近邻距离明显增加。 |

角色结构约束：

- 三个 `unordered` 类别的 `role_definitions=[]`。
- Following 的两个角色按 Follower、Leader 顺序设置 `role_sort_order=0/1`。
- Following 的两个 role key 必须在实施时分别生成互不重复的 `role_<32hex>`。本文不得写死实际 key，实施记录也应以当次真实生成值为准。

## 3. 定义来源与自动校验边界

当前项目采用的定义来源摘要为：

- Mouse Behavior Ethogram；
- Shemesh et al., 2013；
- Weissbrod et al., 2013, *Nature Communications*。

以上仅是当前项目采用的定义来源摘要。实施前若需要正式引文，应另行核对书目信息；本计划不杜撰论文题名或 DOI。

系统当前只自动校验参与小鼠数量和角色完整性。距离、方向、逃避关系以及持续 3/5/10 秒等条件当前均不会自动判定，应作为人工标注规范执行。若需在页面内展示完整定义或进行自动校验，必须另立模型、API、UI 功能，不包含在本计划中。

## 4. 数据库实施计划

以下是实施步骤与核验要求，不是服务器命令或可直接执行的 SQL：

1. **停服与备份**：进入维护窗口并停服；制作 SQLite 一致性备份，同时正确处理 WAL 模式下的主库、`-wal` 与 `-shm` 状态，避免仅复制主文件得到不一致备份。验证备份可读且完整。
2. **只读基线核对**：核实目标确为 project 2；读取现有类别的分组、名称和最大 `sort_order`；检查现有 role key，确保新 key 不冲突；确认无数据库锁与活动写入。
3. **健康基线**：记录数据库 integrity check 与 foreign key check 的实施前结果，任何异常均停止变更。
4. **单事务变更**：在一个明确事务中完成以下事项，失败则整体回滚：
   - 临时移除 `trg_category_locked_insert`，并在同一事务内按原定义恢复；
   - 按最终类别表插入四行，填写颜色、角色、数量、排序、`is_active=true` 与显式 `created_at`；
   - 建议同时临时处理 `trg_project_scheme_lock`，使其不会阻断本次受控变更，并在同一事务内按原定义恢复；
   - 建议将类别方案 `version` 增加 1，并追加由真实 owner actor 执行的 `replace` audit；基于真实变更前后完整方案重算 `before`、`after` 与 `scheme_hash`，不得使用占位 actor 或伪造审计内容；
   - 保持既有 `locked_at`、`locked_by` 不变。
5. **提交与复检**：提交后确认四行、分组、颜色、排序、角色结构、数量约束、版本/审计（若实施）均准确；确认两个临时处理的触发器已按原定义恢复；再次执行 integrity 与 foreign key 检查，并确认锁状态正常。
6. **启服与业务验收**：仅在数据库复检通过后启服，按本文验收清单检查 `projects/2`。
7. **回滚准备**：若事务内失败，直接回滚事务；若提交后或启服后发现异常，立即停服，优先恢复经验证的一致性备份，并复核 integrity、foreign key、schema `0016`、release `50be725...` 及服务状态。不得通过随意删除四行代替完整回滚，尤其是在已产生新标注或审计后。

## 5. 改动矩阵

| 级别 | 项目 | 说明 |
|---|---|---|
| 必需 | DB 新增四行 | 仅为 `projects/2` 增加 Following、Group locomotion、Social clustering、Dispersal。 |
| 必需 | 类别字段 | 正确填写颜色、角色结构、数量约束和相对排序。 |
| 必需 | 触发器恢复 | `trg_category_locked_insert` 必须恢复；若临时处理 `trg_project_scheme_lock`，也必须恢复。 |
| 必需 | 备份与校验 | 停服一致性备份、WAL/`-shm` 注意事项、实施前后 integrity/foreign key 与恢复可用性核验。 |
| 建议 | version/audit 一致性 | `version +1`，真实 owner actor 的 `replace` audit，以及真实 `before`/`after`/`scheme_hash`。 |
| 无需 | 运行时代码 | 不改前端 runtime、后端 runtime、API、Review、export 代码。 |
| 无需 | 默认与演示数据 | 不改默认新项目模板和 demo seed。 |
| 无需 | 历史数据 | 不改既有类别，不补标或改写旧 submission 快照。 |

## 6. 导出兼容性

- 当前导出没有全量标签表头或全量类别 manifest；未标注的新类别不会产生空字段。
- 新旧数据格式相同，不需要导出代码或格式迁移。
- 只有实际 approved 的新标签会产生对应目录或 annotation behavior。
- 既有提交快照保持不变，不因新增类别而回填、重写或补标。

## 7. 前端与视觉验收

实施后在 `projects/2` 完成以下人工验收：

- 类别总数为 16，四类名称、分组与顺序正确；
- 四种颜色在浅色和深色环境下均按数据库值显示，并覆盖类别按钮、类别列表和时间轴；
- Following 显示 Follower、Leader 两个角色槽位，角色顺序正确，且只能各选 1 只、总数必须为 2；
- 三个群体行为类别正确执行至少 3 只的数量门禁，不错误要求角色；
- 长名称 Group locomotion、Social clustering 在相关入口可识别且不丢失；
- 数字快捷键对应的前 10 类保持不变，不因末尾新增四类发生重排；
- 重叠标注在时间轴上仍可辨识，四种新颜色与既有类别表现一致；
- Review 页当前所有类别颜色均退化为灰色是既有行为，不是本次新增导致，也不作为本计划必改项。

## 8. 验收清单

- [ ] 维护窗口内停服，且确认无活动写入或数据库锁。
- [ ] 一致性备份完成，WAL/`-shm` 已正确处理，备份可读并通过完整性核验。
- [ ] 实施前 project 2、现有 group/name/max sort、role key、schema、integrity 与 foreign key 基线已记录。
- [ ] 仅新增四类，没有删除或修改既有类别。
- [ ] 四类名称、分组、颜色、排序、模式、数量、角色和 `created_at` 均符合最终类别表。
- [ ] Following 的两个 role key 为实施时生成的互不重复 `role_<32hex>`，角色顺序为 Follower 0、Leader 1。
- [ ] 所有临时处理的触发器均已按原定义恢复。
- [ ] `locked_at`、`locked_by` 未改变；version/audit 如实施则内容完整且 actor 真实。
- [ ] 提交后 integrity、foreign key、锁状态和业务 API 读取均正常。
- [ ] 前端显示、颜色、角色槽位、数量门禁、长名称、快捷键和重叠时间轴验收通过。
- [ ] 导出抽查符合兼容性边界，旧 submission 快照未变化。
- [ ] 启服后健康状态正常，并保留实施记录与回滚证据。

## 9. 回滚清单

- [ ] 发现异常后停止写入并停服。
- [ ] 事务未提交时执行整体回滚，并确认四行、version/audit 与触发器均回到基线。
- [ ] 已提交时使用实施前验证过的一致性备份恢复，不以手工删行替代完整恢复。
- [ ] 恢复后复核 schema `0016`、integrity、foreign key、project 2 类别方案、触发器定义、锁字段和审计一致性。
- [ ] 确认生产 release 仍为 `50be725743254c0fa55ae3b21de646d457211417`，启服后完成健康与页面抽查。

## 10. 未实施事项

截至本计划形成时，没有修改服务器、数据库、代码或生产状态；没有插入四类、生成实际 role key、调整 version/audit、停启服务或执行部署。本文只记录已确认的后续实施与验收边界。
