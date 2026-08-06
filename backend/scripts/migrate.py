"""数据库迁移 CLI（backend 目录运行）。

用法：
    .venv\Scripts\python scripts\migrate.py
    .venv\Scripts\python scripts\migrate.py --db-url sqlite:///./data/annotation.db
    .venv\Scripts\python scripts\migrate.py --check

行为（幂等，可重复运行）：
- 全新空库 → upgrade head，建立完整 schema；
- 未版本化 P1 旧库（有 users 等表、无版本行）→ 自动 stamp 0001 标记 baseline 后
  upgrade head，不删除已有数据；空 alembic_version 表（先前 alembic check 副作用）
  同样按“无有效版本行”识别，不误判为已版本化；
- 已版本化 → 幂等 upgrade head。
- 非预期表 / 未知版本 / 版本表损坏 → 报错退出（退出码 2），不执行任何修改。

应用启动时（create_app）也会自动执行同样的迁移，本 CLI 供部署/CI 显式迁移使用。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Windows 控制台 UTF-8 输出，避免中文路径/文本乱码
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

from app.config import get_settings  # noqa: E402
from app.migration import (  # noqa: E402
    MigrationStateError,
    current_revision,
    inspect_state,
    run_migrations,
    version_status,
)

STATE_LABELS = {
    "versioned": "已版本化（alembic_version 含有效版本行，幂等 upgrade head）",
    "unversioned_p1": (
        "未版本化的 P1 旧库（有 users 等核心表；无版本表或空版本表，"
        "将 stamp 0001 标记 baseline 后升级，数据保留）"
    ),
    "empty": "全新空库（无数据表；无版本表或空版本表，将从头建立完整 schema）",
}


def _main() -> None:
    parser = argparse.ArgumentParser(description="标注网站后端数据库迁移（Alembic）")
    parser.add_argument(
        "--db-url",
        default=None,
        help="数据库 URL；默认取配置 DATABASE_URL 或 DATA_DIR/annotation.db",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查并输出当前迁移状态，不做任何修改",
    )
    args = parser.parse_args()

    settings = get_settings()
    database_url = args.db_url or settings.resolved_database_url

    print(f"数据库: {database_url}")
    print(f"版本表: {version_status(database_url)}")
    try:
        state = inspect_state(database_url)
    except MigrationStateError as exc:
        print(f"状态: 不安全：{exc}")
        print("未执行任何修改。请人工检查数据库后再处理。")
        sys.exit(2)
    print(f"状态: {STATE_LABELS[state]}")
    if state == "versioned":
        print(f"当前版本: {current_revision(database_url)}")
    if args.check:
        return

    before = run_migrations(database_url)
    print(f"迁移完成（{STATE_LABELS[before]} → head，幂等可重复运行）")


if __name__ == "__main__":
    _main()
