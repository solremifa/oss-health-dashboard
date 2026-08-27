"""issue_analyses table

M3 분석 계층이 쓰는 LLM 분류 결과 테이블. `app/models/tables.py`에서
--autogenerate로 뽑은 뒤 손으로 한 군데를 지웠다.

## 지운 것: `ck_issues_issue_state` drop/create

autogenerate가 `issues`의 enum CHECK를 "지워졌다"고 잡는다. 실제로 지워진 것이
아니라, `Enum(create_constraint=True)`이 만든 제약은 **컬럼 타입에 딸린 것**이라
메타데이터 쪽에서는 세어지지 않는데 DB에서 리플렉션하면 평범한 CHECK로 돌아오기
때문이다. 그대로 두면 이 마이그레이션이 멀쩡한 제약을 진짜로 떨어뜨린다.

같은 이유로 `tests/test_migrations.py`가 비교에서 그 제약을 제외한다. enum 컬럼이
있는 테이블에 마이그레이션을 추가할 때마다 다시 나오므로, 생성된 파일을 눈으로
확인하라는 규칙(`CLAUDE.md` 12절)이 여기에 그대로 걸린다.

Revision ID: 16a64b754406
Revises: 0741fb4039e2
Create Date: 2026-08-27 19:53:16.805036

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "16a64b754406"
down_revision: str | Sequence[str] | None = "0741fb4039e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "issue_analyses",
        sa.Column("issue_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "bug",
                "feature_request",
                "question",
                "other",
                name="issue_category",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "sentiment",
            sa.Enum(
                "positive",
                "neutral",
                "frustrated",
                name="issue_sentiment",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(model) > 0", name=op.f("ck_issue_analyses_model_not_empty")),
        sa.CheckConstraint(
            "length(prompt_version) > 0",
            name=op.f("ck_issue_analyses_prompt_version_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name=op.f("fk_issue_analyses_issue_id_issues"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("issue_id", name=op.f("pk_issue_analyses")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("issue_analyses")
