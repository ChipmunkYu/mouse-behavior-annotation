"""增量：用户删除相关外键的 ON DELETE 策略显式化（Oracle Gate 1 整改）

0001 阶段 P1 原版 schema 的 users 相关外键均未显式声明 ON DELETE（SQLite
默认行为 NO ACTION）。本迁移按 Gate 1 要求把四处“用户删除”策略显式化，
全部通过 SQLite batch（重建表 + 逐行拷贝数据）完成，保留所有列、索引、
检查/唯一约束与数据：

- videos.uploaded_by        NULL     → users.id  ON DELETE SET NULL
  上传者被删除后视频元数据保留，uploaded_by 置空；
- annotations.reviewer_id   NULL     → users.id  ON DELETE SET NULL
  审核人被删除后标注保留，reviewer_id 置空；
- projects.created_by       NOT NULL → users.id  ON DELETE RESTRICT（显式）
  仍被项目引用时禁止删除创建者；
- annotations.annotator_id  NOT NULL → users.id  ON DELETE RESTRICT（显式）
  仍被标注引用时禁止删除标注者。

实现说明：Alembic batch 的 alter_column 无法改写既有未命名外键的 ondelete，
因此采用“反射表 → 原地改写 ForeignKeyConstraint.ondelete → 以 copy_from
传入 batch_alter_table 重建表”的方式；列、索引、约束、数据随反射与数据
拷贝完整保留。外键动作在运行期由应用的 `PRAGMA foreign_keys=ON` 强制生效。

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rebuild_with_fk_ondelete(table_name: str, fk_updates: dict[str, str | None]) -> None:
    """重建表并改写单列外键的 ON DELETE 行为（SQLite batch + 数据保留）。

    fk_updates: {本地列名: ondelete 字符串}；ondelete 为 None 表示回到
    SQLite 默认（无 ON DELETE 子句，等价 NO ACTION）。
    """
    bind = op.get_bind()
    meta = sa.MetaData()
    table = sa.Table(table_name, meta, autoload_with=bind)
    for const in list(table.constraints):
        if not isinstance(const, sa.ForeignKeyConstraint):
            continue
        col_keys = [c.key for c in const.columns]
        if len(col_keys) != 1:
            continue
        col_name = col_keys[0]
        if col_name not in fk_updates:
            continue
        const.ondelete = fk_updates[col_name]
        for fk in const.elements:
            fk.ondelete = fk_updates[col_name]
    # recreate="always"：即使 batch 内无其它指令也强制走“重建表+拷贝数据”
    with op.batch_alter_table(table_name, copy_from=table, recreate="always"):
        pass


def upgrade() -> None:
    _rebuild_with_fk_ondelete("videos", {"uploaded_by": "SET NULL"})
    _rebuild_with_fk_ondelete(
        "annotations", {"reviewer_id": "SET NULL", "annotator_id": "RESTRICT"}
    )
    _rebuild_with_fk_ondelete("projects", {"created_by": "RESTRICT"})


def downgrade() -> None:
    # 回退到 0001/0002 的无显式 ON DELETE（SQLite 默认 NO ACTION）
    _rebuild_with_fk_ondelete("videos", {"uploaded_by": None})
    _rebuild_with_fk_ondelete("annotations", {"reviewer_id": None, "annotator_id": None})
    _rebuild_with_fk_ondelete("projects", {"created_by": None})
