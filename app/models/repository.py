"""저장·조회 함수. **커밋하지 않는다.**

트랜잭션 경계는 호출자가 정한다(`CLAUDE.md` 7절). 여기서 커밋해버리면 "이슈 489건을
받아서 전부 저장한 뒤 커서를 전진시킨다"는 한 덩어리를 쪼갤 수 없다 -- 중간에
실패했을 때 일부만 저장된 채로 커서가 앞서 나가고, 다음 회차가 빠진 구간을 다시
받지 않는다.

## upsert를 하는 방법

**저장 전에 SELECT로 있는지 확인하지 않는다.** 확인과 삽입 사이에 다른 트랜잭션이
끼어들면 둘 다 "없다"고 보고 둘 다 INSERT한다. 제약이 최종 판정자다.

INSERT를 시도하고, UNIQUE 위반이면 UPDATE로 바꾼다. `INSERT ... ON CONFLICT`를 쓰지
않는 이유는 그 구문이 방언마다 다르기 때문이다 -- SQLite와 PostgreSQL에서 import
경로가 갈리고, SQLite 전용 `INSERT OR REPLACE`는 금지되어 있다(`CLAUDE.md` 3절).
"실패하면 갱신"은 양쪽에서 같은 코드로 돈다.

**INSERT는 반드시 `session.begin_nested()`(SAVEPOINT) 안에서 한다.** 그냥 잡으면
실패한 문장이 트랜잭션 전체를 무효화해서 이후 저장이 전부 실패한다. SQLite에서
SAVEPOINT가 제대로 동작하려면 드라이버 설정이 필요한데, 그건 `app.models.db`가
연결 시점에 처리한다.

**UNIQUE 위반만 처리한다.** FK·NOT NULL·CHECK 위반은 그대로 올린다. 뭉뚱그려 잡으면
"참조하는 이슈가 없다"거나 "본문이 NULL이다" 같은 진짜 버그가 갱신 시도로 둔갑해
조용히 사라진다.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.base import Base
from app.models.errors import ConflictingRecordError
from app.models.records import (
    AnalysisRecord,
    CommentRecord,
    FirstResponseRecord,
    IssueFacts,
    IssueRecord,
    SyncCursor,
)
from app.models.tables import (
    Issue,
    IssueAnalysis,
    IssueComment,
    IssueFirstResponse,
    IssueLabel,
    SyncState,
)

logger = get_logger(__name__)

# PostgreSQL의 unique_violation. psycopg가 `sqlstate`로 노출한다.
_UNIQUE_VIOLATION_SQLSTATE = "23505"

# SQLite는 SQLSTATE가 없어 메시지를 본다. PK 위반도 같은 문구로 온다.
_SQLITE_UNIQUE_MARKERS = ("UNIQUE constraint failed",)


class UpsertOutcome(Enum):
    """upsert가 실제로 한 일."""

    INSERTED = "inserted"
    UPDATED = "updated"


def is_unique_violation(error: IntegrityError) -> bool:
    """UNIQUE(또는 PK) 위반인지 판별한다.

    DB마다 신호가 다르다. PostgreSQL은 SQLSTATE `23505`를 주고, SQLite는 코드가
    없어 메시지를 봐야 한다. 메시지를 보는 쪽을 나중에 잊지 않도록 한 곳에 모은다.

    Args:
        error: SQLAlchemy가 감싼 무결성 오류.

    Returns:
        UNIQUE 제약 위반이면 `True`. NOT NULL·FK·CHECK 위반이면 `False`.
    """
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate is not None:
        return str(sqlstate) == _UNIQUE_VIOLATION_SQLSTATE

    message = str(original)
    return any(marker in message for marker in _SQLITE_UNIQUE_MARKERS)


def _insert_or_update(
    session: Session,
    model: type[Base],
    *,
    key: dict[str, Any],
    values: dict[str, Any],
) -> UpsertOutcome:
    """INSERT를 시도하고 UNIQUE 위반이면 UPDATE로 바꾼다.

    Args:
        session: 사용할 세션. 커밋하지 않는다.
        model: 대상 ORM 모델.
        key: 갱신 대상을 특정하는 컬럼 값(보통 PK).
        values: 삽입할 전체 컬럼 값. `key`를 포함한다.

    Returns:
        실제로 삽입했는지 갱신했는지.

    Raises:
        IntegrityError: UNIQUE 이외의 제약을 위반한 경우. 그대로 올린다.
        ConflictingRecordError: UNIQUE 위반인데 `key`로 갱신할 행이 없는 경우.
    """
    try:
        with session.begin_nested():
            session.execute(insert(model).values(**values))
    except IntegrityError as error:
        if not is_unique_violation(error):
            raise

        changes = {column: value for column, value in values.items() if column not in key}
        statement = update(model)
        for column, value in key.items():
            statement = statement.where(getattr(model, column) == value)

        result = session.execute(statement.values(**changes))
        if result.rowcount == 0:
            raise ConflictingRecordError(model.__tablename__, key) from error
        return UpsertOutcome.UPDATED

    return UpsertOutcome.INSERTED


def upsert_issue(session: Session, record: IssueRecord) -> UpsertOutcome:
    """이슈 한 건을 저장하거나 갱신한다. 라벨도 함께 맞춘다.

    같은 이슈를 두 번 수집해도 행은 하나다. `since`가 포함(inclusive) 경계라
    경계에 걸친 이슈는 매 회차 다시 오는데, 그게 정상 동작이 되려면 이 함수가
    멱등이어야 한다.

    Args:
        session: 사용할 세션. **커밋하지 않는다.**
        record: 저장할 이슈.

    Returns:
        실제로 삽입했는지 갱신했는지.

    Raises:
        IntegrityError: UNIQUE 이외의 제약을 위반한 경우.
        ConflictingRecordError: 같은 `(repo_full_name, number)`에 다른 `id`가 온 경우.
    """
    outcome = _insert_or_update(
        session,
        Issue,
        key={"id": record.id},
        values=record.column_values(),
    )
    _replace_labels(session, record.id, record.labels)
    return outcome


def _replace_labels(session: Session, issue_id: int, labels: Sequence[str]) -> None:
    """이슈의 라벨을 통째로 갈아끼운다.

    차이를 계산해서 붙은 것만 넣고 떨어진 것만 지우는 방법도 있지만, 이슈 하나에
    라벨은 많아야 몇 개다. **지우고 다시 넣는 쪽이 "라벨이 제거된 경우"를 따로
    처리하지 않아도 되어 틀릴 여지가 적다.** 같은 트랜잭션 안이라 중간 상태가
    밖에서 보이지도 않는다.

    Args:
        session: 사용할 세션.
        issue_id: 대상 이슈.
        labels: 새 라벨 이름. 순서는 유지하지만 의미는 없다.
    """
    session.execute(delete(IssueLabel).where(IssueLabel.issue_id == issue_id))
    if labels:
        session.execute(
            insert(IssueLabel),
            [{"issue_id": issue_id, "name": name} for name in labels],
        )


def upsert_comment(session: Session, record: CommentRecord) -> UpsertOutcome:
    """코멘트 한 건을 저장하거나 갱신한다.

    같은 이슈를 다시 수집하면 같은 코멘트가 다시 온다. 판정 근거를 남기는 것이
    목적이라 행이 늘어나면 안 되고, 그래서 여기서도 upsert다.

    Args:
        session: 사용할 세션. **커밋하지 않는다.**
        record: 저장할 코멘트.

    Returns:
        실제로 삽입했는지 갱신했는지.

    Raises:
        IntegrityError: 참조하는 이슈가 없는 등 UNIQUE 이외의 제약을 위반한 경우.
    """
    return _insert_or_update(
        session,
        IssueComment,
        key={"id": record.id},
        values=record.column_values(),
    )


def upsert_first_response(session: Session, record: FirstResponseRecord) -> UpsertOutcome:
    """메인테이너 첫 응답 판정 결과를 저장하거나 갱신한다.

    **행의 존재 자체가 "조사 완료"를 뜻한다.** 응답이 없었던 경우에도 저장을
    건너뛰지 않는다. 건너뛰면 "아직 조사 안 함"과 구분되지 않아, 재조사 대상을
    고를 때 영원히 다시 조사하게 된다.

    Args:
        session: 사용할 세션. **커밋하지 않는다.**
        record: 저장할 판정 결과.

    Returns:
        실제로 삽입했는지 갱신했는지.

    Raises:
        IntegrityError: 참조하는 이슈가 없는 등 UNIQUE 이외의 제약을 위반한 경우.
    """
    return _insert_or_update(
        session,
        IssueFirstResponse,
        key={"issue_id": record.issue_id},
        values=record.column_values(),
    )


def load_first_response(session: Session, issue_id: int) -> FirstResponseRecord | None:
    """저장된 첫 응답 판정 결과를 읽는다.

    Args:
        session: 사용할 세션.
        issue_id: 대상 이슈.

    Returns:
        판정 결과. **아직 조사하지 않았으면 `None`.** 조사했지만 메인테이너
        응답이 없었던 경우는 `None`이 아니라 `responded_at is None`인 값이다.
    """
    row = session.get(IssueFirstResponse, issue_id)
    if row is None:
        return None
    return FirstResponseRecord(
        issue_id=row.issue_id,
        checked_at=row.checked_at,
        responded_at=row.responded_at,
        comment_id=row.comment_id,
        responder_login=row.responder_login,
    )


def upsert_analysis(session: Session, record: AnalysisRecord) -> UpsertOutcome:
    """LLM 분류 결과를 저장하거나 갱신한다.

    **실패한 분석은 여기에 오지 않는다.** 분석에 실패한 이슈는 행이 생기지 않고
    미분석으로 남으며, 그 건수는 지표 응답에 드러난다. 실패를 "기타"로 저장해
    성공한 것처럼 만들지 않는다.

    Args:
        session: 사용할 세션. **커밋하지 않는다.**
        record: 저장할 분류 결과.

    Returns:
        실제로 삽입했는지 갱신했는지.

    Raises:
        IntegrityError: 참조하는 이슈가 없는 등 UNIQUE 이외의 제약을 위반한 경우.
    """
    return _insert_or_update(
        session,
        IssueAnalysis,
        key={"issue_id": record.issue_id},
        values=record.column_values(),
    )


def load_analysis(session: Session, issue_id: int) -> AnalysisRecord | None:
    """저장된 분류 결과를 읽는다.

    Args:
        session: 사용할 세션.
        issue_id: 대상 이슈.

    Returns:
        분류 결과. 아직 분석하지 않았거나 분석에 실패했으면 `None`.
    """
    row = session.get(IssueAnalysis, issue_id)
    if row is None:
        return None
    return AnalysisRecord(
        issue_id=row.issue_id,
        category=row.category,
        sentiment=row.sentiment,
        model=row.model,
        prompt_version=row.prompt_version,
        analyzed_at=row.analyzed_at,
    )


def load_issue_facts(session: Session, repo_full_name: str) -> list[IssueFacts]:
    """지표 계산에 필요한 사실을 이슈 단위로 모아 읽는다.

    ## 왜 OUTER JOIN인가

    첫 응답 판정과 LLM 분석은 **이슈보다 늦게, 따로 채워진다.** INNER JOIN을 쓰면
    아직 조사·분석되지 않은 이슈가 결과에서 통째로 사라지고, 그러면 지표는 이미
    처리된 이슈만 보고 계산된다 -- 미분석 건수가 0으로 보이고 분모도 함께 줄어
    **모든 비율이 그럴듯하게 맞는 것처럼 나온다.**

    ## 기간으로 자르지 않는다

    `WINDOW_DAYS`로 거르는 일은 집계(`app/analysis/metrics.py`)가 한다. 여기서도
    자르면 경계 규칙이 SQL과 파이썬 두 곳에 생기고, 한쪽만 고치는 순간 어긋난다.
    대상 저장소 한 곳의 이슈는 실측 기준 수천 건 규모라 전량을 읽어도 부담이 없다
    (`docs/findings.md` 2절).

    Args:
        session: 사용할 세션.
        repo_full_name: 대상 저장소.

    Returns:
        `created_at` 오름차순 이슈별 사실. 저장된 이슈가 없으면 빈 목록.
    """
    statement = (
        select(
            Issue.id,
            Issue.created_at,
            Issue.state,
            # 행의 존재 여부가 "조사 완료"를 뜻한다. 값이 아니라 NULL 여부를 본다.
            IssueFirstResponse.issue_id.label("checked_issue_id"),
            IssueFirstResponse.responded_at,
            IssueAnalysis.category,
            IssueAnalysis.sentiment,
        )
        .outerjoin(IssueFirstResponse, IssueFirstResponse.issue_id == Issue.id)
        .outerjoin(IssueAnalysis, IssueAnalysis.issue_id == Issue.id)
        .where(Issue.repo_full_name == repo_full_name)
        .order_by(Issue.created_at, Issue.id)
    )

    return [
        IssueFacts(
            issue_id=row.id,
            created_at=row.created_at,
            state=row.state,
            first_response_checked=row.checked_issue_id is not None,
            responded_at=row.responded_at,
            category=row.category,
            sentiment=row.sentiment,
        )
        for row in session.execute(statement)
    ]


def load_sync_cursor(session: Session, repo_full_name: str, resource: str) -> SyncCursor | None:
    """저장된 증분 수집 커서를 읽는다.

    Args:
        session: 사용할 세션.
        repo_full_name: 대상 저장소.
        resource: 수집 대상 종류(`"issues"` 등).

    Returns:
        저장된 커서. 한 번도 수집한 적이 없으면 `None`.
    """
    state = session.get(SyncState, (repo_full_name, resource))
    if state is None:
        return None
    return SyncCursor(
        etag=state.etag,
        request_fingerprint=state.request_fingerprint,
        since_cursor=state.since_cursor,
        last_synced_at=state.last_synced_at,
    )


def save_sync_cursor(
    session: Session, repo_full_name: str, resource: str, cursor: SyncCursor
) -> UpsertOutcome:
    """증분 수집 커서를 저장한다. **커밋하지 않는다.**

    커밋을 호출자에게 맡기는 것이 여기서 특히 중요하다. 이슈 저장과 커서 전진이
    **같은 트랜잭션**이어야 "저장은 됐는데 커서는 안 갔다"거나 그 반대가 생기지
    않는다. 뒤쪽이 더 나쁘다 -- 커서만 앞서 나가면 빠진 구간을 다시는 받지 않는다.

    Args:
        session: 사용할 세션.
        repo_full_name: 대상 저장소.
        resource: 수집 대상 종류.
        cursor: 저장할 커서.

    Returns:
        실제로 삽입했는지 갱신했는지.
    """
    return _insert_or_update(
        session,
        SyncState,
        key={"repo_full_name": repo_full_name, "resource": resource},
        values={
            "repo_full_name": repo_full_name,
            "resource": resource,
            "etag": cursor.etag,
            "request_fingerprint": cursor.request_fingerprint,
            "since_cursor": cursor.since_cursor,
            "last_synced_at": cursor.last_synced_at,
        },
    )
