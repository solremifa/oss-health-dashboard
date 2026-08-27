"""`app.models.repository` 테스트 — upsert와 증분 수집 커서.

## 파일 DB를 쓰는 테스트가 섞여 있다

인메모리 SQLite는 `StaticPool`로 **연결 하나를 공유**한다. 그래서 세션을 두 개
만들어도 같은 트랜잭션 안에 있고, "커밋하지 않았으니 다른 세션에는 안 보인다"를
확인할 수 없다. 트랜잭션 경계가 걸린 테스트만 `tmp_path`의 파일 DB를 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AnalysisRecord,
    Base,
    CommentRecord,
    ConflictingRecordError,
    FirstResponseRecord,
    Issue,
    IssueAnalysis,
    IssueCategory,
    IssueComment,
    IssueFirstResponse,
    IssueLabel,
    IssueRecord,
    IssueSentiment,
    IssueState,
    SyncCursor,
    UpsertOutcome,
    create_db_engine,
    create_session_factory,
    load_analysis,
    load_first_response,
    load_sync_cursor,
    save_sync_cursor,
    upsert_analysis,
    upsert_comment,
    upsert_first_response,
    upsert_issue,
)

REPO = "PrefectHQ/fastmcp"
CREATED_AT = datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
UPDATED_AT = datetime(2026, 3, 3, 11, 20, tzinfo=UTC)


def _record(**overrides: Any) -> IssueRecord:
    """기본값이 채워진 저장용 값 객체."""
    values: dict[str, Any] = {
        "id": 3_288_000_001,
        "repo_full_name": REPO,
        "number": 3288,
        "title": "Client hangs when server closes the stream",
        "body": "Steps to reproduce:\n1. start the server\n",
        "state": IssueState.OPEN,
        "state_reason": None,
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
        "closed_at": None,
        "comments_count": 4,
        "author_login": "someone",
        "author_id": 4242,
        "author_type": "User",
        "author_association": "NONE",
        "labels": ("bug", "server"),
    }
    values.update(overrides)
    return IssueRecord(**values)


@pytest.fixture
def file_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """연결을 공유하지 않는 파일 기반 DB의 세션 팩토리."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'repo.db'}")
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _issue_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Issue)) or 0


def _labels_of(session: Session, issue_id: int) -> list[str]:
    return sorted(
        session.scalars(select(IssueLabel.name).where(IssueLabel.issue_id == issue_id)).all()
    )


# ---------------------------------------------------------------------------
# upsert — 같은 이슈를 두 번 수집해도 한 행
# ---------------------------------------------------------------------------


def test_first_upsert_inserts(session: Session):
    outcome = upsert_issue(session, _record())

    assert outcome is UpsertOutcome.INSERTED
    assert _issue_count(session) == 1


def test_upserting_the_same_issue_twice_keeps_one_row(session: Session):
    """`since`가 포함 경계라 경계에 걸친 이슈는 매 회차 다시 온다. 멱등해야 한다."""
    upsert_issue(session, _record())
    outcome = upsert_issue(session, _record())

    assert outcome is UpsertOutcome.UPDATED
    assert _issue_count(session) == 1


def test_upsert_applies_the_new_values(session: Session):
    """두 번째 수집은 갱신이다. 이슈가 닫히거나 코멘트가 늘어난 것이 반영돼야 한다."""
    upsert_issue(session, _record())
    closed_at = UPDATED_AT + timedelta(days=1)

    upsert_issue(
        session,
        _record(
            state=IssueState.CLOSED,
            state_reason="completed",
            closed_at=closed_at,
            comments_count=9,
            updated_at=closed_at,
        ),
    )

    stored = session.get(Issue, 3_288_000_001)
    assert stored is not None
    assert stored.state is IssueState.CLOSED
    assert stored.state_reason == "completed"
    assert stored.closed_at == closed_at
    assert stored.comments_count == 9


def test_upsert_does_not_check_before_inserting(session: Session, monkeypatch):
    """저장 전 SELECT로 존재를 확인하지 않는다.

    확인과 삽입 사이에 다른 트랜잭션이 끼어들면 둘 다 "없다"고 보고 둘 다 INSERT한다.
    제약이 최종 판정자다. 여기서는 upsert가 조회로 시작하지 않는다는 것을,
    **아무것도 없는 상태에서 곧바로 INSERT가 성공하는 것**으로 확인한다.
    """
    executed: list[str] = []
    original = Session.execute

    def _spy(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(type(statement).__name__)
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _spy)
    upsert_issue(session, _record())

    assert executed[0] == "Insert"
    assert "Select" not in executed


# ---------------------------------------------------------------------------
# 라벨 — 정답지를 매 회차 현재 상태로 맞춘다
# ---------------------------------------------------------------------------


def test_labels_are_stored(session: Session):
    upsert_issue(session, _record(labels=("bug", "server")))

    assert _labels_of(session, 3_288_000_001) == ["bug", "server"]


def test_removed_labels_disappear_on_the_next_sync(session: Session):
    """라벨이 떨어진 것도 반영돼야 한다. 붙은 것만 넣으면 정답지가 낡는다."""
    upsert_issue(session, _record(labels=("bug", "server")))
    upsert_issue(session, _record(labels=("enhancement",)))

    assert _labels_of(session, 3_288_000_001) == ["enhancement"]


def test_all_labels_can_be_removed(session: Session):
    upsert_issue(session, _record(labels=("bug",)))
    upsert_issue(session, _record(labels=()))

    assert _labels_of(session, 3_288_000_001) == []


def test_repeated_sync_does_not_duplicate_labels(session: Session):
    """(이슈, 이름)이 PK라 중복은 애초에 들어가지 않는다. 갈아끼우기가 그 위에서 돈다."""
    for _ in range(3):
        upsert_issue(session, _record(labels=("bug", "server")))

    assert _labels_of(session, 3_288_000_001) == ["bug", "server"]


# ---------------------------------------------------------------------------
# UNIQUE 위반만 처리한다
# ---------------------------------------------------------------------------


def test_not_null_violation_is_not_swallowed(session: Session):
    """NOT NULL 위반을 갱신 시도로 바꾸면 진짜 버그가 조용히 사라진다."""
    with pytest.raises(IntegrityError, match="NOT NULL"):
        upsert_issue(session, _record(body=None))


def test_check_violation_is_not_swallowed(session: Session):
    with pytest.raises(IntegrityError, match="CHECK"):
        upsert_issue(session, _record(comments_count=-1))


def test_same_number_with_a_different_id_is_reported(session: Session):
    """PK가 아닌 UNIQUE가 충돌하면 갱신 대상이 0행이다. 조용히 넘기지 않는다."""
    upsert_issue(session, _record(id=1, number=100))

    with pytest.raises(ConflictingRecordError, match="issues"):
        upsert_issue(session, _record(id=2, number=100))


def test_session_survives_a_handled_unique_violation(session: Session):
    """SAVEPOINT 안에서 되돌리기 때문에 이후 저장이 계속된다.

    그냥 잡으면 실패한 문장이 트랜잭션 전체를 무효화해서, 수집 489건 중 두 번째
    이슈부터 전부 실패한다.
    """
    upsert_issue(session, _record(id=1, number=100))
    upsert_issue(session, _record(id=1, number=100))  # UNIQUE 위반 -> 갱신
    upsert_issue(session, _record(id=2, number=101))
    session.flush()

    assert _issue_count(session) == 2


# ---------------------------------------------------------------------------
# sync_state — ETag와 지문, since 커서
# ---------------------------------------------------------------------------


def test_missing_cursor_reads_as_none(session: Session):
    assert load_sync_cursor(session, REPO, "issues") is None


def test_cursor_round_trips(session: Session):
    cursor = SyncCursor(
        etag='W/"abc"',
        request_fingerprint="deadbeef",
        since_cursor=UPDATED_AT,
        last_synced_at=UPDATED_AT + timedelta(hours=1),
    )

    save_sync_cursor(session, REPO, "issues", cursor)
    session.flush()

    assert load_sync_cursor(session, REPO, "issues") == cursor


def test_saving_the_cursor_twice_updates_in_place(session: Session):
    save_sync_cursor(session, REPO, "issues", SyncCursor(since_cursor=UPDATED_AT))
    advanced = SyncCursor(
        etag='W/"next"',
        request_fingerprint="cafe",
        since_cursor=UPDATED_AT + timedelta(days=1),
    )

    outcome = save_sync_cursor(session, REPO, "issues", advanced)
    session.flush()

    assert outcome is UpsertOutcome.UPDATED
    assert load_sync_cursor(session, REPO, "issues") == advanced


def test_cursors_are_kept_per_repo_and_resource(session: Session):
    """저장소와 리소스마다 커서가 따로다. 하나로 합치면 서로를 덮어쓴다."""
    save_sync_cursor(session, "a/one", "issues", SyncCursor(since_cursor=CREATED_AT))
    save_sync_cursor(session, "b/two", "issues", SyncCursor(since_cursor=UPDATED_AT))
    session.flush()

    first = load_sync_cursor(session, "a/one", "issues")
    second = load_sync_cursor(session, "b/two", "issues")
    assert first is not None
    assert second is not None
    assert first.since_cursor == CREATED_AT
    assert second.since_cursor == UPDATED_AT


# ---------------------------------------------------------------------------
# issue_comments · issue_first_responses — 행의 존재가 "조사 완료"다 (#8)
# ---------------------------------------------------------------------------


def _comment(**overrides: Any) -> CommentRecord:
    """기본값이 채워진 코멘트 값 객체."""
    values: dict[str, Any] = {
        "id": 9_000_001,
        "issue_id": 3_288_000_001,
        "body": "looking into it",
        "created_at": CREATED_AT + timedelta(hours=2),
        "author_login": "jlowin",
        "author_id": 153,
        "author_type": "User",
        "author_association": "MEMBER",
    }
    values.update(overrides)
    return CommentRecord(**values)


def test_comment_round_trips(session: Session):
    upsert_issue(session, _record())

    outcome = upsert_comment(session, _comment())
    session.flush()

    assert outcome is UpsertOutcome.INSERTED
    stored = session.get(IssueComment, 9_000_001)
    assert stored is not None
    assert stored.author_type == "User"
    assert stored.created_at == CREATED_AT + timedelta(hours=2)


def test_recollecting_the_same_comment_keeps_one_row(session: Session):
    """같은 이슈를 다시 수집하면 같은 코멘트가 다시 온다. 행이 늘면 안 된다."""
    upsert_issue(session, _record())
    upsert_comment(session, _comment())
    upsert_comment(session, _comment(body="edited"))
    session.flush()

    assert session.scalar(select(func.count()).select_from(IssueComment)) == 1
    stored = session.get(IssueComment, 9_000_001)
    assert stored is not None
    assert stored.body == "edited"


def test_comment_for_an_unknown_issue_is_rejected(session: Session):
    """FK 위반은 UNIQUE 위반과 달리 그대로 올라와야 한다."""
    with pytest.raises(IntegrityError):
        upsert_comment(session, _comment(issue_id=999_999))
        session.flush()


def test_unchecked_issue_has_no_row(session: Session):
    """행이 없다 = 아직 조사하지 않았다."""
    upsert_issue(session, _record())
    session.flush()

    assert load_first_response(session, 3_288_000_001) is None


def test_no_response_is_stored_as_a_row(session: Session):
    """조사했는데 응답이 없었던 것은 결측이 아니다. 행으로 남는다."""
    upsert_issue(session, _record())

    upsert_first_response(
        session, FirstResponseRecord(issue_id=3_288_000_001, checked_at=UPDATED_AT)
    )
    session.flush()

    loaded = load_first_response(session, 3_288_000_001)
    assert loaded is not None
    assert loaded.responded is False
    assert loaded.responded_at is None
    assert loaded.checked_at == UPDATED_AT


def test_first_response_round_trips(session: Session):
    upsert_issue(session, _record())
    record = FirstResponseRecord(
        issue_id=3_288_000_001,
        checked_at=UPDATED_AT,
        responded_at=CREATED_AT + timedelta(hours=2),
        comment_id=9_000_001,
        responder_login="jlowin",
    )

    upsert_first_response(session, record)
    session.flush()

    assert load_first_response(session, 3_288_000_001) == record


def test_recheck_overwrites_the_previous_verdict(session: Session):
    """판별 규칙이 바뀌면 다시 조사한다. 이슈당 행은 하나다."""
    upsert_issue(session, _record())
    upsert_first_response(
        session,
        FirstResponseRecord(
            issue_id=3_288_000_001,
            checked_at=UPDATED_AT,
            responded_at=CREATED_AT + timedelta(hours=2),
            comment_id=9_000_001,
            responder_login="coderabbitai[bot]",
        ),
    )

    outcome = upsert_first_response(
        session, FirstResponseRecord(issue_id=3_288_000_001, checked_at=UPDATED_AT)
    )
    session.flush()

    assert outcome is UpsertOutcome.UPDATED
    assert session.scalar(select(func.count()).select_from(IssueFirstResponse)) == 1
    loaded = load_first_response(session, 3_288_000_001)
    assert loaded is not None
    assert loaded.responded_at is None
    assert loaded.comment_id is None
    assert loaded.responder_login is None


def test_partially_filled_verdict_is_rejected_before_it_reaches_the_db():
    """응답 시각만 있고 응답자를 모르는 상태는 데이터가 아니라 판정 로직의 버그다."""
    with pytest.raises(ValueError, match="함께 있거나 함께 없어야"):
        FirstResponseRecord(
            issue_id=3_288_000_001,
            checked_at=UPDATED_AT,
            responded_at=CREATED_AT,
        )


def test_the_db_check_also_rejects_a_partial_verdict(session: Session):
    """값 객체를 우회해도 DB가 막는다. 무결성의 최종 판정자는 제약이다."""
    upsert_issue(session, _record())
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            insert(IssueFirstResponse).values(
                issue_id=3_288_000_001,
                responded_at=CREATED_AT,
                comment_id=None,
                responder_login=None,
                checked_at=UPDATED_AT,
            )
        )
        session.flush()


# ---------------------------------------------------------------------------
# issue_analyses — 무엇으로 분석했는지가 함께 남는다 (#9)
# ---------------------------------------------------------------------------


def _analysis(**overrides: Any) -> AnalysisRecord:
    """기본값이 채워진 분류 결과."""
    values: dict[str, Any] = {
        "issue_id": 3_288_000_001,
        "category": IssueCategory.BUG,
        "sentiment": IssueSentiment.NEUTRAL,
        "model": "claude-opus-5",
        "prompt_version": "1",
        "analyzed_at": UPDATED_AT,
    }
    values.update(overrides)
    return AnalysisRecord(**values)


def test_analysis_round_trips(session: Session):
    upsert_issue(session, _record())

    outcome = upsert_analysis(session, _analysis())
    session.flush()

    assert outcome is UpsertOutcome.INSERTED
    assert load_analysis(session, 3_288_000_001) == _analysis()


def test_analysis_for_an_unknown_issue_is_rejected(session: Session):
    """FK 위반은 그대로 올라와야 한다. 분석 결과만 떠 있는 행은 만들지 않는다."""
    with pytest.raises(IntegrityError):
        upsert_analysis(session, _analysis(issue_id=999_999))
        session.flush()


def test_provenance_cannot_be_blank(session: Session):
    """model과 prompt_version이 비면 나중에 섞인 데이터를 구분할 수 없다."""
    with pytest.raises(ValueError, match="model은 비어 있을 수 없습니다"):
        _analysis(model="")

    with pytest.raises(ValueError, match="prompt_version은 비어 있을 수 없습니다"):
        _analysis(prompt_version="")


def test_the_db_also_rejects_blank_provenance(session: Session):
    """값 객체를 우회해도 CHECK가 막는다. 무결성의 최종 판정자는 제약이다."""
    upsert_issue(session, _record())
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            insert(IssueAnalysis).values(
                issue_id=3_288_000_001,
                category=IssueCategory.BUG,
                sentiment=IssueSentiment.NEUTRAL,
                model="",
                prompt_version="1",
                analyzed_at=UPDATED_AT,
            )
        )
        session.flush()


def test_an_unknown_category_is_rejected_by_the_check_constraint(session: Session):
    """enum · 프롬프트 · 스키마가 어긋나 낯선 값이 흘러들어도 DB에서 멈춘다."""
    upsert_issue(session, _record())
    session.flush()

    with pytest.raises(IntegrityError):
        session.execute(
            insert(IssueAnalysis).values(
                issue_id=3_288_000_001,
                category="documentation",
                sentiment="neutral",
                model="claude-opus-5",
                prompt_version="1",
                analyzed_at=UPDATED_AT,
            )
        )
        session.flush()


# ---------------------------------------------------------------------------
# 트랜잭션 경계 — repository는 커밋하지 않는다
# ---------------------------------------------------------------------------


def test_repository_does_not_commit(file_session_factory: sessionmaker[Session]):
    """커밋은 호출자의 몫이다. 여기서 커밋하면 "저장은 됐는데 커서는 안 갔다"가 생긴다."""
    with file_session_factory() as writer:
        upsert_issue(writer, _record())
        save_sync_cursor(writer, REPO, "issues", SyncCursor(since_cursor=UPDATED_AT))
        writer.flush()

        with file_session_factory() as reader:
            assert _issue_count(reader) == 0
            assert load_sync_cursor(reader, REPO, "issues") is None

        writer.rollback()


def test_a_failed_run_leaves_no_trace_and_the_next_run_resumes(
    file_session_factory: sessionmaker[Session],
):
    """중간에 실패한 회차는 통째로 사라지고, 다음 회차가 같은 구간을 다시 받는다.

    이슈 저장과 커서 전진이 **같은 트랜잭션**이라 가능한 일이다. 저장 시점마다
    커밋했다면 커서만 앞서 나가고 빠진 구간을 다시는 받지 않는 상태가 생긴다.
    """
    first_batch = [_record(id=1, number=1), _record(id=2, number=2)]
    resumed_batch = [*first_batch, _record(id=3, number=3)]

    # 1회차: 두 건을 저장하고 커서까지 옮겼지만 마지막에 실패한다.
    with file_session_factory() as session:
        for record in first_batch:
            upsert_issue(session, record)
        save_sync_cursor(session, REPO, "issues", SyncCursor(since_cursor=UPDATED_AT))
        session.flush()
        session.rollback()

    with file_session_factory() as session:
        assert _issue_count(session) == 0
        # 커서가 그대로여야 다음 회차가 같은 구간을 다시 받는다.
        assert load_sync_cursor(session, REPO, "issues") is None

    # 2회차: 같은 구간을 다시 받아 저장한다. 1회차와 겹치는 두 건이 중복되지 않는다.
    with file_session_factory() as session:
        for record in resumed_batch:
            upsert_issue(session, record)
        save_sync_cursor(session, REPO, "issues", SyncCursor(since_cursor=UPDATED_AT))
        session.commit()

    with file_session_factory() as session:
        assert _issue_count(session) == 3
        cursor = load_sync_cursor(session, REPO, "issues")
        assert cursor is not None
        assert cursor.since_cursor == UPDATED_AT
