"""`app.collectors.pagination` 테스트.

두 방식을 모두 재현한다. 이슈 목록은 **커서 기반**이라 `rel="last"`가 없고 불투명한
`after=` 커서가 붙고, 코멘트는 **오프셋 기반**이라 `rel="last"`가 있다
(`docs/findings.md` 함정 4). 한쪽만 테스트하면 다른 쪽에서 조용히 틀린다.
"""

from __future__ import annotations

import httpx2
import pytest

from app.collectors.client import GitHubClient
from app.collectors.errors import CollectorError, PaginationLimitError, PaginationLoopError
from app.collectors.pagination import iter_items, iter_pages

BASE = "https://api.github.com"

# docs/findings.md 함정 4에 실제로 기록된 Link 헤더의 커서.
REAL_CURSOR = "Y3Vyc29yOnYyOpLPAAABnLW2JLjO74rQJw%3D%3D"


class _Pages:
    """요청을 순서대로 받아 미리 짜 넣은 페이지를 돌려주는 핸들러."""

    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        # 짜 넣은 것보다 많이 요청하면 IndexError로 즉시 드러난다.
        return self.responses[len(self.urls) - 1]

    @property
    def call_count(self) -> int:
        return len(self.urls)


def _page(items: list[dict[str, int]], link: str | None = None) -> httpx2.Response:
    headers = {"Link": link} if link is not None else {}
    return httpx2.Response(200, json=items, headers=headers)


def _client(handler: object) -> GitHubClient:
    return GitHubClient(
        "ghp_test_token",
        base_url=BASE,
        transport=httpx2.MockTransport(handler),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# 단일 페이지
# ---------------------------------------------------------------------------


def test_single_page_without_link_header_stops_after_one_request():
    handler = _Pages([_page([{"n": 1}, {"n": 2}])])

    with _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues"))

    assert items == [{"n": 1}, {"n": 2}]
    assert handler.call_count == 1


def test_empty_single_page_yields_nothing():
    handler = _Pages([_page([])])

    with _client(handler) as client:
        assert list(iter_items(client, "/repos/o/r/issues")) == []


# ---------------------------------------------------------------------------
# 커서 기반 (이슈 목록) — rel="last"가 없다
# ---------------------------------------------------------------------------


def test_cursor_pagination_follows_next_without_rel_last():
    next_url = f"{BASE}/repositories/896296825/issues?per_page=100&page=2&after={REAL_CURSOR}"
    handler = _Pages(
        [
            _page([{"n": 1}], link=f'<{next_url}>; rel="next"'),
            _page([{"n": 2}]),
        ]
    )

    with _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues"))

    assert items == [{"n": 1}, {"n": 2}]
    assert handler.call_count == 2


def test_opaque_cursor_is_followed_verbatim():
    """URL을 재조립하면 커서를 빼먹고 잘못된 구간을 받는다."""
    next_url = f"{BASE}/repositories/896296825/issues?per_page=100&page=2&after={REAL_CURSOR}"
    handler = _Pages([_page([], link=f'<{next_url}>; rel="next"'), _page([])])

    with _client(handler) as client:
        list(iter_items(client, "/repos/o/r/issues"))

    second = handler.urls[1]
    assert f"after={REAL_CURSOR}" in second
    assert "page=2" in second


def test_first_page_params_are_not_reapplied_to_later_pages():
    """2페이지 URL은 이미 완성돼 있다. params를 다시 얹으면 커서를 덮어쓴다."""
    # 다음 페이지 URL의 since가 첫 요청의 since와 **다르다**. params가 다시
    # 얹히면 A로 되돌아가므로 그 사고를 정확히 잡아낸다.
    next_url = f"{BASE}/repos/o/r/issues?since=B&page=2"
    handler = _Pages([_page([], link=f'<{next_url}>; rel="next"'), _page([])])

    with _client(handler) as client:
        list(iter_items(client, "/repos/o/r/issues", params={"since": "A"}))

    assert "since=A" in handler.urls[0]
    assert "since=B" in handler.urls[1]
    assert "since=A" not in handler.urls[1]


def test_conditional_headers_apply_to_the_first_page_only():
    """If-None-Match는 첫 페이지 URL에 대한 ETag라 2페이지에 붙이면 의미가 없다."""
    next_url = f"{BASE}/repos/o/r/issues?page=2"
    handler = _Pages([_page([], link=f'<{next_url}>; rel="next"'), _page([])])
    seen: list[str | None] = []

    def recording(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("if-none-match"))
        return handler(request)

    with _client(recording) as client:
        list(iter_items(client, "/repos/o/r/issues", headers={"If-None-Match": 'W/"abc"'}))

    assert seen == ['W/"abc"', None]


# ---------------------------------------------------------------------------
# 오프셋 기반 (코멘트) — rel="last"가 있지만 쓰지 않는다
# ---------------------------------------------------------------------------


def test_offset_pagination_ignores_rel_last_and_follows_next():
    page1 = (
        f'<{BASE}/repos/o/r/issues/1/comments?page=2>; rel="next", '
        f'<{BASE}/repos/o/r/issues/1/comments?page=3>; rel="last"'
    )
    page2 = (
        f'<{BASE}/repos/o/r/issues/1/comments?page=3>; rel="next", '
        f'<{BASE}/repos/o/r/issues/1/comments?page=3>; rel="last"'
    )
    # 마지막 페이지에는 prev/first만 남고 next가 없다.
    page3 = f'<{BASE}/repos/o/r/issues/1/comments?page=2>; rel="prev"'
    handler = _Pages(
        [
            _page([{"n": 1}], link=page1),
            _page([{"n": 2}], link=page2),
            _page([{"n": 3}], link=page3),
        ]
    )

    with _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues/1/comments"))

    assert items == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert handler.call_count == 3


def test_last_page_with_only_prev_and_last_stops_quietly(caplog):
    """next가 없는 마지막 페이지는 정상 종료다. 경고를 남기면 안 된다."""
    link = f'<{BASE}/x?page=1>; rel="first", <{BASE}/x?page=9>; rel="last"'
    handler = _Pages([_page([{"n": 1}], link=link)])

    with caplog.at_level("WARNING"), _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues"))

    assert items == [{"n": 1}]
    assert handler.call_count == 1
    assert caplog.records == []


# ---------------------------------------------------------------------------
# 깨진 Link 헤더
# ---------------------------------------------------------------------------


def test_truncated_link_header_stops_but_warns(caplog):
    """조용히 멈추면 1페이지만 수집하고 정상 종료한 것처럼 보인다."""
    truncated = f"<{BASE}/repositories/896296825/issues?page=2&after={REAL_CURSOR}"
    handler = _Pages([_page([{"n": 1}], link=truncated)])

    with caplog.at_level("WARNING"), _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues"))

    assert items == [{"n": 1}]
    assert handler.call_count == 1
    assert any("Link" in record.message for record in caplog.records)


def test_garbage_link_header_stops_but_warns(caplog):
    handler = _Pages([_page([], link="not a link header at all")])

    with caplog.at_level("WARNING"), _client(handler) as client:
        list(iter_items(client, "/repos/o/r/issues"))

    assert handler.call_count == 1
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_no_link_header_does_not_warn(caplog):
    handler = _Pages([_page([{"n": 1}])])

    with caplog.at_level("WARNING"), _client(handler) as client:
        list(iter_items(client, "/repos/o/r/issues"))

    assert caplog.records == []


# ---------------------------------------------------------------------------
# 상한 · 루프
# ---------------------------------------------------------------------------


def test_page_limit_raises_instead_of_truncating():
    """잘라내고 진행하면 표본이 잘린 채 그럴듯한 지표가 나온다."""
    urls: list[str] = []

    def endless(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        page = len(urls)
        return httpx2.Response(
            200,
            json=[{"n": page}],
            headers={"Link": f'<{BASE}/x?page={page + 1}>; rel="next"'},
        )

    with _client(endless) as client, pytest.raises(PaginationLimitError) as excinfo:
        list(iter_items(client, "/repos/o/r/issues", max_pages=3))

    assert excinfo.value.max_pages == 3
    assert len(urls) == 3  # 상한만큼만 요청하고 멈췄다


def test_exactly_max_pages_succeeds():
    """경계값 — 상한과 페이지 수가 같으면 통과해야 한다."""
    handler = _Pages(
        [
            _page([{"n": 1}], link=f'<{BASE}/x?page=2>; rel="next"'),
            _page([{"n": 2}], link=f'<{BASE}/x?page=3>; rel="next"'),
            _page([{"n": 3}]),
        ]
    )

    with _client(handler) as client:
        items = list(iter_items(client, "/repos/o/r/issues", max_pages=3))

    assert items == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_self_referencing_next_is_caught_on_the_first_lap():
    """상한까지 두면 같은 요청을 수십 번 반복하며 레이트리밋만 태운다."""
    self_url = f"{BASE}/repos/o/r/issues"
    urls: list[str] = []

    def looping(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return httpx2.Response(200, json=[], headers={"Link": f'<{self_url}>; rel="next"'})

    with _client(looping) as client, pytest.raises(PaginationLoopError):
        list(iter_items(client, self_url, max_pages=100))

    assert len(urls) == 1


# ---------------------------------------------------------------------------
# 304 · 예상 밖 본문
# ---------------------------------------------------------------------------


def test_not_modified_yields_the_response_but_no_items():
    """304에는 본문이 없다. .json()을 부르면 터진다."""
    handler = _Pages([httpx2.Response(304)])

    with _client(handler) as client:
        pages = list(iter_pages(client, "/repos/o/r/issues"))

    assert [page.status_code for page in pages] == [304]

    handler = _Pages([httpx2.Response(304)])
    with _client(handler) as client:
        assert list(iter_items(client, "/repos/o/r/issues")) == []


def test_non_list_body_raises():
    """목록 엔드포인트가 배열이 아닌 걸 주면 조용히 넘기지 않는다."""
    handler = _Pages([httpx2.Response(200, json={"message": "Moved Permanently"})])

    with _client(handler) as client, pytest.raises(CollectorError):
        list(iter_items(client, "/repos/o/r/issues"))


def test_pages_are_fetched_lazily():
    """제너레이터다. 소비하지 않으면 요청도 나가지 않는다."""
    handler = _Pages([_page([{"n": 1}], link=f'<{BASE}/x?page=2>; rel="next"'), _page([{"n": 2}])])

    with _client(handler) as client:
        pages = iter_pages(client, "/repos/o/r/issues")
        assert handler.call_count == 0
        next(pages)
        assert handler.call_count == 1
