"""메인테이너 첫 응답 수집 — 봇과 제3자를 걸러낸다.

"메인테이너 응답 속도 = 이슈 생성 -> 첫 코멘트까지의 시간"을 그대로 구현하면 틀린다.
실측 10건에서 **첫 코멘트의 절반이 봇**이었다(`docs/findings.md` 함정 3). CI 봇과 리뷰
봇이 사람보다 먼저 응답하므로, 그대로 재면 응답 속도가 실제보다 극적으로 빨라진다.

## 세 조건이 모두 필요하다

```
comment.user.type == "User"                                    # 봇 제외
and comment.author_association in {OWNER, MEMBER, COLLABORATOR} # 제3자 제외
and comment.user.login != issue.user.login                      # self-reply 제외
```

하나라도 빼면 조용히 틀린다:

- **`user.type`을 빼면** 봇이 첫 응답으로 잡힌다. `author_association`으로는 못 거른다 --
  실측에서 봇들의 association이 전부 `CONTRIBUTOR`였다.
- **`author_association`을 빼면** 지나가던 제3자(`User` + `NONE`)가 메인테이너로 잡힌다.
- **self-reply를 빼면** 작성자가 자기 이슈에 덧붙인 코멘트가 "응답"이 된다. 작성자가
  메인테이너이기도 하면 흔한 일이다.

## 응답이 없는 것도 결과다

메인테이너가 끝내 응답하지 않은 이슈는 **결측이 아니라 의미 있는 값**이다. 그래서
"조사했지만 응답이 없었다"를 `responded_at is None`인 판정 결과로 남긴다. 저장을
건너뛰면 "아직 조사 안 함"과 구분되지 않는다(`CLAUDE.md` 7절).

## 왜 조기 종료하지 않는가

메인테이너 응답을 찾는 즉시 다음 페이지를 안 받으면 요청을 아낄 수 있다. 그러려면
**서버가 코멘트를 작성 순서대로 준다는 가정**에 기대야 하는데, 그 가정이 깨져도
에러는 나지 않는다 -- 나중에 달린 응답이 "첫 응답"으로 잡히고 응답 속도만 조용히
틀어진다. 이 프로젝트에서 가장 피하려는 종류의 실패다.

그래서 페이지를 끝까지 받고 **로컬에서 `created_at` 순으로 정렬해** 가장 이른 것을
고른다. 실측 기준 이슈 1건당 코멘트는 첫 페이지(100건) 안에 들어가므로 대부분의
이슈에서 요청 수는 어차피 1회다(`docs/findings.md` 5절). 값이 비싸지는 것은 코멘트가
100건을 넘는 소수의 이슈뿐이고, 그건 정확도를 위해 낼 만한 값이다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from app.collectors.client import GitHubClient
from app.collectors.pagination import DEFAULT_MAX_PAGES, iter_pages, page_items
from app.collectors.schemas import CommentSchema, InvalidItem, parse_comments
from app.logging import get_logger
from app.models import CommentRecord, FirstResponseRecord

logger = get_logger(__name__)

# 봇 판별의 1차 기준. `login`의 `[bot]` 접미사도 같은 결과를 주지만, 사람이 그런
# 이름을 쓸 수 있으므로 계정 종류를 본다(`docs/findings.md` 함정 3).
USER_ACCOUNT_TYPE: Final = "User"

# 저장소에 소속된 사람만 메인테이너로 본다. `CONTRIBUTOR`는 넣지 않는다 -- 실측에서
# 봇들의 association이 전부 `CONTRIBUTOR`였고, PR을 한 번 보낸 외부인도 여기 들어온다.
MAINTAINER_ASSOCIATIONS: Final = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# 한 페이지 100건이 상한이다. 요청 수를 최소화한다.
PAGE_SIZE: Final = 100


def comments_path(repo_full_name: str, issue_number: int) -> str:
    """이슈 코멘트 엔드포인트 경로를 만든다.

    Args:
        repo_full_name: `"owner/name"` 형식의 대상 저장소.
        issue_number: 저장소 안에서의 이슈 번호. **전역 ID가 아니다.**

    Returns:
        베이스 URL 기준 경로.
    """
    return f"/repos/{repo_full_name}/issues/{issue_number}/comments"


def comment_list_params() -> dict[str, Any]:
    """코멘트 목록 요청의 쿼리 파라미터를 만든다.

    작성 순 오름차순을 요청하지만 **그 순서에 판정을 기대지는 않는다.** 정렬을
    요청하는 것은 페이지가 나뉠 때 앞쪽 페이지에 이른 코멘트가 모이게 해서 읽기
    좋게 하려는 것이고, 첫 응답을 고르는 것은 받은 뒤 로컬 정렬이 한다.

    Returns:
        쿼리 파라미터.
    """
    return {"per_page": PAGE_SIZE, "sort": "created", "direction": "asc"}


def is_maintainer_response(comment: CommentSchema, *, issue_author_login: str) -> bool:
    """코멘트가 메인테이너의 응답인지 판별한다.

    세 조건을 **모두** 만족해야 한다. 근거는 이 모듈의 docstring과
    `docs/findings.md` 함정 3에 있다.

    Args:
        comment: 판별할 코멘트.
        issue_author_login: 이슈 작성자의 로그인. self-reply를 거르는 데 쓴다.

    Returns:
        메인테이너의 응답이면 `True`.
    """
    return (
        comment.user.type == USER_ACCOUNT_TYPE
        and comment.author_association in MAINTAINER_ASSOCIATIONS
        and comment.user.login != issue_author_login
    )


def select_first_response(
    comments: Iterable[CommentSchema], *, issue_author_login: str
) -> CommentSchema | None:
    """메인테이너 응답 중 가장 이른 것을 고른다.

    받은 순서를 신뢰하지 않고 `created_at`으로 정렬한다. 같은 시각에 두 건이
    있으면 코멘트 ID가 작은 쪽을 고른다 -- 어느 쪽을 고르든 응답 시각은 같지만,
    같은 입력에 매번 같은 답이 나와야 재실행이 결과를 흔들지 않는다.

    Args:
        comments: 검증된 코멘트들. 순서는 상관없다.
        issue_author_login: 이슈 작성자의 로그인.

    Returns:
        첫 메인테이너 응답. 해당하는 코멘트가 없으면 `None`.
    """
    candidates = [
        comment
        for comment in comments
        if is_maintainer_response(comment, issue_author_login=issue_author_login)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda comment: (comment.created_at, comment.id))


@dataclass(frozen=True)
class FirstResponseFetch:
    """이슈 한 건에 대한 첫 응답 조사 결과.

    성공한 것만 담지 않는다. 검증에 실패한 코멘트와 실제로 쓴 요청 수까지 드러낸다.
    수집은 부분 실패가 정상인 작업이라 결과 객체가 곧 보고서다.

    Attributes:
        record: 판정 결과. 응답이 없었어도 **항상 존재한다.**
        comments: 판정 근거로 저장할 코멘트들. 규칙이 바뀌면 이것만으로 다시
            계산할 수 있다.
        invalid: 검증에 실패해 건너뛴 코멘트.
        requests_made: 실제로 보낸 요청 수. 코멘트가 0건이면 0이다.
    """

    record: FirstResponseRecord
    comments: list[CommentRecord] = field(default_factory=list)
    invalid: list[InvalidItem] = field(default_factory=list)
    requests_made: int = 0


def _utcnow() -> datetime:
    """현재 시각(UTC aware).

    Returns:
        timezone-aware한 현재 시각.
    """
    return datetime.now(UTC)


def fetch_first_response(
    client: GitHubClient,
    repo_full_name: str,
    *,
    issue_id: int,
    issue_number: int,
    issue_author_login: str,
    comments_count: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    now: datetime | None = None,
) -> FirstResponseFetch:
    """이슈 하나의 메인테이너 첫 응답을 조사한다.

    이슈를 `IssueSchema`가 아니라 값 네 개로 받는다. 조사 대상이 방금 API에서 온
    이슈일 수도 있고 DB에서 읽어온 이슈일 수도 있어서, 어느 한쪽 모양에 묶지 않는다.

    `comments_count`가 0이면 **요청을 보내지 않는다.** 그 값은 같은 이슈 응답에서
    온 것이라 어긋날 여지가 작고, 실측 표본 45건 중 8건이 코멘트 0건이었다. 이슈를
    받은 뒤 코멘트가 달렸다면 다음 회차에 `updated_at`이 바뀌어 다시 조사되므로
    스스로 복구된다.

    Args:
        client: 요청을 보낼 클라이언트.
        repo_full_name: `"owner/name"` 형식의 대상 저장소.
        issue_id: 이슈의 GitHub 전역 ID. 저장에 쓴다.
        issue_number: 저장소 안에서의 이슈 번호. 요청 경로에 쓴다.
        issue_author_login: 이슈 작성자의 로그인. self-reply를 거르는 데 쓴다.
        comments_count: API가 알려준 코멘트 수. `0`이면 요청을 생략한다.
            모르면 `None`을 넘긴다.
        max_pages: 허용할 페이지 수 상한.
        now: 기록할 조사 시각. 테스트에서 고정하기 위해 주입받는다.

    Returns:
        판정 결과와 근거 코멘트.

    Raises:
        CollectorError: 응답이 배열이 아닌 경우.
        GitHubAPIError: 재시도해도 소용없는 4xx 응답인 경우.
        RateLimitError: 레이트리밋 대기 시간이 허용치를 넘은 경우.
        RetryLimitExceededError: 재시도 상한까지 실패한 경우.
    """
    checked_at = now if now is not None else _utcnow()

    if comments_count == 0:
        logger.debug("코멘트가 0건이라 요청을 생략합니다: #%d", issue_number)
        return FirstResponseFetch(
            record=FirstResponseRecord(issue_id=issue_id, checked_at=checked_at)
        )

    path = comments_path(repo_full_name, issue_number)
    items: list[Any] = []
    requests_made = 0

    for response in iter_pages(client, path, params=comment_list_params(), max_pages=max_pages):
        requests_made += 1
        items.extend(page_items(response, path))

    batch = parse_comments(items)
    first = select_first_response(batch.comments, issue_author_login=issue_author_login)

    if first is None:
        # 응답 없음도 결과다. 0으로 채우거나 저장을 건너뛰지 않는다.
        logger.debug(
            "메인테이너 응답이 없습니다 (코멘트 %d건): #%d", len(batch.comments), issue_number
        )
        record = FirstResponseRecord(issue_id=issue_id, checked_at=checked_at)
    else:
        record = FirstResponseRecord(
            issue_id=issue_id,
            checked_at=checked_at,
            responded_at=first.created_at,
            comment_id=first.id,
            responder_login=first.user.login,
        )

    return FirstResponseFetch(
        record=record,
        comments=[comment.to_record(issue_id) for comment in batch.comments],
        invalid=batch.invalid,
        requests_made=requests_made,
    )
