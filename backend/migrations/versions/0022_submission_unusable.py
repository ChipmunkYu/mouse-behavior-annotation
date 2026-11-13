"""Forward-upgrade already applied 0021 databases for unusable review decisions."""
from alembic import op

from app.authority_triggers import TRIGGERS, install_sqlite_authority_triggers

revision = "0022"
down_revision = "0021"
branch_labels = depends_on = None


def _replace_status_constraint(statuses):
    connection = op.get_bind()
    # SQLite validates external trigger references during batch table rename.
    # Preserve non-authority triggers too; only authority definitions are refreshed.
    triggers = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
    ).all()
    for name, _sql in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint("ck_submissions_status", type_="check")
        batch.create_check_constraint("ck_submissions_status", statuses)
    for name, sql in triggers:
        if name not in TRIGGERS:
            connection.exec_driver_sql(sql)
    install_sqlite_authority_triggers(connection)


def upgrade():
    _replace_status_constraint(
        "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'unusable', 'superseded')"
    )


def downgrade():
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM submissions WHERE status='unusable' LIMIT 1"
    ).first():
        raise RuntimeError("Cannot downgrade while unusable submission evidence exists")
    _replace_status_constraint(
        "status IN ('submitted', 'withdrawn', 'approved', 'rejected', 'superseded')"
    )
