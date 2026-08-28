"""지표 엔드포인트 테스트.

이 파일이 고정하려는 것은 **세 상태가 뭉개지지 않는가**이다.

| 상태 | 응답 |
|---|---|
| 모르는 저장소 | 404 |
| 아는데 아직 수집 전 | 200 + `status="pending"` |
| 수집 끝남 | 200 + `status="ready"` |

뒤의 둘을 합치면 오타로 친 저장소가 "수집 중"으로 보이고, 앞의 둘을 합치면
수집을 기다리는 중인데 "없는 저장소"라고 답한다. 특히 **"수집은 끝났는데 기간 안
이슈가 0건"** 은 `pending`이 아니라 `ready`다 -- 기다려도 값이 생기지 않는다.

`TestClient`는 앱을 별도 스레드에서 돌린다. 인메모리 SQLite에 `StaticPool`이
없으면 그 스레드가 **다른 빈 데이터베이스**를 열어 `no such table`이 난다
(`CLAUDE.md` 10절). `create_db_engine()`이 그 설정을 붙인다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import create_app
from app.api.deps import get_now
from app.collectors.sync import ISSUES_RESOURCE
from app.config import Settings
from app.models import (
    IssueAnalysis,
    IssueCategory,
    IssueFirstResponse,
    IssueSentiment,
    IssueState,
    SyncCursor,
    save_sync_cursor,
)
from tests.conftest import TEST_REPO

# 고정된 기준 시각. 응답의 숫자가 실행 시각에 따라 달라지면 정확한 값을 확인할 수 없다.
FIXED_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

# WINDOW_DAYS=180 -> 2026-02-25 12:00 / STALE_DAYS=90 -> 2026-05-26 12:00
WINDOW_START = FIXED_NOW - timedelta(days=180)
STALE_CUTOFF = FIXED_NOW - timedelta(days=90)

OTHER_REPO = "someone/else"

METRICS_PATH = "/api/repos/{repo}/metrics"


def metrics_url(repo_full_name: str) -> str:
    """저장소의 지표 엔드포인트 경로."""
    return METRICS_PATH.format(repo=repo_full_name)


@pytest.fixture
def settings(required_settings: dict[str, str]) -> Settings:
    """대상 저장소가 `TEST_REPO`인 설정."""
    return Settings(**required_settings, target_repo=TEST_REPO)


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """인메모리 DB를 보는 앱의 테스트 클라이언트.

    세션 팩토리를 주입한다. 주입할 자리가 없으면 `DATABASE_URL`이 가리키는 실제
    파일 DB를 열게 되고, 테스트가 개발용 데이터를 읽거나 덮어쓴다.
    """
    app = create_app(settings=settings, session_factory=session_factory)
    # 시각을 고정한다. 라우터가 datetime.now()를 직접 부르면 6개월 경계에 걸린
    # 이슈가 실행 시점에 따라 들어왔다 나갔다 한다.
    app.dependency_overrides[get_now] = lambda: FIXED_NOW
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seed(session_factory: sessionmaker[Session], make_issue: Any):
    """이슈와 딸린 판정 결과를 넣고 **커밋하는** 헬퍼.

    커밋까지 하는 것이 중요하다. `StaticPool`이라 앱과 테스트가 같은 연결을
    공유하므로, 열어 둔 트랜잭션을 그대로 두면 요청 처리와 뒤엉킨다.
    """

    def _seed(
        *,
        issue_id: int,
        number: int,
        created_at: datetime,
        state: IssueState,
        repo_full_name: str = TEST_REPO,
        checked: bool = False,
        responded_after: timedelta | None = None,
        category: IssueCategory | None = None,
        sentiment: IssueSentiment | None = None,
    ) -> None:
        with session_factory() as session:
            session.add(
                make_issue(
                    id=issue_id,
                    number=number,
                    repo_full_name=repo_full_name,
                    created_at=created_at,
                    updated_at=created_at,
                    state=state,
                    closed_at=None if state is IssueState.OPEN else created_at,
                    state_reason=None if state is IssueState.OPEN else "completed",
                )
            )
            session.flush()

            if checked:
                responded_at = None if responded_after is None else created_at + responded_after
                session.add(
                    IssueFirstResponse(
                        issue_id=issue_id,
                        checked_at=FIXED_NOW,
                        responded_at=responded_at,
                        # 세 필드는 함께 채워지거나 함께 빈다(DB CHECK).
                        comment_id=None if responded_at is None else issue_id + 1,
                        responder_login=None if responded_at is None else "jlowin",
                    )
                )

            if category is not None and sentiment is not None:
                session.add(
                    IssueAnalysis(
                        issue_id=issue_id,
                        category=category,
                        sentiment=sentiment,
                        model="claude-opus-5",
                        prompt_version="1",
                        analyzed_at=FIXED_NOW,
                    )
                )

            session.commit()

    return _seed


@pytest.fixture
def mark_synced(session_factory: sessionmaker[Session]):
    """ "수집을 한 번 끝냈다"는 커서를 남긴다."""

    def _mark(
        repo_full_name: str = TEST_REPO, *, last_synced_at: datetime | None = FIXED_NOW
    ) -> None:
        with session_factory() as session:
            save_sync_cursor(
                session,
                repo_full_name,
                ISSUES_RESOURCE,
                SyncCursor(since_cursor=WINDOW_START, last_synced_at=last_synced_at),
            )
            session.commit()

    return _mark


# ---------------------------------------------------------------------------
# StaticPool — 없으면 아래 테스트가 전부 no such table로 죽는다
# ---------------------------------------------------------------------------


def test_in_memory_engine_uses_static_pool(engine: Engine):
    """`TestClient`의 스레드가 같은 데이터베이스를 보게 하는 설정.

    기본 풀은 연결마다 **서로 다른 빈 인메모리 DB**를 연다. 테이블을 만든 연결과
    조회하는 연결이 달라지는 순간 `no such table`이 난다.
    """
    assert isinstance(engine.pool, StaticPool)


# ---------------------------------------------------------------------------
# 404 — 이 대시보드가 다루지 않는 저장소
# ---------------------------------------------------------------------------


def test_unknown_repo_returns_404(client: TestClient):
    response = client.get(metrics_url("nobody/nothing"))

    assert response.status_code == 404
    # 무엇이 대상인지 알려준다. "없다"만 돌려주면 오타인지 미수집인지 알 수 없다.
    assert TEST_REPO in response.json()["detail"]


def test_unknown_repo_is_404_even_when_another_repo_has_data(
    client: TestClient, seed: Any, mark_synced: Any
):
    """다른 저장소에 데이터가 있어도 대상이 아니면 404다."""
    mark_synced()
    seed(
        issue_id=1,
        number=1,
        created_at=FIXED_NOW - timedelta(days=10),
        state=IssueState.OPEN,
    )

    assert client.get(metrics_url("nobody/nothing")).status_code == 404


def test_previously_synced_repo_is_not_404_even_if_it_is_not_the_target(
    client: TestClient, mark_synced: Any
):
    """`sync_state`에 커서가 있으면 "아는 저장소"다.

    `TARGET_REPO`를 바꿔 돌린 뒤에도 예전에 수집한 저장소의 지표를 계속 볼 수
    있어야 한다. 설정 한 줄을 바꿨다고 저장된 데이터가 404가 되면 안 된다.
    """
    mark_synced(OTHER_REPO)

    response = client.get(metrics_url(OTHER_REPO))

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# pending — 아는 저장소인데 아직 수집 전
# ---------------------------------------------------------------------------


def test_target_repo_without_any_sync_is_pending(client: TestClient):
    response = client.get(metrics_url(TEST_REPO))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["repo"] == TEST_REPO


def test_pending_response_leaves_the_metrics_null_instead_of_zero(client: TestClient):
    """0으로 채우지 않는다.

    채우면 프론트가 **수집이 끝난 빈 저장소와 구분할 수 없고**, 그래프는 정상적으로
    그려진다. 값이 없다는 사실이 화면 어디에도 남지 않는다.
    """
    body = client.get(metrics_url(TEST_REPO)).json()

    assert body["stale_issues"] is None
    assert body["categories"] is None
    assert body["sentiments"] is None
    assert body["response_time"] is None
    assert body["issue_count"] is None
    # 조건은 그대로 알려준다. 무엇을 기다리는 중인지는 알 수 있어야 한다.
    assert body["window_days"] == 180


def test_started_but_unfinished_sync_is_still_pending(client: TestClient, mark_synced: Any):
    """커서 행만 있고 `last_synced_at`이 비면 아직 한 번도 끝내지 못한 것이다."""
    mark_synced(last_synced_at=None)

    assert client.get(metrics_url(TEST_REPO)).json()["status"] == "pending"


def test_finished_sync_with_no_issues_is_ready_not_pending(client: TestClient, mark_synced: Any):
    """수집이 끝났으면 이슈가 0건이어도 `ready`다.

    이슈 건수로 `pending`을 판정하면 **기다려도 값이 생기지 않는 저장소가 영원히
    "수집 중"으로 보인다.** 낼 수 없는 비율은 `null`로 답하는 것이지 미완이 아니다.
    """
    mark_synced()

    body = client.get(metrics_url(TEST_REPO)).json()

    assert body["status"] == "ready"
    assert body["issue_count"] == 0
    assert body["stale_issues"]["ratio"] is None
    assert body["categories"]["ratios"] is None
    assert body["response_time"]["median_seconds"] is None


# ---------------------------------------------------------------------------
# ready — 지표 4개
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_repo(seed: Any, mark_synced: Any) -> None:
    """지표 4개를 한 번에 확인할 수 있는 표본.

    | # | 상태 | 생성 | 첫 응답 | 분석 |
    |---|---|---|---|---|
    | 1 | open | 방치 기준보다 오래됨 | 조사함 · 응답 없음 | bug / frustrated |
    | 2 | open | 오래됨 | 2시간 뒤 응답 | bug / neutral |
    | 3 | open | 최근 | 4시간 뒤 응답 | feature_request / positive |
    | 4 | closed | 오래됨 | 조사함 · 응답 없음 | 미분석 |
    | 5 | open | 오래됨 | **미조사** | 미분석 |
    | 6 | open | **기간 밖** | 미조사 | 미분석 |
    """
    mark_synced()
    seed(
        issue_id=1,
        number=1,
        created_at=STALE_CUTOFF - timedelta(days=1),
        state=IssueState.OPEN,
        checked=True,
        category=IssueCategory.BUG,
        sentiment=IssueSentiment.FRUSTRATED,
    )
    seed(
        issue_id=2,
        number=2,
        created_at=STALE_CUTOFF - timedelta(days=2),
        state=IssueState.OPEN,
        checked=True,
        responded_after=timedelta(hours=2),
        category=IssueCategory.BUG,
        sentiment=IssueSentiment.NEUTRAL,
    )
    seed(
        issue_id=3,
        number=3,
        created_at=FIXED_NOW - timedelta(days=4),
        state=IssueState.OPEN,
        checked=True,
        responded_after=timedelta(hours=4),
        category=IssueCategory.FEATURE_REQUEST,
        sentiment=IssueSentiment.POSITIVE,
    )
    seed(
        issue_id=4,
        number=4,
        created_at=STALE_CUTOFF - timedelta(days=3),
        state=IssueState.CLOSED,
        checked=True,
    )
    seed(
        issue_id=5,
        number=5,
        created_at=STALE_CUTOFF - timedelta(days=4),
        state=IssueState.OPEN,
    )
    seed(
        issue_id=6,
        number=6,
        created_at=WINDOW_START - timedelta(days=1),
        state=IssueState.OPEN,
    )


def test_ready_response_counts_only_issues_created_inside_the_window(
    client: TestClient, seeded_repo: None
):
    body = client.get(metrics_url(TEST_REPO)).json()

    assert body["status"] == "ready"
    # 6번은 기간 밖이라 어느 지표에도 들어가지 않는다.
    assert body["issue_count"] == 5


def test_ready_response_reports_stale_ratio_with_the_undetermined_count(
    client: TestClient, seeded_repo: None
):
    stale = client.get(metrics_url(TEST_REPO)).json()["stale_issues"]

    # 분모는 기간 안 open 이슈(1·2·3·5). closed인 4번은 들어가지 않는다.
    assert stale["open_total"] == 4
    assert stale["stale_count"] == 1
    assert stale["ratio"] == pytest.approx(0.25)
    # 5번은 미조사라 방치인지 아닌지 아직 모른다. 분자에 넣지 않고 드러낸다.
    assert stale["unchecked_count"] == 1
    assert stale["stale_days"] == 90


def test_ready_response_exposes_the_unanalyzed_count(client: TestClient, seeded_repo: None):
    """미분석을 분모에서 조용히 빼지 않는다.

    분모는 분석 완료 3건이고, 대상 5건 중 2건이 미분석이라는 사실이 응답에 남는다.
    """
    categories = client.get(metrics_url(TEST_REPO)).json()["categories"]

    assert categories["analyzed_count"] == 3
    assert categories["unanalyzed_count"] == 2
    assert categories["total"] == 5
    # 0건인 값도 키로 남는다.
    assert categories["counts"] == {"bug": 2, "feature_request": 1, "question": 0, "other": 0}
    assert categories["ratios"]["bug"] == pytest.approx(2 / 3)


def test_ready_response_reports_the_sentiment_distribution(client: TestClient, seeded_repo: None):
    sentiments = client.get(metrics_url(TEST_REPO)).json()["sentiments"]

    assert sentiments["counts"] == {"positive": 1, "neutral": 1, "frustrated": 1}
    assert sentiments["unanalyzed_count"] == 2


def test_ready_response_excludes_no_response_from_the_median_and_counts_it(
    client: TestClient, seeded_repo: None
):
    """응답 없음을 0으로 채우지 않는다.

    채웠다면 중앙값이 3시간이 아니라 0에 가까워진다. 응답이 느린 저장소일수록
    지표가 좋아지는 셈이다.
    """
    response_time = client.get(metrics_url(TEST_REPO)).json()["response_time"]

    # 2시간(2번)과 4시간(3번)의 중앙값.
    assert response_time["median_seconds"] == pytest.approx(3 * 3600)
    assert response_time["responded_count"] == 2
    # 1번과 4번은 조사했지만 응답이 없었다.
    assert response_time["no_response_count"] == 2
    # 5번은 아직 조사하지 않았다. 위와 다른 값이다.
    assert response_time["unchecked_count"] == 1


def test_window_boundary_applies_through_the_api(client: TestClient, seed: Any, mark_synced: Any):
    """경계에 정확히 걸린 이슈는 포함, 1마이크로초 앞은 제외."""
    mark_synced()
    seed(issue_id=10, number=10, created_at=WINDOW_START, state=IssueState.OPEN)
    seed(
        issue_id=11,
        number=11,
        created_at=WINDOW_START - timedelta(microseconds=1),
        state=IssueState.OPEN,
    )

    assert client.get(metrics_url(TEST_REPO)).json()["issue_count"] == 1


def test_metrics_of_one_repo_do_not_leak_into_another(
    client: TestClient, seed: Any, mark_synced: Any
):
    """`repo_full_name`으로 범위를 자른다.

    이슈 `number`는 저장소 안에서만 유일하므로, 저장소를 바꿔 돌리면 다른
    저장소의 이슈가 섞일 수 있다(`app/models/tables.py`).
    """
    mark_synced()
    mark_synced(OTHER_REPO)
    seed(
        issue_id=20,
        number=1,
        created_at=FIXED_NOW - timedelta(days=5),
        state=IssueState.OPEN,
    )
    seed(
        issue_id=21,
        number=1,
        repo_full_name=OTHER_REPO,
        created_at=FIXED_NOW - timedelta(days=5),
        state=IssueState.OPEN,
    )

    assert client.get(metrics_url(TEST_REPO)).json()["issue_count"] == 1
    assert client.get(metrics_url(OTHER_REPO)).json()["issue_count"] == 1


def test_generated_at_is_the_time_the_metrics_were_computed(client: TestClient, mark_synced: Any):
    mark_synced()

    body = client.get(metrics_url(TEST_REPO)).json()

    assert datetime.fromisoformat(body["generated_at"]) == FIXED_NOW
