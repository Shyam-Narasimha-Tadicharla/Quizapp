"""auth and multi-tenancy

Revision ID: 002
Revises: 001
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create users table
    op.create_table(
        "users",
        sa.Column("id",         postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("auth_id",    postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("school_id",  postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("role",       sa.String(20),                  nullable=False, server_default="teacher"),
        sa.Column("created_at", sa.DateTime(timezone=True),     nullable=False),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("auth_id", name="uq_users_auth_id"),
    )

    # 2. Add created_by to quizzes (nullable first — we backfill before enforcing)
    op.add_column("quizzes",
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True)
    )

    # 3. Insert a seed school + seed user to own all existing data.
    #    These UUIDs are stable — downgrade() deletes them by ID.
    SEED_SCHOOL_ID = "00000000-0000-0000-0000-000000000001"
    SEED_USER_ID   = "00000000-0000-0000-0000-000000000002"
    SEED_AUTH_ID   = "00000000-0000-0000-0000-000000000003"

    conn = op.get_bind()

    conn.execute(sa.text("""
        INSERT INTO schools (id, name, created_at)
        VALUES (:id, :name, now())
        ON CONFLICT (id) DO NOTHING
    """), {"id": SEED_SCHOOL_ID, "name": "Default School"})

    conn.execute(sa.text("""
        INSERT INTO users (id, auth_id, school_id, role, created_at)
        VALUES (:id, :auth_id, :school_id, 'admin', now())
        ON CONFLICT (id) DO NOTHING
    """), {"id": SEED_USER_ID, "auth_id": SEED_AUTH_ID, "school_id": SEED_SCHOOL_ID})

    # 4. Backfill existing quizzes and questions with seed IDs
    conn.execute(sa.text(
        "UPDATE quizzes   SET school_id = :sid, created_by = :uid WHERE school_id IS NULL"
    ), {"sid": SEED_SCHOOL_ID, "uid": SEED_USER_ID})

    conn.execute(sa.text(
        "UPDATE questions SET school_id = :sid WHERE school_id IS NULL"
    ), {"sid": SEED_SCHOOL_ID})

    # 5. Now enforce NOT NULL
    op.alter_column("quizzes",   "school_id",  nullable=False)
    op.alter_column("quizzes",   "created_by", nullable=False)
    op.alter_column("questions", "school_id",  nullable=False)

    # 6. Add FK constraint on quizzes.created_by → users.id
    op.create_foreign_key(
        "fk_quizzes_created_by",
        "quizzes", "users",
        ["created_by"], ["id"],
    )

    # 7. Index for common per-school list query
    op.create_index("ix_quizzes_school_id",   "quizzes",   ["school_id"])
    op.create_index("ix_questions_school_id",  "questions", ["school_id"])


def downgrade() -> None:
    op.drop_index("ix_questions_school_id",  table_name="questions")
    op.drop_index("ix_quizzes_school_id",    table_name="quizzes")

    op.drop_constraint("fk_quizzes_created_by", "quizzes", type_="foreignkey")

    op.alter_column("quizzes",   "school_id",  nullable=True)
    op.alter_column("quizzes",   "created_by", nullable=True)
    op.alter_column("questions", "school_id",  nullable=True)

    # Remove backfill data
    SEED_SCHOOL_ID = "00000000-0000-0000-0000-000000000001"
    SEED_USER_ID   = "00000000-0000-0000-0000-000000000002"

    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE quizzes   SET school_id = NULL, created_by = NULL WHERE school_id = :sid"
    ), {"sid": SEED_SCHOOL_ID})
    conn.execute(sa.text(
        "UPDATE questions SET school_id = NULL WHERE school_id = :sid"
    ), {"sid": SEED_SCHOOL_ID})

    conn.execute(sa.text("DELETE FROM users   WHERE id = :uid"), {"uid": SEED_USER_ID})
    conn.execute(sa.text("DELETE FROM schools WHERE id = :sid"), {"sid": SEED_SCHOOL_ID})

    op.drop_column("quizzes", "created_by")
    op.drop_table("users")
