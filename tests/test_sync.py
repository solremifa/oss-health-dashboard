"""`app.collectors.sync` 테스트 — 증분 수집의 커서와 조건부 요청.

여기서 검증하는 것들은 전부 **틀려도 에러가 나지 않는** 종류다. ETag가 무시되면
전체 재수집이 되고, 커서가 잘못 전진하면 구간이 통째로 비는데, 둘 다 로그에는
"수집 성공"만 남는다. 그래서 요청에 실제로 어떤 헤더와 파라미터가 실려 나갔는지를
직접 본다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select

from app.collectors import sync
from app.collectors.client import GitHubClient
from app.collectors.sync import (
    ISSUES_RESOURCE,
    build_request_fingerprint,
    conditional_headers,
    fetch_issues,
    issue_list_params,
    issue_list_path,
)
from app.models import Issue, SyncCursor, load_sync_cursor, save_sync_cursor, upsert_issue

BASE = "https://api.github.com"
REPO = "PrefectHQ/fastmcp"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))


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


def _issue(number: int, updated_at: str, **overrides: Any) -> dict[str, Any]:
    """실제 응답 모양을 흉내 낸 이슈 아이템."""
    item: dict[str, Any] = {
        "id": 3_288_000_000 + number,
        "number": number,
        "title": f"issue {number}",
        "body": "body",
        "state": "closed",
        "state_reason": "completed",
        "created_at": "2026-03-01T09:15:00Z",
        "updated_at": updated_at,
        "closed_at": None,
        "comments": 1,
        "user": {"login": "someone", "id": 4242, "type": "User"},
        "author_association": "NONE",
        "labels": [{"id": 1, "name": "bug"}],
    }
    item.update(overrides)
    return item


def _page(items: list[dict[str, Any]], **headers: str) -> httpx2.Response:
    return httpx2.Response(200, json=items, headers=headers)


# ---------------------------------------------------------------------------
# 요청 파라미터 — 수집 축은 updated_at이다
# ---------------------------------------------------------------------------


def test_params_ask_for_all_states_sorted_by_updated_ascending():
    """오름차순이어야 최대 `updated_at`을 다음 커서로 쓸 수 있다."""
    params = issue_list_params(None)

    assert params["state"] == "all"
    assert params["sort"] == "updated"
    assert params["direction"] == "asc"
    assert "since" not in params


def test_since_is_formatted_as_utc_iso8601():
    params = issue_list_params(datetime(2026, 2, 24, 0, 0, tzinfo=UTC))

    assert params["since"] == "2026-02-24T00:00:00Z"


def test_since_is_converted_to_utc():
    """다른 시간대로 들어와도 UTC로 보낸다. 로컬 시각을 그대로 보내면 구간이 밀린다."""
    params = issue_list_params(datetime(2026, 2, 24, 18, 0, tzinfo=KST))

    assert params["since"] == "2026-02-24T09:00:00Z"


# ---------------------------------------------------------------------------
# 요청 지문 — 함정 5
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_request():
    path = issue_list_path(REPO)
    params = issue_list_params(None)

    assert build_request_fingerprint(path, params) == build_request_fingerprint(path, params)


def test_fingerprint_changes_when_since_changes():
    path = issue_list_path(REPO)

    first = build_request_fingerprint(path, issue_list_params(None))
    second = build_request_fingerprint(path, issue_list_params(NOW))

    assert first != second


def test_fingerprint_changes_when_the_repo_changes():
    params = issue_list_params(None)

    assert build_request_fingerprint(issue_list_path("a/one"), params) != build_request_fingerprint(
        issue_list_path("b/two"), params
    )


def test_fingerprint_covers_the_accept_header(monkeypatch):
    """ETag가 갈린 실제 원인이 `Accept` 헤더였다. 지문이 그 변화를 잡아야 한다."""
    path = issue_list_path(REPO)
    params = issue_list_params(None)
    original = build_request_fingerprint(path, params)

    monkeypatch.setattr(sync, "ACCEPT_HEADER", "application/json")

    assert build_request_fingerprint(path, params) != original


# ---------------------------------------------------------------------------
# 조건부 요청 헤더 — 지문이 다르면 ETag를 버린다
# ---------------------------------------------------------------------------


def test_no_cursor_means_no_conditional_header():
    assert conditional_headers(None, "abc") == {}


def test_cursor_without_etag_means_no_conditional_header():
    cursor = SyncCursor(since_cursor=NOW)

    assert conditional_headers(cursor, "abc") == {}


def test_matching_fingerprint_sends_the_etag():
    cursor = SyncCursor(etag='W/"abc"', request_fingerprint="fp")

    assert conditional_headers(cursor, "fp") == {"If-None-Match": 'W/"abc"'}


def test_mismatched_fingerprint_discards_the_etag(caplog):
    """지문이 다르면 ETag를 버린다.

    그대로 보내면 304가 아니라 200이 오고 **에러 없이 전체를 다시 받는다.**
    증분 수집이 도는 것처럼 보이면서 매번 전체 재수집이 된다.
    """
    cursor = SyncCursor(etag='W/"abc"', request_fingerprint="old-fingerprint")

    with caplog.at_level("INFO"):
        headers = conditional_headers(cursor, "new-fingerprint")

    assert headers == {}
    assert any("ETag" in record.getMessage() for record in caplog.records)


def test_etag_and_fingerprint_must_come_as_a_pair():
    """한쪽만 든 커서는 만들어질 수 없다. DB CHECK 전에 여기서 막는다."""
    with pytest.raises(ValueError, match="함께"):
        SyncCursor(etag='W/"abc"')

    with pytest.raises(ValueError, match="함께"):
        SyncCursor(request_fingerprint="fp")


# ---------------------------------------------------------------------------
# 첫 수집
# ---------------------------------------------------------------------------


def test_first_fetch_sends_no_since_and_no_conditional_header():
    handler = _Recorder([_page([_issue(1, "2026-03-01T10:00:00Z")], ETag='W/"one"')])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    request = handler.requests[0]
    assert "since" not in str(request.url)
    assert "If-None-Match" not in request.headers
    assert len(fetch.batch.issues) == 1
    assert fetch.requests_made == 1


def test_cursor_advances_to_the_latest_updated_at():
    """`since`는 **본 것 중 가장 늦은 `updated_at`**으로만 전진한다."""
    handler = _Recorder(
        [
            _page(
                [
                    _issue(1, "2026-03-01T10:00:00Z"),
                    _issue(2, "2026-03-05T11:30:00Z"),
                    _issue(3, "2026-03-03T09:00:00Z"),
                ],
                ETag='W/"one"',
            )
        ]
    )

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert fetch.cursor.since_cursor == datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    assert fetch.cursor.last_synced_at == NOW


def test_cursor_does_not_advance_to_now():
    """현재 시각으로 당기면 수집이 도는 동안 갱신된 이슈를 영원히 놓친다."""
    handler = _Recorder([_page([_issue(1, "2026-03-01T10:00:00Z")], ETag='W/"one"')])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert fetch.cursor.since_cursor != NOW
    assert fetch.cursor.since_cursor == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def test_etag_is_stored_with_the_fingerprint_of_the_request_that_produced_it():
    handler = _Recorder([_page([_issue(1, "2026-03-01T10:00:00Z")], ETag='W/"one"')])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    expected = build_request_fingerprint(issue_list_path(REPO), issue_list_params(None))
    assert fetch.cursor.etag == 'W/"one"'
    assert fetch.cursor.request_fingerprint == expected


def test_pull_requests_are_dropped_before_the_cursor_is_computed():
    """PR의 `updated_at`으로 커서를 옮기면 안 된다. 저장하지 않는 데이터다."""
    handler = _Recorder(
        [
            _page(
                [
                    _issue(1, "2026-03-01T10:00:00Z"),
                    _issue(2, "2026-09-01T10:00:00Z", pull_request={"url": "..."}),
                ],
                ETag='W/"one"',
            )
        ]
    )

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert fetch.batch.pull_requests_skipped == 1
    assert fetch.cursor.since_cursor == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 두 번째 수집 — since와 조건부 요청
# ---------------------------------------------------------------------------


def test_second_fetch_sends_since_and_the_etag():
    since = datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    params = issue_list_params(since)
    fingerprint = build_request_fingerprint(issue_list_path(REPO), params)
    cursor = SyncCursor(etag='W/"one"', request_fingerprint=fingerprint, since_cursor=since)
    handler = _Recorder([_page([], ETag='W/"two"')])

    with _client(handler) as client:
        fetch_issues(client, REPO, cursor=cursor, now=NOW)

    request = handler.requests[0]
    assert "since=2026-03-05T11%3A30%3A00Z" in str(request.url)
    assert request.headers["If-None-Match"] == 'W/"one"'


def test_stale_fingerprint_means_the_etag_is_not_sent():
    """지난 회차와 요청이 달라졌으면 ETag를 붙이지 않는다."""
    cursor = SyncCursor(
        etag='W/"one"',
        request_fingerprint="지난-회차의-다른-지문",
        since_cursor=datetime(2026, 3, 5, 11, 30, tzinfo=UTC),
    )
    handler = _Recorder([_page([], ETag='W/"two"')])

    with _client(handler) as client:
        fetch_issues(client, REPO, cursor=cursor, now=NOW)

    assert "If-None-Match" not in handler.requests[0].headers


def test_not_modified_keeps_the_cursor_and_still_costs_a_request():
    """304여도 요청 1회로 센다. 문서와 달리 실측에서 쿼터를 소모했다(함정 5)."""
    since = datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    fingerprint = build_request_fingerprint(issue_list_path(REPO), issue_list_params(since))
    cursor = SyncCursor(etag='W/"one"', request_fingerprint=fingerprint, since_cursor=since)
    handler = _Recorder([httpx2.Response(304, headers={"ETag": 'W/"one"'})])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=cursor, now=NOW)

    assert fetch.not_modified is True
    assert fetch.batch.issues == []
    assert fetch.requests_made == 1
    # 커서는 그대로, 수집 시각만 갱신된다.
    assert fetch.cursor.etag == 'W/"one"'
    assert fetch.cursor.request_fingerprint == fingerprint
    assert fetch.cursor.since_cursor == since
    assert fetch.cursor.last_synced_at == NOW


def test_empty_page_does_not_move_the_cursor_backwards():
    """받은 이슈가 없으면 커서를 그대로 둔다. `None`으로 되돌리면 전량 재수집이 된다."""
    since = datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    fingerprint = build_request_fingerprint(issue_list_path(REPO), issue_list_params(since))
    cursor = SyncCursor(etag='W/"one"', request_fingerprint=fingerprint, since_cursor=since)
    handler = _Recorder([_page([], ETag='W/"two"')])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=cursor, now=NOW)

    assert fetch.cursor.since_cursor == since


def test_missing_etag_header_keeps_the_previous_pair_intact():
    """ETag가 안 오면 지문만 남기지 않는다. 한쪽만 있는 커서는 DB가 거부한다."""
    since = datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    fingerprint = build_request_fingerprint(issue_list_path(REPO), issue_list_params(since))
    cursor = SyncCursor(etag='W/"one"', request_fingerprint=fingerprint, since_cursor=since)
    handler = _Recorder([_page([_issue(1, "2026-03-06T10:00:00Z")])])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=cursor, now=NOW)

    assert (fetch.cursor.etag is None) == (fetch.cursor.request_fingerprint is None)
    assert fetch.cursor.since_cursor == datetime(2026, 3, 6, 10, 0, tzinfo=UTC)


def test_first_fetch_without_etag_header_leaves_the_pair_empty():
    handler = _Recorder([_page([_issue(1, "2026-03-01T10:00:00Z")])])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert fetch.cursor.etag is None
    assert fetch.cursor.request_fingerprint is None


# ---------------------------------------------------------------------------
# 여러 페이지
# ---------------------------------------------------------------------------


def test_etag_comes_from_the_first_page_only():
    """2페이지의 ETag를 저장하면 다음 회차의 첫 요청에 붙어 아무 의미가 없다."""
    first = _page(
        [_issue(1, "2026-03-01T10:00:00Z")],
        ETag='W/"page-one"',
        Link=f'<{BASE}/repositories/1/issues?page=2&after=cursor>; rel="next"',
    )
    second = _page([_issue(2, "2026-03-02T10:00:00Z")], ETag='W/"page-two"')
    handler = _Recorder([first, second])

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert len(handler.requests) == 2
    assert fetch.requests_made == 2
    assert fetch.cursor.etag == 'W/"page-one"'
    assert len(fetch.batch.issues) == 2
    assert fetch.cursor.since_cursor == datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def test_conditional_header_is_not_repeated_on_the_second_page():
    """`If-None-Match`는 첫 페이지 URL에 대한 것이라 2페이지에 붙이면 의미가 없다."""
    since = datetime(2026, 3, 5, 11, 30, tzinfo=UTC)
    fingerprint = build_request_fingerprint(issue_list_path(REPO), issue_list_params(since))
    cursor = SyncCursor(etag='W/"one"', request_fingerprint=fingerprint, since_cursor=since)
    first = _page(
        [_issue(1, "2026-03-06T10:00:00Z")],
        ETag='W/"page-one"',
        Link=f'<{BASE}/repositories/1/issues?page=2&after=cursor>; rel="next"',
    )
    handler = _Recorder([first, _page([_issue(2, "2026-03-07T10:00:00Z")])])

    with _client(handler) as client:
        fetch_issues(client, REPO, cursor=cursor, now=NOW)

    assert handler.requests[0].headers["If-None-Match"] == 'W/"one"'
    assert "If-None-Match" not in handler.requests[1].headers


def test_invalid_items_are_reported_not_dropped_silently():
    """검증에 실패한 건은 결과 객체에 드러난다. 커서는 성공한 것 기준으로 전진한다."""
    handler = _Recorder(
        [
            _page(
                [
                    _issue(1, "2026-03-01T10:00:00Z"),
                    _issue(2, "2026-03-09T10:00:00Z", state="reopened"),
                ],
                ETag='W/"one"',
            )
        ]
    )

    with _client(handler) as client:
        fetch = fetch_issues(client, REPO, cursor=None, now=NOW)

    assert len(fetch.batch.issues) == 1
    assert len(fetch.batch.invalid) == 1
    assert fetch.cursor.since_cursor == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 수집 → 저장 — 두 회차를 이어 돌린다
# ---------------------------------------------------------------------------


def test_two_consecutive_syncs_keep_one_row_per_issue(session):
    """`since`가 포함 경계라 경계에 걸친 이슈는 2회차에 다시 온다. 행은 하나여야 한다.

    1회차에서 두 건을 받아 저장하고 커서를 옮긴다. 2회차는 그 커서로 다시 요청해서
    경계에 걸린 이슈 하나와 새 이슈 하나를 받는다. 겹치는 건이 중복 저장되면
    "방치율"의 분모부터 틀어진다.
    """
    first = _page(
        [_issue(1, "2026-03-01T10:00:00Z"), _issue(2, "2026-03-05T11:30:00Z")],
        ETag='W/"one"',
    )
    # 2회차: since=2026-03-05T11:30:00Z가 포함 경계라 #2가 다시 온다.
    second = _page(
        [_issue(2, "2026-03-05T11:30:00Z"), _issue(3, "2026-03-07T08:00:00Z")],
        ETag='W/"two"',
    )
    handler = _Recorder([first, second])

    with _client(handler) as client:
        run_one = fetch_issues(client, REPO, cursor=None, now=NOW)
        for issue in run_one.batch.issues:
            upsert_issue(session, issue.to_record(REPO))
        save_sync_cursor(session, REPO, ISSUES_RESOURCE, run_one.cursor)
        session.flush()

        cursor = load_sync_cursor(session, REPO, ISSUES_RESOURCE)
        run_two = fetch_issues(client, REPO, cursor=cursor, now=NOW)
        for issue in run_two.batch.issues:
            upsert_issue(session, issue.to_record(REPO))
        save_sync_cursor(session, REPO, ISSUES_RESOURCE, run_two.cursor)
        session.flush()

    assert "since=2026-03-05T11%3A30%3A00Z" in str(handler.requests[1].url)
    assert session.scalar(select(func.count()).select_from(Issue)) == 3
    stored_cursor = load_sync_cursor(session, REPO, ISSUES_RESOURCE)
    assert stored_cursor is not None
    assert stored_cursor.since_cursor == datetime(2026, 3, 7, 8, 0, tzinfo=UTC)


def test_not_modified_round_trip_keeps_the_stored_etag(session):
    """304를 받아도 저장된 ETag와 지문이 짝을 유지해야 다음 회차가 조건부로 나간다."""
    handler = _Recorder(
        [
            _page([_issue(1, "2026-03-01T10:00:00Z")], ETag='W/"one"'),
            httpx2.Response(304, headers={"ETag": 'W/"one"'}),
        ]
    )

    with _client(handler) as client:
        run_one = fetch_issues(client, REPO, cursor=None, now=NOW)
        save_sync_cursor(session, REPO, ISSUES_RESOURCE, run_one.cursor)
        session.flush()

        run_two = fetch_issues(
            client, REPO, cursor=load_sync_cursor(session, REPO, ISSUES_RESOURCE), now=NOW
        )
        save_sync_cursor(session, REPO, ISSUES_RESOURCE, run_two.cursor)
        session.flush()

    assert run_two.not_modified is True
    stored = load_sync_cursor(session, REPO, ISSUES_RESOURCE)
    assert stored is not None
    assert stored.etag == 'W/"one"'
    assert stored.request_fingerprint is not None
