"""ORM 선언 기반 클래스와 제약 이름 규칙.

## 왜 naming_convention을 지정하는가

SQLite는 `ALTER TABLE ... DROP CONSTRAINT`를 지원하지 않는다. Alembic이 SQLite에서
제약을 고치려면 **테이블을 새로 만들고 데이터를 옮기는(batch mode)** 수밖에 없는데,
이때 제약에 이름이 없으면 재생성할 대상을 특정하지 못해 마이그레이션이 실패한다.

이름을 붙이지 않으면 SQLAlchemy가 익명 제약(`CHECK (...)`)을 만들고, 그건
`create_all()`로 만든 테스트 DB에서는 아무 문제가 없다가 **마이그레이션을 되돌릴 때
배포 DB에서만 터진다.** PostgreSQL로 옮긴 뒤에도 자동 생성된 이름이 DB 종류마다
달라 다운그레이드가 재현되지 않는다.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# ix=인덱스 / uq=UNIQUE / ck=CHECK / fk=FK / pk=PK.
# 이름을 사람이 예측할 수 있어야 마이그레이션에 그대로 적을 수 있다.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """모든 ORM 모델의 기반 클래스."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
