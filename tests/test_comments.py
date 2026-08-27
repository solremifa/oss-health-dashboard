"""`app.collectors.comments` 테스트 — 메인테이너 첫 응답 판별.

여기서 검증하는 것은 전부 **틀려도 에러가 나지 않는** 종류다. 봇을 못 거르면 응답
속도가 실제보다 빨라지고, 제3자를 메인테이너로 세면 방치율이 낮아지는데, 둘 다
그럴듯한 숫자로 나와서 틀렸다는 사실조차 드러나지 않는다.

그래서 판별 조건 하나씩을 따로 무너뜨려 본다. 등장인물은 `docs/findings.md` 함정 3의
실측 표본에서 그대로 가져왔다 -- 특히 **봇들의 `author_association`이 전부
`CONTRIBUTOR`**라는 점이 중요하다. association만으로 거르는 흔한 구현이 왜 통하지
않는지가 그 값에 들어 있다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors.client import GitHubClient
from app.collectors.comments import (
    MAINTAINER_ASSOCIATIONS,
    FirstResponseFetch,
    comment_list_params,
    comments_path,
    fetch_first_response,
    is_maintainer_response,
    select_first_response,
)
from app.collectors.schemas import CommentSchema
from app.models import (
    IssueComment,
    IssueFirstResponse,
    IssueRecord,
    IssueState,
    load_first_response,
    upsert_comment,
    upsert_first_response,
    upsert_issue,
)

BASE = "https://api.github.com"
REPO = "PrefectHQ/fastmcp"

ISSUE_ID = 3_288_000_001
ISSUE_NUMBER = 3288
ISSUE_AUTHOR = "sharabash"

OPENED_AT = datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
CHECKED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _Recorder:
    """요청을 기록하면서 미리 짜 넣은 응답을 순서대로 돌려주는 핸들러."""

    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def _client(handler: _Recorder) -> GitHubClient:
    return GitHubClient(
        "ghp_test_token",
        base_url=BASE,
        transport=httpx2.MockTransport(handler),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
    )


def _comment_payload(
    comment_id: int,
    *,
    hours: int,
    login: str,
    account_type: str = "User",
    association: str = "MEMBER",
) -> dict[str, Any]:
    """실제 응답 모양을 흉내 낸 코멘트 아이템."""
    return {
        "id": comment_id,
        "body": "on it",
        "created_at": (OPENED_AT + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": {"login": login, "id": comment_id % 10_000, "type": account_type},
        "author_association": association,
    }


def _comment(**kwargs: Any) -> CommentSchema:
    return CommentSchema.model_validate(_comment_payload(**kwargs))


def _page(items: list[dict[str, Any]], **headers: str) -> httpx2.Response:
    return httpx2.Response(200, json=items, headers=headers)


def _next_link(page: int) -> str:
    """rel="next" Link 헤더 값. URL은 서버가 준 것을 그대로 쓰는 것이 규칙이다."""
    url = f"{BASE}/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100&page={page}"
    return f'<{url}>; rel="next"'


def _fetch(handler: _Recorder, *, comments_count: int | None = None) -> FirstResponseFetch:
    with _client(handler) as client:
        return fetch_first_response(
            client,
            REPO,
            issue_id=ISSUE_ID,
            issue_number=ISSUE_NUMBER,
            issue_author_login=ISSUE_AUTHOR,
            comments_count=comments_count,
            now=CHECKED_AT,
        )


# 실측 표본에 나온 실제 등장인물들(`docs/findings.md` 함정 3).
MAINTAINER = {"login": "jlowin", "association": "MEMBER"}
# 봇의 association은 CONTRIBUTOR다. association만 보면 봇이 걸러지지 않는다.
REVIEW_BOT = {
    "login": "coderabbitai[bot]",
    "account_type": "Bot",
    "association": "CONTRIBUTOR",
}
# user.type은 User지만 저장소와 아무 관계가 없는 제3자.
BYSTANDER = {"login": "passerby", "association": "NONE"}


# ---------------------------------------------------------------------------
# 요청 — 경로와 파라미터
# ---------------------------------------------------------------------------


def test_path_uses_the_issue_number_not_the_global_id():
    """전역 ID를 경로에 쓰면 404가 나거나 남의 이슈를 읽는다."""
    assert comments_path(REPO, ISSUE_NUMBER) == f"/repos/{REPO}/issues/3288/comments"


def test_params_ask_for_a_full_page_in_creation_order():
    params = comment_list_params()

    assert params["per_page"] == 100
    assert params["sort"] == "created"
    assert params["direction"] == "asc"


# ---------------------------------------------------------------------------
# 판별 조건 — 셋 중 하나만 빠져도 틀린다
# ---------------------------------------------------------------------------


def test_maintainer_comment_is_a_response():
    comment = _comment(comment_id=1, hours=2, **MAINTAINER)

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is True


def test_bot_is_not_a_response_even_though_it_is_a_contributor():
    """실측에서 첫 코멘트의 절반이 봇이었고, 그 봇들의 association은 전부 CONTRIBUTOR였다."""
    comment = _comment(comment_id=2, hours=1, **REVIEW_BOT)

    assert comment.author_association not in MAINTAINER_ASSOCIATIONS
    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is False


def test_bot_with_a_maintainer_association_is_still_not_a_response():
    """association이 MEMBER인 봇이 와도 user.type이 막는다. 두 조건은 서로를 대신하지 못한다."""
    comment = _comment(
        comment_id=3, hours=1, login="marvin[bot]", account_type="Bot", association="MEMBER"
    )

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is False


def test_bystander_is_not_a_response():
    """사람이지만 저장소와 관계가 없다. 지나가던 제3자의 답은 메인테이너 응답이 아니다."""
    comment = _comment(comment_id=4, hours=1, **BYSTANDER)

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is False


def test_contributor_is_not_a_maintainer():
    """CONTRIBUTOR는 PR을 한 번 보낸 외부인도 받는 값이다."""
    comment = _comment(comment_id=5, hours=1, login="helper", association="CONTRIBUTOR")

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is False


def test_self_reply_is_not_a_response():
    """작성자가 자기 이슈에 덧붙인 코멘트는 응답이 아니다."""
    comment = _comment(comment_id=6, hours=1, login=ISSUE_AUTHOR, association="MEMBER")

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is False


def test_maintainer_replying_to_someone_elses_issue_is_a_response():
    """self-reply 조건이 메인테이너의 정상 응답까지 막으면 안 된다."""
    comment = _comment(comment_id=7, hours=1, login="jlowin", association="OWNER")

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is True


@pytest.mark.parametrize("association", sorted(MAINTAINER_ASSOCIATIONS))
def test_all_three_maintainer_associations_count(association: str):
    comment = _comment(comment_id=8, hours=1, login="maint", association=association)

    assert is_maintainer_response(comment, issue_author_login=ISSUE_AUTHOR) is True


# ---------------------------------------------------------------------------
# 첫 응답 고르기 — 받은 순서를 믿지 않는다
# ---------------------------------------------------------------------------


def test_the_earliest_maintainer_response_wins_even_if_a_bot_answered_first():
    """봇이 먼저 답해도 응답 시각은 메인테이너가 답한 시각이다."""
    comments = [
        _comment(comment_id=10, hours=1, **REVIEW_BOT),
        _comment(comment_id=11, hours=6, **MAINTAINER),
    ]

    first = select_first_response(comments, issue_author_login=ISSUE_AUTHOR)

    assert first is not None
    assert first.id == 11
    assert first.created_at == OPENED_AT + timedelta(hours=6)


def test_order_received_does_not_decide_the_first_response():
    """서버가 순서를 뒤집어 줘도 created_at이 가장 이른 응답을 고른다."""
    comments = [
        _comment(comment_id=20, hours=30, **MAINTAINER),
        _comment(comment_id=21, hours=3, login="jlowin", association="OWNER"),
    ]

    first = select_first_response(comments, issue_author_login=ISSUE_AUTHOR)

    assert first is not None
    assert first.id == 21


def test_ties_are_broken_by_comment_id_so_reruns_agree():
    comments = [
        _comment(comment_id=31, hours=4, login="b", association="MEMBER"),
        _comment(comment_id=30, hours=4, login="a", association="MEMBER"),
    ]

    first = select_first_response(comments, issue_author_login=ISSUE_AUTHOR)

    assert first is not None
    assert first.id == 30


def test_no_maintainer_among_the_comments_is_none():
    comments = [
        _comment(comment_id=40, hours=1, **REVIEW_BOT),
        _comment(comment_id=41, hours=2, **BYSTANDER),
    ]

    assert select_first_response(comments, issue_author_login=ISSUE_AUTHOR) is None


# ---------------------------------------------------------------------------
# 수집 — 응답이 없어도 판정 결과는 항상 나온다
# ---------------------------------------------------------------------------


def test_maintainer_response_is_recorded_with_its_evidence():
    handler = _Recorder(
        [
            _page(
                [
                    _comment_payload(50, hours=1, **REVIEW_BOT),
                    _comment_payload(51, hours=5, **MAINTAINER),
                ]
            )
        ]
    )

    fetch = _fetch(handler, comments_count=2)

    assert fetch.record.responded is True
    assert fetch.record.responded_at == OPENED_AT + timedelta(hours=5)
    assert fetch.record.comment_id == 51
    assert fetch.record.responder_login == "jlowin"
    assert fetch.record.checked_at == CHECKED_AT
    # 판정 근거는 함께 저장한다. 규칙이 바뀌면 이것만으로 다시 계산할 수 있다.
    assert [comment.id for comment in fetch.comments] == [50, 51]
    assert all(comment.issue_id == ISSUE_ID for comment in fetch.comments)
    assert fetch.requests_made == 1


def test_only_a_bot_answered():
    handler = _Recorder([_page([_comment_payload(60, hours=1, **REVIEW_BOT)])])

    fetch = _fetch(handler, comments_count=1)

    assert fetch.record.responded is False
    assert fetch.record.responded_at is None
    assert fetch.record.comment_id is None
    assert fetch.record.responder_login is None
    # 조사는 했다. 그 사실이 checked_at으로 남는다.
    assert fetch.record.checked_at == CHECKED_AT
    assert len(fetch.comments) == 1


def test_only_a_bystander_answered():
    handler = _Recorder([_page([_comment_payload(61, hours=1, **BYSTANDER)])])

    fetch = _fetch(handler, comments_count=1)

    assert fetch.record.responded is False
    assert fetch.record.checked_at == CHECKED_AT


def test_only_the_author_replied_to_themselves():
    handler = _Recorder(
        [
            _page(
                [
                    _comment_payload(62, hours=1, login=ISSUE_AUTHOR, association="MEMBER"),
                    _comment_payload(63, hours=2, login=ISSUE_AUTHOR, association="MEMBER"),
                ]
            )
        ]
    )

    fetch = _fetch(handler, comments_count=2)

    assert fetch.record.responded is False
    assert fetch.record.checked_at == CHECKED_AT


def test_no_comments_at_all_skips_the_request():
    """실측 45건 중 8건이 코멘트 0건이었다. 그만큼 요청을 아낀다."""
    handler = _Recorder([])

    fetch = _fetch(handler, comments_count=0)

    assert handler.requests == []
    assert fetch.requests_made == 0
    assert fetch.record.responded is False
    assert fetch.record.checked_at == CHECKED_AT


def test_unknown_comment_count_still_asks():
    """개수를 모르면 생략하지 않는다. 조사를 건너뛰면 '조사 안 함'과 구분되지 않는다."""
    handler = _Recorder([_page([])])

    fetch = _fetch(handler)

    assert len(handler.requests) == 1
    assert fetch.requests_made == 1
    assert fetch.record.responded is False


# ---------------------------------------------------------------------------
# 페이지네이션 — 조기 종료하지 않는다
# ---------------------------------------------------------------------------


def test_pages_are_followed_even_after_a_maintainer_is_found():
    """1페이지에서 찾았다고 멈추면, 서버 정렬이 어긋났을 때 늦은 응답이 첫 응답이 된다."""
    handler = _Recorder(
        [
            _page([_comment_payload(70, hours=30, **MAINTAINER)], Link=_next_link(2)),
            _page([_comment_payload(71, hours=2, login="jlowin", association="OWNER")]),
        ]
    )

    fetch = _fetch(handler, comments_count=2)

    assert fetch.requests_made == 2
    assert fetch.record.comment_id == 71
    assert fetch.record.responded_at == OPENED_AT + timedelta(hours=2)
    assert len(fetch.comments) == 2


def test_the_next_page_url_is_followed_as_is():
    """다음 페이지 URL을 재조립하면 구간이 어긋난다. 통째로 따라간다."""
    handler = _Recorder(
        [
            _page([_comment_payload(80, hours=1, **REVIEW_BOT)], Link=_next_link(2)),
            _page([]),
        ]
    )

    _fetch(handler, comments_count=101)

    expected = f"{BASE}/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100&page=2"
    assert str(handler.requests[1].url) == expected


# ---------------------------------------------------------------------------
# 부분 실패 — 조용히 빼지 않는다
# ---------------------------------------------------------------------------


def test_a_broken_comment_is_reported_and_the_rest_still_decide():
    handler = _Recorder(
        [
            _page(
                [
                    {"id": 90, "body": "no user field"},
                    _comment_payload(91, hours=3, **MAINTAINER),
                ]
            )
        ]
    )

    fetch = _fetch(handler, comments_count=2)

    assert [item.identifier for item in fetch.invalid] == ["id=90"]
    assert fetch.record.comment_id == 91
    assert [comment.id for comment in fetch.comments] == [91]


def test_a_null_body_is_normalized_to_an_empty_string():
    """DB의 body는 NOT NULL이다. 여기서 정규화하지 않으면 저장에서 터진다."""
    payload = _comment_payload(92, hours=3, **MAINTAINER)
    payload["body"] = None
    handler = _Recorder([_page([payload])])

    fetch = _fetch(handler, comments_count=1)

    assert fetch.comments[0].body == ""


def test_emoji_in_a_comment_body_survives():
    """이슈 본문 45건 중 10건에 non-ASCII가 있었다(함정 6)."""
    payload = _comment_payload(93, hours=3, **MAINTAINER)
    payload["body"] = "\U0001f680 고쳤습니다"
    handler = _Recorder([_page([payload])])

    fetch = _fetch(handler, comments_count=1)

    assert fetch.comments[0].body == "\U0001f680 고쳤습니다"


# ---------------------------------------------------------------------------
# 저장까지 — 행의 존재가 "조사 완료"다
# ---------------------------------------------------------------------------


def _issue_record() -> IssueRecord:
    return IssueRecord(
        id=ISSUE_ID,
        repo_full_name=REPO,
        number=ISSUE_NUMBER,
        title="Client hangs when server closes the stream",
        body="steps to reproduce",
        state=IssueState.OPEN,
        state_reason=None,
        created_at=OPENED_AT,
        updated_at=OPENED_AT + timedelta(days=1),
        closed_at=None,
        comments_count=1,
        author_login=ISSUE_AUTHOR,
        author_id=4242,
        author_type="User",
        author_association="NONE",
    )


def test_a_fetch_with_no_response_still_leaves_a_row(session: Session):
    """수집 결과를 그대로 저장했을 때 "조사했고 응답이 없었다"가 DB에 남는지 본다."""
    handler = _Recorder([_page([_comment_payload(100, hours=1, **REVIEW_BOT)])])
    fetch = _fetch(handler, comments_count=1)

    upsert_issue(session, _issue_record())
    for comment in fetch.comments:
        upsert_comment(session, comment)
    upsert_first_response(session, fetch.record)
    session.flush()

    stored = load_first_response(session, ISSUE_ID)
    assert stored is not None
    assert stored.responded_at is None
    assert session.scalar(select(func.count()).select_from(IssueFirstResponse)) == 1
    assert session.scalar(select(func.count()).select_from(IssueComment)) == 1


def test_a_fetch_with_a_response_stores_the_verdict(session: Session):
    handler = _Recorder(
        [
            _page(
                [
                    _comment_payload(110, hours=1, **REVIEW_BOT),
                    _comment_payload(111, hours=4, **MAINTAINER),
                ]
            )
        ]
    )
    fetch = _fetch(handler, comments_count=2)

    upsert_issue(session, _issue_record())
    for comment in fetch.comments:
        upsert_comment(session, comment)
    upsert_first_response(session, fetch.record)
    session.flush()

    stored = load_first_response(session, ISSUE_ID)
    assert stored is not None
    assert stored.responded_at == OPENED_AT + timedelta(hours=4)
    assert stored.responder_login == "jlowin"
    assert stored.comment_id == 111
