"""pytest 공통 픽스처."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    Issue,
    IssueState,
    create_db_engine,
    create_session_factory,
)

# Settings가 읽는 환경변수 전부. 실제 셸이나 CI에 값이 설정돼 있으면 테스트가
# 그 값을 주워 읽어 결과가 환경에 따라 달라진다. 매 테스트마다 비운다.
_SETTINGS_ENV_VARS = (
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "TARGET_REPO",
    "WINDOW_DAYS",
    "STALE_DAYS",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch):
    """설정 관련 환경변수를 비운 상태에서 테스트를 시작한다."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def required_settings() -> dict[str, str]:
    """필수 값만 채운 최소 설정 딕셔너리."""
    return {
        "github_token": "ghp_test_token",
        "anthropic_api_key": "sk-ant-test-key",
    }


# ---------------------------------------------------------------------------
# DB 픽스처
# ---------------------------------------------------------------------------

TEST_REPO = "PrefectHQ/fastmcp"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """테이블이 만들어진 인메모리 SQLite 엔진.

    `create_db_engine`을 거치는 것이 중요하다. 여기서 `create_engine`을 직접
    부르면 `PRAGMA foreign_keys=ON` 리스너와 `StaticPool` 설정이 빠진 채로
    돌아가고, **FK 제약을 검증하는 테스트가 전부 통과해버린다.**
    """
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """테스트용 세션 팩토리."""
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """테스트용 세션 하나."""
    with session_factory() as session:
        yield session


@pytest.fixture
def make_issue():
    """기본값이 채워진 `Issue`를 만드는 팩토리.

    테스트마다 필요한 컬럼만 덮어쓴다. 제약 위반을 검증할 때 "다른 컬럼이 비어서
    실패한 것"과 헷갈리지 않도록 나머지는 항상 유효한 값으로 둔다.
    """

    def _make(**overrides: Any) -> Issue:
        values: dict[str, Any] = {
            "id": 3_288_000_001,
            "repo_full_name": TEST_REPO,
            "number": 3288,
            "title": "Client hangs when server closes the stream",
            "body": "Steps to reproduce:\n1. start the server\n",
            "state": IssueState.CLOSED,
            "state_reason": "completed",
            "created_at": datetime(2026, 3, 1, 9, 15, tzinfo=UTC),
            "updated_at": datetime(2026, 3, 3, 11, 20, tzinfo=UTC),
            "closed_at": datetime(2026, 3, 3, 11, 20, tzinfo=UTC),
            "comments_count": 4,
            "author_login": "someone",
            "author_id": 4242,
            "author_type": "User",
            "author_association": "NONE",
        }
        values.update(overrides)
        return Issue(**values)

    return _make
