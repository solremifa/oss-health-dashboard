"""SQLAlchemy ORM 모델, Pydantic 스키마, 저장/조회 로직.

최하단 레이어다. 다른 어떤 레이어도 import하지 않는다.

넣지 않는 것: 집계 로직, 외부 API 호출, HTTP 관심사.

이 패키지를 import하면 모든 테이블이 `Base.metadata`에 등록된다. Alembic의
`env.py`와 테스트의 `create_all()`이 둘 다 이 사실에 기댄다 -- 모델 모듈을
개별로 import하게 두면 새 테이블을 추가한 사람이 한쪽에만 등록하는 일이 생긴다.
"""

from __future__ import annotations

from app.models.base import NAMING_CONVENTION, Base
from app.models.db import create_db_engine, create_session_factory
from app.models.enums import IssueState, sa_enum
from app.models.tables import (
    Issue,
    IssueComment,
    IssueFirstResponse,
    IssueLabel,
    SyncState,
)
from app.models.types import UtcDateTime

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Issue",
    "IssueComment",
    "IssueFirstResponse",
    "IssueLabel",
    "IssueState",
    "SyncState",
    "UtcDateTime",
    "create_db_engine",
    "create_session_factory",
    "sa_enum",
]
