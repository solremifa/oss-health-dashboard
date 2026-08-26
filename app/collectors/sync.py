"""증분 수집 — `since` 커서와 ETag 조건부 요청.

**이 모듈은 DB에 쓰지 않는다.** 저장된 커서를 값으로 받아서, 받아온 이슈와 **다음
회차에 저장할 새 커서**를 함께 돌려준다. 실제 저장과 트랜잭션 경계는 호출자가
정한다(`CLAUDE.md` 5절 · 7절).

## 수집 축과 집계 축이 다르다

`since` 파라미터는 `created_at`이 아니라 **`updated_at` 기준**이다
(`docs/findings.md` 함정 2). 실측에서 `since=2026-02-24`로 요청했더니 2024년에
생성된 이슈가 딸려 왔다 -- 오래전에 열렸지만 최근에 코멘트가 달린 것들이다.

그래서 두 시각의 역할을 나눈다:

- `updated_at` -> **무엇을 가져올지** 정하는 축 (이 모듈)
- `created_at` -> **무엇을 셀지** 정하는 축 (집계)

여기서 둘을 합치려 하면 증분 수집이 깨지거나 지표가 틀린다.

## 왜 `sort=updated&direction=asc`인가

**받은 것의 최대 `updated_at`이 그대로 다음 커서가 되기 때문이다.** 오름차순으로
받으면 "여기까지 받았다"는 지점 이전은 전부 받은 것이 보장되므로, 그 지점을 다음
`since`로 삼아도 빠지는 구간이 없다.

내림차순이었다면 첫 페이지에 가장 최근 이슈가 오고, 그 시각을 커서로 삼는 순간
**아직 받지 않은 아래쪽 구간을 통째로 건너뛴다.** 커서는 앞서 나가고 데이터는
비는데, 로그에는 "수집 성공"만 남는다.

`since`는 포함(inclusive) 경계라 경계에 걸친 이슈는 다음 회차에 한 번 더 온다.
저장이 멱등(upsert)이라 문제가 되지 않는다. 반대로 1초를 더해 건너뛰면 같은 초에
갱신된 다른 이슈를 영원히 놓친다.

## ETag는 요청 지문과 함께 다룬다

ETag는 요청 헤더 조합에 종속된다. `Accept`가 다르면 같은 ETag로도 304가 아니라
200이 오고 **에러 없이 전체를 다시 받는다**(`docs/findings.md` 함정 5). 증분 수집이
작동하는 것처럼 보이면서 매번 전체 재수집으로 퇴화하고, 로그를 봐도 정상으로 보인다.

그래서 ETag를 쓰기 전에 **저장된 지문과 지금 보낼 요청의 지문이 같은지** 확인하고,
다르면 조용히 폐기한다. 폐기는 손해가 아니다 -- 전체를 다시 받는 것이 잘못된 304를
믿는 것보다 낫다.

## 304도 요청 1회로 센다

GitHub 문서는 304가 레이트리밋에 계상되지 않는다고 하지만 실측은 반대였다
(`docs/findings.md` 함정 5). "304는 공짜"라는 가정 위에 예산을 세우지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Final

from app.collectors.client import ACCEPT_HEADER, API_VERSION_HEADER, GitHubClient
from app.collectors.pagination import DEFAULT_MAX_PAGES, iter_pages, page_items
from app.collectors.schemas import IssueBatch, parse_issues
from app.logging import get_logger
from app.models import SyncCursor

logger = get_logger(__name__)

# `sync_state.resource` 값. 저장소당 여러 커서를 두기 위한 구분자다.
ISSUES_RESOURCE: Final = "issues"

# 한 페이지 100건이 상한이다. 요청 수를 최소화한다.
PAGE_SIZE: Final = 100

_NOT_MODIFIED: Final = 304

# GitHub이 받는 형식. `datetime.isoformat()`은 `+00:00`을 붙이는데, 그것도 통하지만
# 지문 계산에 들어가는 문자열이라 표현을 하나로 고정해 둔다.
_TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def issue_list_path(repo_full_name: str) -> str:
    """이슈 목록 엔드포인트 경로를 만든다.

    Args:
        repo_full_name: `"owner/name"` 형식의 대상 저장소.

    Returns:
        베이스 URL 기준 경로.
    """
    return f"/repos/{repo_full_name}/issues"


def issue_list_params(since: datetime | None) -> dict[str, Any]:
    """이슈 목록 요청의 쿼리 파라미터를 만든다.

    `state=all`로 받는다. 닫힌 이슈가 있어야 "응답 속도"와 "방치율"의 분모를 제대로
    셀 수 있다.

    Args:
        since: 이 시각 이후에 **갱신된** 이슈만 받는다. `None`이면 전량 수집이다.

    Returns:
        쿼리 파라미터.
    """
    params: dict[str, Any] = {
        "state": "all",
        "per_page": PAGE_SIZE,
        # 중간에 실패해도 받은 데까지가 연속 구간이 되도록 갱신 시각 오름차순으로 받는다.
        "sort": "updated",
        "direction": "asc",
    }
    if since is not None:
        params["since"] = since.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
    return params


def build_request_fingerprint(path: str, params: Mapping[str, Any]) -> str:
    """ETag와 함께 저장할 요청 지문을 만든다.

    ETag가 "어떤 요청에 대한 것인지"를 식별한다. 경로·쿼리 파라미터뿐 아니라
    **`Accept`와 API 버전 헤더까지** 넣는 것이 핵심이다 -- 실측에서 ETag가 갈린
    원인이 바로 `Accept` 헤더였다.

    Args:
        path: 요청 경로.
        params: 쿼리 파라미터.

    Returns:
        64자 hex 문자열.
    """
    canonical = json.dumps(
        {
            "path": path,
            # 값 타입이 int든 str이든 같은 요청이므로 문자열로 통일한다.
            "params": {key: str(value) for key, value in sorted(params.items())},
            "accept": ACCEPT_HEADER,
            "api_version": API_VERSION_HEADER,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def conditional_headers(cursor: SyncCursor | None, fingerprint: str) -> dict[str, str]:
    """조건부 요청 헤더를 만든다. 지문이 다르면 ETag를 버린다.

    Args:
        cursor: 저장된 커서. 처음 수집이면 `None`.
        fingerprint: 지금 보낼 요청의 지문.

    Returns:
        `If-None-Match`가 들어 있거나 비어 있는 헤더.
    """
    if cursor is None or cursor.etag is None:
        return {}

    if cursor.request_fingerprint != fingerprint:
        # 조용히 넘기면 안 되는 지점이다. 잘못된 ETag를 그대로 보내면 200이 오고
        # 전체를 다시 받는데, 그건 에러가 아니라서 로그에 아무것도 남지 않는다.
        logger.info(
            "요청이 지난번과 달라 저장된 ETag를 폐기합니다 (저장된 지문=%s, 현재=%s)",
            cursor.request_fingerprint,
            fingerprint,
        )
        return {}

    return {"If-None-Match": cursor.etag}


@dataclass(frozen=True)
class IssueFetch:
    """이슈 증분 수집 한 회차의 결과.

    성공한 것만 담지 않는다. 304였는지, 요청을 몇 번 썼는지까지 드러낸다. 수집은
    부분 실패가 정상인 작업이라 결과 객체가 곧 보고서다.

    Attributes:
        batch: 검증된 이슈와, 버린 PR 수·실패한 아이템.
        cursor: 이 회차의 결과를 반영한 **저장할 커서**.
        not_modified: 첫 응답이 304였는지 여부.
        requests_made: 실제로 보낸 요청 수. **304도 1회로 센다.**
    """

    batch: IssueBatch
    cursor: SyncCursor
    not_modified: bool
    requests_made: int


def _utcnow() -> datetime:
    """현재 시각(UTC aware).

    Returns:
        timezone-aware한 현재 시각.
    """
    return datetime.now(UTC)


def fetch_issues(
    client: GitHubClient,
    repo_full_name: str,
    *,
    cursor: SyncCursor | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    now: datetime | None = None,
) -> IssueFetch:
    """저장된 커서 이후로 갱신된 이슈를 받아온다.

    Args:
        client: 요청을 보낼 클라이언트.
        repo_full_name: `"owner/name"` 형식의 대상 저장소.
        cursor: 지난 회차의 커서. 처음 수집이면 `None`.
        max_pages: 허용할 페이지 수 상한.
        now: 기록할 수집 시각. 테스트에서 고정하기 위해 주입받는다.

    Returns:
        받아온 이슈와 다음 회차에 쓸 커서.

    Raises:
        CollectorError: 응답이 배열이 아닌 경우.
        GitHubAPIError: 재시도해도 소용없는 4xx 응답인 경우.
        RateLimitError: 레이트리밋 대기 시간이 허용치를 넘은 경우.
        RetryLimitExceededError: 재시도 상한까지 실패한 경우.
    """
    synced_at = now if now is not None else _utcnow()
    path = issue_list_path(repo_full_name)
    params = issue_list_params(cursor.since_cursor if cursor is not None else None)
    fingerprint = build_request_fingerprint(path, params)
    headers = conditional_headers(cursor, fingerprint)

    items: list[Any] = []
    etag: str | None = None
    not_modified = False
    requests_made = 0

    for index, response in enumerate(
        iter_pages(client, path, params=params, headers=headers, max_pages=max_pages)
    ):
        # 304도 쿼터를 소모한다. 문서와 실측이 달랐다(함정 5).
        requests_made += 1

        if index == 0:
            # ETag는 첫 페이지 요청에 대한 것이다. 2페이지부터의 ETag를 저장하면
            # 다음 회차의 첫 요청에 붙어 아무 의미 없는 조건부 요청이 된다.
            etag = response.headers.get("ETag")

        if response.status_code == _NOT_MODIFIED:
            not_modified = True
            break

        items.extend(page_items(response, path))

    if not_modified:
        logger.info("304 Not Modified — 갱신된 이슈가 없습니다: %s", repo_full_name)
        # 커서를 그대로 두고 수집 시각만 갱신한다. ETag를 새로 받은 것으로 바꾸지
        # 않는 이유는, 304 응답의 ETag가 없을 수도 있는데 그때 지문만 남으면
        # 짝이 깨지기 때문이다.
        return IssueFetch(
            batch=IssueBatch(),
            cursor=_advance(cursor, etag=None, fingerprint=None, latest=None, synced_at=synced_at),
            not_modified=True,
            requests_made=requests_made,
        )

    batch = parse_issues(items)
    latest = max((issue.updated_at for issue in batch.issues), default=None)

    logger.info(
        "이슈 %d건 수집(PR %d건 제외, 실패 %d건), 요청 %d회: %s",
        len(batch.issues),
        batch.pull_requests_skipped,
        len(batch.invalid),
        requests_made,
        repo_full_name,
    )

    return IssueFetch(
        batch=batch,
        cursor=_advance(
            cursor,
            etag=etag,
            fingerprint=fingerprint,
            latest=latest,
            synced_at=synced_at,
        ),
        not_modified=False,
        requests_made=requests_made,
    )


def _advance(
    cursor: SyncCursor | None,
    *,
    etag: str | None,
    fingerprint: str | None,
    latest: datetime | None,
    synced_at: datetime,
) -> SyncCursor:
    """이번 회차 결과를 반영한 새 커서를 만든다.

    `since`는 **실제로 본 이슈의 최대 `updated_at`**으로만 전진시킨다. 현재 시각으로
    당기면 수집이 도는 동안 갱신된 이슈를 영원히 놓친다 -- 그 이슈는 이미 지나간
    구간에 속하게 되어 다음 회차의 `since`에 걸리지 않는다.

    받은 이슈가 없으면 커서를 그대로 둔다. 뒤로 물러나지도, 앞서 나가지도 않는다.

    검증에 실패한 아이템은 커서 계산에서 빠진다. 그래서 계속 실패하는 이슈가 있으면
    그 지점 이후로 커서가 나아가지 못하고 매 회차 같은 구간을 다시 받는다. 의도한
    동작이다 -- **저장하지 못한 데이터를 지나쳐 버리는 것보다 다시 받는 편이 낫다.**
    실패는 `IssueBatch.invalid`와 WARNING 로그로 드러나므로 조용히 반복되지 않는다.

    Args:
        cursor: 지난 회차의 커서.
        etag: 이번에 받은 ETag. 없으면 `None`.
        fingerprint: 그 ETag에 대응하는 요청 지문. `etag`와 짝이어야 한다.
        latest: 이번에 본 이슈의 최대 `updated_at`.
        synced_at: 이번 수집 시각.

    Returns:
        저장할 새 커서.
    """
    previous_since = cursor.since_cursor if cursor is not None else None
    since = latest if latest is not None else previous_since

    if etag is None:
        # 지문만 남기면 DB CHECK에 걸린다. 짝을 맞춰 둘 다 버리거나, 지난 값을
        # 그대로 유지한다.
        if cursor is not None and cursor.etag is not None:
            return SyncCursor(
                etag=cursor.etag,
                request_fingerprint=cursor.request_fingerprint,
                since_cursor=since,
                last_synced_at=synced_at,
            )
        return SyncCursor(since_cursor=since, last_synced_at=synced_at)

    return SyncCursor(
        etag=etag,
        request_fingerprint=fingerprint,
        since_cursor=since,
        last_synced_at=synced_at,
    )
