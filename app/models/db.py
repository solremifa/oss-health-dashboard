"""엔진·세션 생성과, SQLite를 제대로 동작시키기 위한 전역 리스너.

## SQLite의 기본값 두 가지가 이 프로젝트의 규칙과 정면으로 어긋난다

**(1) FK를 강제하지 않는다.** SQLite는 `PRAGMA foreign_keys`가 기본 OFF다. 선언한
FK가 **장식으로만 남고** 고아 행이 조용히 들어간다. 테스트도 통과하고 수집도
성공한다. PostgreSQL로 옮긴 다음에야 "이 데이터는 원래 못 들어갔어야 한다"고
터진다. 그래서 연결이 열릴 때마다 켠다.

**(2) pysqlite 드라이버가 트랜잭션을 자기 마음대로 연다.** 표준 라이브러리의
`sqlite3`는 DML 앞에서 임의로 `BEGIN`을 넣고 DDL 앞에서 커밋해버려서
**SAVEPOINT가 의도대로 동작하지 않는다.** `session.begin_nested()`로 UNIQUE 위반만
되돌리려는 #7의 처리가 여기에 직접 걸린다. SQLAlchemy 문서가 권하는 대로 드라이버의
암묵적 트랜잭션을 끄고 `BEGIN`을 우리가 직접 낸다.

두 리스너 모두 **`Engine` 클래스에 전역 등록**한다. 엔진을 만들 때마다 붙이는
방식이면 테스트에서 새 엔진을 만든 사람이 빼먹는 순간 FK가 꺼진 채로 돌아간다.
대신 `sqlite3.Connection`인지 확인해서 PostgreSQL 연결에는 손대지 않는다.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.logging import get_logger

logger = get_logger(__name__)

_IN_MEMORY_URLS = frozenset({"sqlite://", "sqlite:///:memory:"})


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection: Any, connection_record: Any) -> None:
    """SQLite 연결마다 FK를 켜고, 드라이버의 암묵적 트랜잭션을 끈다.

    Args:
        dbapi_connection: 방금 열린 DBAPI 연결.
        connection_record: 커넥션 풀이 관리하는 레코드. 쓰지 않는다.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        # PostgreSQL 등 다른 드라이버. 손대지 않는다.
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

    # SAVEPOINT를 쓰려면 트랜잭션 시작 시점을 우리가 쥐어야 한다. 아래 "begin"
    # 리스너와 반드시 짝으로 동작한다 -- 한쪽만 두면 자동 커밋 상태가 된다.
    dbapi_connection.isolation_level = None


@event.listens_for(Engine, "begin")
def _emit_explicit_begin(connection: Connection) -> None:
    """SQLite에서 트랜잭션을 명시적으로 연다.

    Args:
        connection: 트랜잭션을 시작하는 연결.
    """
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql("BEGIN")


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """접속 문자열 하나로 엔진을 만든다.

    경로를 코드에 하드코딩하지 않는다. PostgreSQL 이관은 이 문자열만 바꾸는
    작업이어야 한다(`CLAUDE.md` 3절).

    인메모리 SQLite에는 `StaticPool`을 쓴다. 기본 풀은 연결마다 **서로 다른 빈
    데이터베이스**를 열어서, 테이블을 만든 연결과 조회하는 연결이 달라지는 순간
    `no such table`이 난다.

    Args:
        url: SQLAlchemy 접속 문자열.
        echo: SQL을 로그로 출력할지 여부.

    Returns:
        설정이 끝난 엔진.
    """
    if url in _IN_MEMORY_URLS:
        return create_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, echo=echo)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """세션 팩토리를 만든다.

    `expire_on_commit=False`로 둔다. 커밋 직후 객체 속성을 읽을 때마다 SELECT가
    다시 나가면, 커밋으로 트랜잭션을 닫은 뒤 응답을 만드는 라우터에서
    `DetachedInstanceError`가 난다.

    Args:
        engine: 세션이 사용할 엔진.

    Returns:
        세션 팩토리.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
