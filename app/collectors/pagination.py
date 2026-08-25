"""공용 페이지네이터 — `rel="next"`만 따라간다.

GitHub의 페이지네이션은 **엔드포인트마다 방식이 다르다.** 하나를 보고 일반화하면
다른 쪽에서 조용히 틀린다(`docs/findings.md` 함정 4).

| 엔드포인트 | 방식 | Link 헤더 |
|---|---|---|
| 이슈 목록 | 커서 | `rel="next"`뿐. `rel="last"`가 **없고** 불투명한 `after=` 커서가 붙는다 |
| 코멘트 | 오프셋 | `rel="next"` + `rel="last"` |

그래서 두 가지를 하지 않는다:

1. **총 페이지 수를 미리 알려고 하지 않는다.** 커서 기반에는 `rel="last"`가 없다.
   진행률 표시도 만들 수 없다.
2. **`page` 번호를 재조립하지 않는다.** `for page in range(1, N)` 식 구현은 커서를
   빼먹고 잘못된 구간을 받는다. `rel="next"` URL을 **통째로 그대로** 따라가는 것이
   두 방식 모두에서 동작하는 유일한 규칙이다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

import httpx2

from app.collectors.client import GitHubClient
from app.collectors.errors import CollectorError, PaginationLimitError, PaginationLoopError
from app.logging import get_logger

logger = get_logger(__name__)

# 실측 기준 이슈 목록이 ~17페이지(`docs/findings.md` 5절)라 100은 넉넉하다.
# 상한은 잘라내기 위한 값이 아니라 "무한 루프를 알아채기 위한" 값이다.
DEFAULT_MAX_PAGES: Final = 100

_NOT_MODIFIED: Final = 304


def _next_url(response: httpx2.Response) -> str | None:
    """응답의 Link 헤더에서 다음 페이지 URL을 꺼낸다.

    URL을 파싱하거나 재조립하지 않는다. 불투명한 `after=` 커서가 들어 있어서
    손대는 순간 잘못된 구간을 받게 된다.

    Args:
        response: 방금 받은 응답.

    Returns:
        다음 페이지 URL. 마지막 페이지면 `None`.
    """
    if "Link" not in response.headers:
        # 단일 페이지. 정상이다.
        return None

    links = response.links
    next_link = links.get("next")
    if next_link is not None:
        return next_link["url"]

    if not any("rel" in link for link in links.values()):
        # Link 헤더가 왔는데 rel을 하나도 못 읽었다 = 잘렸거나 형식이 깨졌다.
        # 여기서 조용히 멈추면 1페이지만 수집하고 정상 종료한 것처럼 보인다.
        logger.warning(
            "Link 헤더를 해석하지 못해 다음 페이지를 따라가지 못합니다: %r",
            response.headers.get("Link"),
        )
        return None

    # rel="prev"/"last"/"first"만 있는 마지막 페이지. 정상이다.
    return None


def iter_pages(
    client: GitHubClient,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Iterator[httpx2.Response]:
    """`rel="next"`를 따라가며 페이지를 순서대로 내놓는다.

    `params`와 `headers`는 **첫 요청에만** 적용한다. 2페이지부터 쓰는 URL은
    `rel="next"`가 준 완성된 URL이라 쿼리 파라미터가 이미 다 들어 있고, 여기에
    `params`를 다시 얹으면 커서를 덮어쓸 수 있다. `If-None-Match`도 마찬가지로
    첫 페이지 URL에 대한 ETag라서 2페이지에 붙이면 의미가 없다.

    Args:
        client: 요청을 보낼 클라이언트.
        url: 첫 페이지 URL 또는 경로.
        params: 첫 요청의 쿼리 파라미터.
        headers: 첫 요청에만 붙일 헤더.
        max_pages: 허용할 페이지 수 상한.

    Yields:
        페이지 응답. 첫 페이지가 304면 그 응답 하나만 내놓고 끝난다.

    Raises:
        PaginationLimitError: 상한까지 받았는데도 다음 페이지가 남은 경우.
        PaginationLoopError: `rel="next"`가 방금 요청한 URL을 그대로 가리키는 경우.
    """
    next_url: str | None = url
    request_params = params
    request_headers = headers
    page = 0

    while next_url is not None:
        page += 1
        if page > max_pages:
            raise PaginationLimitError(max_pages, url)

        current_url = next_url
        response = client.get(current_url, params=request_params, headers=request_headers)
        yield response

        if response.status_code == _NOT_MODIFIED:
            # 조건부 요청이 "안 바뀌었다"고 답했다. 304에는 Link 헤더가 없다.
            return

        following = _next_url(response)
        if following is not None and following == current_url:
            raise PaginationLoopError(current_url)

        next_url = following
        request_params = None
        request_headers = None

    logger.debug("%d페이지를 받았습니다: %s", page, url)


def iter_items(
    client: GitHubClient,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Iterator[dict[str, Any]]:
    """페이지들을 이어붙여 아이템 하나씩 내놓는다.

    검증은 하지 않는다. 원시 dict를 그대로 넘기고, 스키마 검증과 PR 필터는
    `app.collectors.schemas`가 맡는다.

    Args:
        client: 요청을 보낼 클라이언트.
        url: 첫 페이지 URL 또는 경로.
        params: 첫 요청의 쿼리 파라미터.
        headers: 첫 요청에만 붙일 헤더.
        max_pages: 허용할 페이지 수 상한.

    Yields:
        응답 배열의 아이템.

    Raises:
        CollectorError: 응답 본문이 배열이 아닌 경우.
    """
    for response in iter_pages(client, url, params=params, headers=headers, max_pages=max_pages):
        if response.status_code == _NOT_MODIFIED:
            logger.debug("304 Not Modified — 내놓을 아이템이 없습니다: %s", url)
            continue

        payload = response.json()
        if not isinstance(payload, list):
            raise CollectorError(
                f"목록 응답이 배열이 아닙니다: {type(payload).__name__} (url={url})"
            )
        yield from payload
