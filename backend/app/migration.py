"""程序化 Alembic 迁移入口：create_app 启动时自动迁移与 scripts/migrate.py CLI 共用。

幂等策略：
- 全新空库：直接 `upgrade head`（0001 建 P1 表，0002/0003 增量）建立完整 schema。
- 未版本化的 P1 旧库（已有 users 等表、无版本行）：先 `stamp 0001`
  标记 baseline，再 `upgrade head`；不删除任何已有数据。
- 空 alembic_version 表（由先前 `alembic check` 副作用创建、无版本行）：
  按“无有效版本行”处理——含 P1 核心表归入 unversioned_p1（stamp baseline 后升级），
  无 P1 核心表归入 empty（直接从头升级），与“无版本表”等价。
- 已版本化：直接 `upgrade head`，重复运行无副作用。
- 非预期表 / 未知版本 / 版本表损坏：抛 MigrationStateError，不做任何修改，禁止盲 stamp。

版本号只作为字面量出现在迁移脚本（migrations/versions/）与本模块的 baseline 标记中，
不在业务路由/模型里硬编码任何迁移版本。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

BACKEND_DIR = Path(__file__).resolve().parent.parent

# P1 baseline 版本号（迁移脚本 migrations/versions/0001_baseline_p1.py 的字面量）
BASELINE_REVISION = "0001"

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
SCRIPT_LOCATION = BACKEND_DIR / "migrations"

VERSION_TABLE = "alembic_version"

# 已知迁移版本（迁移脚本 migrations/versions/ 中的 revision 字面量）
KNOWN_REVISIONS = frozenset(
    {"0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012"}
)

# 0001 baseline 建立的 6 张 P1 核心表
P1_TABLES = frozenset(
    {"users", "projects", "project_memberships", "behavior_categories", "videos", "annotations"}
)
# 0002 增量新增的表
P2_TABLES = frozenset({"reviews", "clips", "background_jobs"})
# 0005 增量新增的表（YOLO 检测导入与身份修正基础）
P3_TABLES = frozenset(
    {
        "video_import_batches",
        "detection_imports",
        "raw_detections",
        "corrected_tracks",
        "corrected_detection_assignments",
        "identity_edits",
        "detection_suppressions",
        "suppression_detections",
    }
)
# 0008 additive sparse-state / immutable-submission foundation
P4_TABLES = frozenset(
    {
        "detection_state_overrides",
        "draft_identity_edits",
        "draft_detection_changes",
        "detection_snapshots",
        "detection_snapshot_states",
        "submissions",
        "submission_annotations",
    }
)


class MigrationStateError(RuntimeError):
    """迁移状态不安全（非预期表 / 未知版本 / 版本表损坏）时抛出，禁止盲 stamp。"""


def alembic_config(database_url: str) -> Config:
    """构造指向本项目迁移目录、绑定指定数据库 URL 的 Alembic 配置。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _ensure_sqlite_parent(database_url: str) -> None:
    """sqlite 文件库：确保父目录存在（全新库也需能落盘）。"""
    if not database_url.startswith("sqlite:///"):
        return
    path = database_url[len("sqlite:///"):]
    if path == ":memory:":
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _inspect(database_url: str) -> tuple[frozenset[str], str | None]:
    """返回 (全部表名集合, alembic 当前版本号或 None)。

    版本号取自 alembic_version 表的唯一 version_num 行；无版本表或空版本表
    （由先前 `alembic check` 副作用创建、无任何版本行）均视为 None。
    版本表损坏（结构异常 / 多行版本记录）时抛 MigrationStateError。
    """
    _ensure_sqlite_parent(database_url)
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    try:
        tables = frozenset(inspect(engine).get_table_names())
        if VERSION_TABLE not in tables:
            return tables, None
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).fetchall()
        except Exception as exc:  # noqa: BLE001  版本表结构异常一律视为损坏
            raise MigrationStateError(
                f"{VERSION_TABLE} 表无法读取版本号（{exc}），数据库迁移状态损坏"
            ) from exc
    finally:
        engine.dispose()
    if len(rows) > 1:
        raise MigrationStateError(
            f"{VERSION_TABLE} 表含 {len(rows)} 行版本记录（应为 0 或 1 行），数据库迁移状态损坏"
        )
    return tables, (rows[0][0] if rows else None)


def inspect_state(database_url: str) -> str:
    """返回数据库迁移状态：versioned / unversioned_p1 / empty。

    判定以 alembic_version 的版本行为准（而非仅看表是否存在）：
    - 有有效版本行 → versioned；
    - 无有效版本行（无版本表，或存在由先前 `alembic check` 创建的空版本表）：
      - 含 users 等 P1 核心表 → unversioned_p1；
      - 不含 P1 核心表 → empty。
    非预期表 / 未知版本 / 版本表损坏 → 抛 MigrationStateError，禁止盲 stamp。
    """
    tables, version = _inspect(database_url)

    if version is not None:
        if version not in KNOWN_REVISIONS:
            raise MigrationStateError(
                f"未知迁移版本 {version!r}（已知版本: {', '.join(sorted(KNOWN_REVISIONS))}），"
                "拒绝继续，禁止盲 stamp"
            )
        return "versioned"

    # ---- 无有效版本行（无版本表或空版本表）----
    unexpected = tables - P1_TABLES - P2_TABLES - P3_TABLES - P4_TABLES - {VERSION_TABLE}
    if unexpected:
        raise MigrationStateError(
            f"数据库存在非预期表 {sorted(unexpected)}，无法安全判定迁移状态，"
            "拒绝 stamp / 升级（请人工检查后再处理）"
        )
    has_post_baseline = bool((P2_TABLES | P3_TABLES | P4_TABLES) & tables)
    if has_post_baseline:
        # 有增量表却无有效版本行：状态不一致（如 head 库版本行被清空），不可盲 stamp
        raise MigrationStateError(
            f"数据库含增量表 {sorted((P2_TABLES | P3_TABLES | P4_TABLES) & tables)} 却无有效版本行，"
            "迁移状态不一致，拒绝自动 stamp"
        )
    if P1_TABLES & tables:
        return "unversioned_p1"
    return "empty"


def current_revision(database_url: str) -> str | None:
    """读取 alembic_version 当前版本号；无版本表或空版本表返回 None。供 CLI 展示。"""
    _tables, version = _inspect(database_url)
    return version


def version_status(database_url: str) -> str:
    """返回 human-readable 的 alembic_version 现状（供 CLI --check 输出/排查）。"""
    tables, version = _inspect(database_url)
    if VERSION_TABLE not in tables:
        return f"无 {VERSION_TABLE} 表"
    if version is None:
        return f"{VERSION_TABLE} 表存在但为空（可能由先前 alembic check 创建，无版本行）"
    return f"{VERSION_TABLE} 当前版本: {version}"


def upgrade_to(database_url: str, revision: str) -> None:
    """原始 Alembic 升级到指定版本（测试构造 P1 旧库等场景使用）。"""
    _ensure_sqlite_parent(database_url)
    command.upgrade(alembic_config(database_url), revision)


def downgrade_to(database_url: str, revision: str) -> None:
    """原始 Alembic 降级到指定版本（测试验证 downgrade 场景使用）。"""
    _ensure_sqlite_parent(database_url)
    command.downgrade(alembic_config(database_url), revision)


def run_migrations(database_url: str) -> str:
    """幂等迁移到 head；返回迁移前的状态（versioned / unversioned_p1 / empty）。

    - unversioned_p1（含“P1 核心表 + 空 alembic_version 表”缺陷形态）：先 `stamp 0001`
      标记 baseline，再 `upgrade head`，已有数据完整保留；
    - empty：直接 `upgrade head` 建立完整 schema；
    - versioned：直接 `upgrade head`，重复运行无副作用。
    非预期表 / 未知版本 / 版本表损坏等不安全状态抛 MigrationStateError，不做任何修改。
    """
    _ensure_sqlite_parent(database_url)
    state = inspect_state(database_url)
    cfg = alembic_config(database_url)
    if state == "unversioned_p1":
        # 标记 baseline：声明该库当前 schema 等价于 0001，随后应用后续增量（0002/0003）
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")
    return state
