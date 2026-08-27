"""마이그레이션이 ORM 모델과 같은 스키마를 만드는지 검증한다.

## 왜 이 테스트가 필요한가

테스트는 보통 `Base.metadata.create_all()`로 DB를 만든다. 그러면 **ORM만 고치고
마이그레이션을 빼먹어도 테스트가 전부 통과한다.** 어긋난 사실은 배포 DB에
`alembic upgrade head`를 돌린 뒤에야, 그것도 "컬럼이 없다"는 런타임 오류로 드러난다.

그래서 여기서는 반대로 간다 -- **마이그레이션만으로 DB를 만들고**, 그 결과를 ORM
메타데이터와 비교해서 차이가 0인지 본다. `--autogenerate`가 쓰는 비교기를 그대로
호출하므로, 다음 마이그레이션을 만들 때 알렘빅이 볼 차이와 같은 것을 본다.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, MetaData, inspect

from app.models import Base, create_db_engine

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(url: str) -> Config:
    """테스트용 Alembic 설정을 만든다.

    접속 문자열은 `-x db_url=...`과 같은 경로로 넘긴다. 환경변수를 건드리면
    다른 테스트에 샌다.

    Args:
        url: 마이그레이션을 적용할 DB 접속 문자열.

    Returns:
        `command.upgrade()`에 넘길 설정.
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.cmd_opts = Namespace(x=[f"db_url={url}"])
    return config


def _type_bound_check_names(metadata: MetaData) -> dict[str, set[str]]:
    """`Enum(create_constraint=True)`이 만들어낸 CHECK 제약 이름을 테이블별로 모은다.

    이런 제약은 컬럼 타입에 딸린 것이라(`_type_bound`) 알렘빅의 autogenerate가
    메타데이터 쪽에서는 세지 않는다. 반면 DB에서 리플렉션하면 평범한 CHECK로
    돌아온다. 그대로 비교하면 **양쪽이 완전히 같은데도 "제약이 지워졌다"는 차이가
    잡힌다.**

    이름을 여기에 적어두지 않고 메타데이터에서 뽑는다. enum 컬럼을 새로 추가한
    사람이 이 목록을 갱신하는 것을 잊어도 자동으로 따라온다. 테이블별로 나누는
    이유는 아래에서 "정말 그 테이블에 만들어졌는지"를 확인하기 때문이다 --
    한 덩어리로 합치면 A 테이블의 제약이 B 테이블에 있는 것으로 통과한다.

    Args:
        metadata: 검사할 메타데이터.

    Returns:
        테이블 이름 -> 타입에 딸린 CHECK 제약 이름 집합. 그런 제약이 없는
        테이블은 포함하지 않는다.
    """
    by_table: dict[str, set[str]] = {}
    for table in metadata.tables.values():
        names = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            # SQLAlchemy가 "이 CHECK는 타입이 만든 것"이라고 표시해 두는 비공개 플래그다.
            and getattr(constraint, "_type_bound", False)
            and constraint.name is not None
        }
        if names:
            by_table[table.name] = names
    return by_table


def test_migrations_produce_the_orm_schema(tmp_path: Path):
    """`alembic upgrade head`로 만든 스키마가 ORM 모델과 일치해야 한다."""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(url), "head")

    type_bound_checks = _type_bound_check_names(Base.metadata)
    assert type_bound_checks, "enum CHECK가 하나도 없다면 sa_enum 설정이 무력화된 것이다"
    excluded_names = {name for names in type_bound_checks.values() for name in names}

    engine = create_db_engine(url)
    try:
        # 비교에서 빼는 제약이 정말로 DB에 만들어졌는지는 따로 확인한다.
        # 그냥 빼기만 하면 마이그레이션에서 enum CHECK가 통째로 빠져도 통과한다.
        inspector = inspect(engine)
        for table_name, expected in type_bound_checks.items():
            reflected = {
                constraint["name"] for constraint in inspector.get_check_constraints(table_name)
            }
            assert expected <= reflected, table_name

        def include_object(
            obj: Any, name: str | None, type_: str, reflected_: bool, compare_to: Any
        ) -> bool:
            return not (type_ == "check_constraint" and name in excluded_names)

        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "target_metadata": Base.metadata,
                    "include_object": include_object,
                },
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


def test_migrations_downgrade_cleanly(tmp_path: Path):
    """되돌리기도 동작해야 한다.

    `downgrade`는 평소에 쓰지 않아 깨져도 모르고 지나가는데, 정작 필요한 순간은
    배포가 잘못돼서 급히 되돌릴 때다. 그때 처음 돌려보게 되면 늦는다.
    """
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            remaining = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).scalars()
            # alembic_version은 downgrade 후에도 남는다. 그 외에는 없어야 한다.
            assert [name for name in remaining if name != "alembic_version"] == []
    finally:
        engine.dispose()
