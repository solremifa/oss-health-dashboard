"""Alembic 실행 환경.

`alembic.ini`는 시스템 로캘(이 PC는 cp949)로 읽히기 때문에 ASCII만 넣을 수 있다.
그래서 설명과 한글 주석은 전부 이 파일에 둔다 -- Python 소스는 UTF-8로 읽힌다.
`docs/findings.md` 함정 6.

## 접속 문자열을 어떻게 정하는가

`DATABASE_URL` **하나로만** 정한다(`CLAUDE.md` 3절). `alembic.ini`에 URL을 적어두면
코드와 마이그레이션이 서로 다른 DB를 보게 되고, 그 사실은 마이그레이션이 "성공"한
뒤에야 드러난다.

다만 `Settings`를 그대로 쓰면 `GITHUB_TOKEN`·`ANTHROPIC_API_KEY`가 없을 때
`ValidationError`로 죽는다. **마이그레이션에는 그 키들이 필요 없다.** CI나 새로
받은 저장소에서 `alembic upgrade head`가 API 키 때문에 막히는 것은 말이 안 되므로,
설정을 읽되 실패하면 환경변수로 물러난다.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from pydantic import ValidationError
from sqlalchemy import Connection

from app.config import Settings, get_settings
from app.models import Base, UtcDateTime, create_db_engine

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers는 기본이 True다. 그대로 두면 알렘빅을 프로세스 안에서
    # 부르는 순간(테스트, 기동 시 자동 마이그레이션) 이미 만들어진 app.* 로거가 전부
    # 꺼진다. 로그가 사라지는 것으로만 드러나서 알아채기 어렵다.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 이 metadata가 --autogenerate의 비교 대상이다. `app.models`를 통째로 import하는
# 것이 중요하다. 모델 모듈을 하나씩 import하면 새 테이블을 추가한 사람이 여기에
# 등록하는 것을 빼먹고, 마이그레이션은 조용히 그 테이블을 만들지 않는다.
target_metadata = Base.metadata


def _database_url() -> str:
    """마이그레이션을 적용할 DB 접속 문자열을 정한다.

    우선순위: `alembic -x db_url=...` > `.env`/환경변수의 `DATABASE_URL` > 기본값.

    Returns:
        SQLAlchemy 접속 문자열.
    """
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override

    try:
        return get_settings().database_url
    except ValidationError:
        # API 키가 없는 환경(CI, 갓 클론한 저장소)에서도 마이그레이션은 돌아야 한다.
        # 기본값은 Settings에서 그대로 꺼내 온다. 여기에 다시 적으면 두 곳이 어긋난다.
        default = Settings.model_fields["database_url"].default
        return os.environ.get("DATABASE_URL") or str(default)


def _render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """커스텀 타입을 DDL 기준 타입으로 바꿔 적는다.

    `--autogenerate`는 `app.models.types.UtcDateTime(...)`이라고 적으려 하는데,
    그러면 **마이그레이션 스크립트가 애플리케이션 코드를 import하게 된다.**
    나중에 그 클래스를 옮기거나 이름을 바꾸는 순간 과거 마이그레이션이 전부
    ImportError로 죽는다. 마이그레이션은 몇 년 뒤에도 그대로 돌아야 하는 기록이라
    현재 코드에 묶으면 안 된다.

    `UtcDateTime`이 DB에 만드는 것은 `DateTime(timezone=True)` 그대로이고, UTC
    강제는 Python 쪽 변환일 뿐이라 DDL에는 차이가 없다.

    Args:
        type_: 렌더링 대상 종류(`"type"`, `"column"` 등).
        obj: 렌더링할 객체.
        autogen_context: 렌더링 컨텍스트. import 문을 추가하는 데 쓴다.

    Returns:
        스크립트에 적을 코드 문자열. 기본 렌더링에 맡기려면 `False`.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """온라인·오프라인 모드가 공유하는 컨텍스트 설정을 건다.

    `render_as_batch=True`가 SQLite에 필요하다. SQLite는
    `ALTER TABLE ... DROP CONSTRAINT`를 지원하지 않아서, 제약을 고치려면 테이블을
    새로 만들고 데이터를 옮기는 수밖에 없다. 이 옵션이 없으면 컬럼 하나를 고치는
    마이그레이션이 SQLite에서만 실패한다.

    Args:
        connection: 온라인 모드에서 사용할 연결.
        url: 오프라인 모드에서 SQL만 찍을 때 쓸 접속 문자열.
    """
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        render_as_batch=True,
        # 컬럼 타입이 바뀐 것도 잡는다. 기본값은 False라 UtcDateTime <-> DateTime
        # 같은 변경이 autogenerate에서 조용히 누락된다.
        compare_type=True,
        render_item=_render_item,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"},
    )


def run_migrations_offline() -> None:
    """DB에 붙지 않고 SQL 스크립트만 만든다."""
    _configure(url=_database_url())

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """실제 DB에 연결해 마이그레이션을 적용한다.

    엔진은 `app.models.create_db_engine`으로 만든다. 여기서 따로 만들면
    `PRAGMA foreign_keys=ON` 리스너와 인메모리 풀 설정이 빠진 채로 돈다.
    """
    engine = create_db_engine(_database_url())

    with engine.connect() as connection:
        _configure(connection=connection)

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
