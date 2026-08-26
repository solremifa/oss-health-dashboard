"""`app.models` 테스트 -- 제약이 **실제로 거부하는지** 확인한다.

선언한 제약이 DB에 만들어졌는지는 스키마를 읽어서 확인할 수 없다. SQLite는 FK를
기본으로 강제하지 않으므로 `FOREIGN KEY (...)` 문구가 테이블에 그대로 남아 있어도
고아 행이 들어간다. `Enum`도 인자 하나를 빠뜨리면 CHECK 없는 맨 VARCHAR가 되는데
DDL만 보면 알 수 없다.

그래서 이 파일은 **위반을 시도하고 거절당하는 것**만 검증한다. 거절되지 않으면
제약이 장식이라는 뜻이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Issue,
    IssueComment,
    IssueFirstResponse,
    IssueLabel,
    IssueState,
    SyncState,
)
from tests.conftest import TEST_REPO

_CREATED_AT = datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
_KST = timezone(timedelta(hours=9))


def _comment(issue_id: int, **overrides: object) -> IssueComment:
    """기본값이 채워진 코멘트를 만든다."""
    values: dict[str, object] = {
        "id": 900_001,
        "issue_id": issue_id,
        "body": "Thanks for the report -- fixed on main.",
        "created_at": _CREATED_AT + timedelta(hours=3),
        "author_login": "jlowin",
        "author_id": 153,
        "author_type": "User",
        "author_association": "MEMBER",
    }
    values.update(overrides)
    return IssueComment(**values)


# ---------------------------------------------------------------------------
# FK -- SQLite는 기본으로 강제하지 않는다
# ---------------------------------------------------------------------------


def test_foreign_keys_pragma_is_on_for_every_connection(engine: Engine):
    """연결마다 PRAGMA가 켜져 있어야 한다. 한 연결에서만 켜면 의미가 없다."""
    for _ in range(2):
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_label_for_unknown_issue_is_rejected(session: Session):
    """존재하지 않는 이슈에 라벨을 달 수 없다. PRAGMA가 꺼지면 이 테스트가 깨진다."""
    session.add(IssueLabel(issue_id=999_999, name="bug"))

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


def test_comment_for_unknown_issue_is_rejected(session: Session):
    """코멘트도 마찬가지다."""
    session.add(_comment(issue_id=999_999))

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


def test_deleting_an_issue_cascades_to_its_children(session: Session, make_issue):
    """이슈를 지우면 라벨·코멘트·첫 응답 기록도 함께 사라진다."""
    issue = make_issue()
    session.add(issue)
    session.flush()
    session.add_all(
        [
            IssueLabel(issue_id=issue.id, name="bug"),
            _comment(issue_id=issue.id),
            IssueFirstResponse(issue_id=issue.id, checked_at=_CREATED_AT),
        ]
    )
    session.flush()

    # ORM의 cascade가 아니라 DB의 ON DELETE CASCADE를 확인하려는 것이라
    # 세션을 거치지 않고 지운다.
    session.execute(text("DELETE FROM issues WHERE id = :id"), {"id": issue.id})

    assert session.scalars(select(IssueLabel)).all() == []
    assert session.scalars(select(IssueComment)).all() == []
    assert session.scalars(select(IssueFirstResponse)).all() == []


# ---------------------------------------------------------------------------
# NOT NULL · UNIQUE
# ---------------------------------------------------------------------------


def test_null_body_is_rejected(session: Session, make_issue):
    """`body`는 NOT NULL이다. API의 `body: null`은 빈 문자열로 정규화해서 넣는다."""
    session.add(make_issue(body=None))

    with pytest.raises(IntegrityError, match="NOT NULL"):
        session.flush()


def test_empty_body_is_accepted(session: Session, make_issue):
    """빈 문자열은 정상 값이다. NULL만 막는 것이지 본문 없는 이슈를 막는 게 아니다."""
    session.add(make_issue(body=""))
    session.flush()

    assert session.get(Issue, 3_288_000_001).body == ""


def test_same_issue_number_twice_in_one_repo_is_rejected(session: Session, make_issue):
    """같은 저장소 안에서 이슈 번호는 유일하다."""
    session.add(make_issue(id=1, number=100))
    session.flush()
    session.add(make_issue(id=2, number=100))

    with pytest.raises(IntegrityError, match="UNIQUE"):
        session.flush()


def test_same_issue_number_in_another_repo_is_allowed(session: Session, make_issue):
    """번호는 저장소 안에서만 유일하다. 저장소가 다르면 충돌이 아니다."""
    session.add(make_issue(id=1, number=100, repo_full_name="a/one"))
    session.add(make_issue(id=2, number=100, repo_full_name="b/two"))
    session.flush()

    assert len(session.scalars(select(Issue)).all()) == 2


def test_duplicate_label_on_one_issue_is_rejected(session: Session, make_issue):
    """(이슈, 라벨 이름)이 PK다. 같은 라벨을 두 번 붙일 수 없다."""
    issue = make_issue()
    session.add(issue)
    session.flush()
    session.add(IssueLabel(issue_id=issue.id, name="bug"))
    session.flush()
    session.add(IssueLabel(issue_id=issue.id, name="bug"))

    with pytest.raises(IntegrityError, match=r"UNIQUE|PRIMARY KEY"):
        session.flush()


# ---------------------------------------------------------------------------
# CHECK
# ---------------------------------------------------------------------------


def test_negative_comment_count_is_rejected(session: Session, make_issue):
    """음수 코멘트 수는 파싱이 틀렸다는 뜻이다."""
    session.add(make_issue(comments_count=-1))

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_zero_issue_number_is_rejected(session: Session, make_issue):
    """이슈 번호는 1부터다. 0은 필드를 잘못 읽었을 때 나오는 값이다."""
    session.add(make_issue(number=0))

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_empty_label_name_is_rejected(session: Session, make_issue):
    """빈 문자열 라벨은 GitHub에 존재할 수 없다."""
    issue = make_issue()
    session.add(issue)
    session.flush()
    session.add(IssueLabel(issue_id=issue.id, name=""))

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_unknown_issue_state_is_rejected(session: Session, make_issue):
    """enum 컬럼에 CHECK가 실제로 걸려 있어야 한다.

    `create_constraint=True`를 빠뜨리면 CHECK 없는 맨 VARCHAR가 되어 아무 문자열이나
    들어간다. ORM을 거치면 Python enum이 먼저 막아버려 그 사실이 드러나지 않으므로
    **원시 SQL로** 넣어본다.
    """
    session.add(make_issue())
    session.flush()

    with pytest.raises(IntegrityError, match="CHECK"):
        session.execute(text("UPDATE issues SET state = 'reopened' WHERE id = 3288000001"))


def test_issue_state_is_stored_as_value_not_member_name(session: Session, make_issue):
    """`values_callable`이 없으면 `"CLOSED"`(멤버 이름)가 저장된다.

    ORM으로 읽으면 어느 쪽이든 `IssueState.CLOSED`로 돌아와서 차이가 드러나지 않는다.
    저장된 문자열을 직접 확인해야 한다.
    """
    session.add(make_issue(state=IssueState.CLOSED))
    session.flush()

    stored = session.execute(text("SELECT state FROM issues WHERE id = 3288000001")).scalar_one()
    assert stored == "closed"


def test_first_response_requires_all_three_fields_together(session: Session, make_issue):
    """응답 시각만 있고 누가 응답했는지 모르는 행은 판정 로직의 버그다."""
    issue = make_issue()
    session.add(issue)
    session.flush()
    session.add(
        IssueFirstResponse(
            issue_id=issue.id,
            responded_at=_CREATED_AT + timedelta(hours=3),
            comment_id=None,
            responder_login=None,
            checked_at=_CREATED_AT,
        )
    )

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_first_response_row_can_record_no_response(session: Session, make_issue):
    """**행은 존재하고 `responded_at`만 NULL** -- 조사했지만 응답이 없었다는 뜻이다.

    "아직 조사 안 함"(행 없음)과 구분되어야 한다. 이 구분이 지표에서 "응답 없음
    건수"를 따로 보고할 수 있게 하는 근거다.
    """
    issue = make_issue()
    session.add(issue)
    session.flush()
    session.add(IssueFirstResponse(issue_id=issue.id, checked_at=_CREATED_AT))
    session.flush()

    stored = session.get(IssueFirstResponse, issue.id)
    assert stored is not None
    assert stored.responded_at is None


def test_etag_without_fingerprint_is_rejected(session: Session):
    """지문 없는 ETag는 쓸 수 없다. 어떤 요청에 대한 ETag인지 모르기 때문이다."""
    session.add(SyncState(repo_full_name=TEST_REPO, resource="issues", etag='W/"abc"'))

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_fingerprint_without_etag_is_rejected(session: Session):
    """반대 방향도 막는다. ETag 없는 지문은 의미가 없다."""
    session.add(
        SyncState(repo_full_name=TEST_REPO, resource="issues", request_fingerprint="deadbeef")
    )

    with pytest.raises(IntegrityError, match="CHECK"):
        session.flush()


def test_etag_with_fingerprint_is_accepted(session: Session):
    """짝이 맞으면 통과한다."""
    session.add(
        SyncState(
            repo_full_name=TEST_REPO,
            resource="issues",
            etag='W/"abc"',
            request_fingerprint="deadbeef",
        )
    )
    session.flush()

    stored = session.get(SyncState, (TEST_REPO, "issues"))
    assert stored is not None
    assert stored.etag == 'W/"abc"'


# ---------------------------------------------------------------------------
# UtcDateTime -- SQLite는 시간대를 저장하지 못한다
# ---------------------------------------------------------------------------


def test_naive_datetime_is_rejected_on_write(session: Session, make_issue):
    """naive datetime을 UTC로 **추측해서** 넣지 않는다. 거부한다."""
    session.add(make_issue(created_at=datetime(2026, 3, 1, 9, 15)))

    with pytest.raises(StatementError, match="naive"):
        session.flush()


def test_datetime_round_trips_as_aware_utc(session: Session, make_issue):
    """넣은 값과 꺼낸 값의 타입이 같아야 한다.

    맨 `DateTime`이었다면 SQLite에서 naive로 돌아와서, 지표 계산의
    `created_at >= cutoff` 비교가 터지거나 시간대만큼 밀린 채로 성립한다.
    """
    session.add(make_issue(created_at=_CREATED_AT))
    session.flush()
    session.expire_all()

    stored = session.get(Issue, 3_288_000_001)
    assert stored is not None
    assert stored.created_at.tzinfo is not None
    assert stored.created_at == _CREATED_AT


def test_non_utc_offset_is_normalized_to_utc(session: Session, make_issue):
    """다른 시간대로 들어와도 UTC로 맞춰 저장한다."""
    kst = datetime(2026, 3, 1, 18, 15, tzinfo=_KST)
    session.add(make_issue(created_at=kst))
    session.flush()
    session.expire_all()

    stored = session.get(Issue, 3_288_000_001)
    assert stored is not None
    assert stored.created_at == _CREATED_AT


# ---------------------------------------------------------------------------
# SAVEPOINT -- #7의 UNIQUE 위반 처리가 여기에 기댄다
# ---------------------------------------------------------------------------


def test_savepoint_rolls_back_only_the_failed_statement(
    session_factory: sessionmaker[Session], make_issue
):
    """제약 위반을 SAVEPOINT 안에서 되돌리면 세션을 계속 쓸 수 있어야 한다.

    pysqlite 드라이버가 트랜잭션을 임의로 여닫으면 SAVEPOINT가 의도대로 동작하지
    않는다. `app.models.db`가 그것을 끄고 `BEGIN`을 직접 내는 이유가 이 동작이다.
    """
    with session_factory() as session:
        session.add(make_issue(id=1, number=100))
        session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(make_issue(id=2, number=100))
            session.flush()

        # SAVEPOINT 밖의 작업은 살아 있고, 세션은 계속 쓸 수 있다.
        session.add(make_issue(id=3, number=101))
        session.flush()
        session.commit()

    with session_factory() as session:
        numbers = sorted(issue.number for issue in session.scalars(select(Issue)))
        assert numbers == [100, 101]
