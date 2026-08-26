"""저장 계층이 주고받는 값 객체.

## 왜 `IssueSchema`를 그대로 쓰지 않는가

레이어 의존은 한 방향이다 -- `collectors → models`. `models`가 `collectors`의
`IssueSchema`를 import하면 화살표가 양방향이 되고, 나중에 수집 방식을 바꾸면
저장 계층이 함께 흔들린다(`CLAUDE.md` 5절).

그래서 저장에 필요한 모양을 **저장 계층이 직접 정의하고**, API 응답을 이 모양으로
바꾸는 일은 수집 계층이 한다. 두 모양은 실제로 다르기도 하다 -- `IssueSchema`에는
저장소 이름이 없고(어느 저장소를 긁는지는 요청이 알고 있다), `comments`라는 이름은
DB에서 `comments_count`가 된다.

여기 있는 것은 전부 frozen이다. 저장 직전에 값이 바뀌면 어디서 바뀌었는지 찾기
어렵다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.models.enums import IssueState


@dataclass(frozen=True)
class IssueRecord:
    """이슈 한 건을 저장하기 위한 값.

    Attributes:
        id: GitHub 전역 이슈 ID.
        repo_full_name: `"owner/name"`.
        number: 저장소 안에서의 이슈 번호.
        title: 제목.
        body: 본문. 빈 문자열은 되지만 `None`은 안 된다.
        state: 열림/닫힘.
        state_reason: 종료 사유.
        created_at: 생성 시각.
        updated_at: 갱신 시각.
        closed_at: 종료 시각.
        comments_count: 코멘트 수.
        author_login: 작성자 로그인.
        author_id: 작성자 ID.
        author_type: 계정 종류.
        author_association: 작성자와 저장소의 관계.
        labels: 라벨 이름. LLM 분류 결과와 대조할 정답지다.
    """

    id: int
    repo_full_name: str
    number: int
    title: str
    body: str
    state: IssueState
    state_reason: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    comments_count: int
    author_login: str
    author_id: int
    author_type: str
    author_association: str
    labels: tuple[str, ...] = ()

    def column_values(self) -> dict[str, Any]:
        """`issues` 테이블에 넣을 컬럼 값을 만든다.

        라벨은 별도 테이블이라 여기에 포함하지 않는다.

        Returns:
            컬럼 이름 -> 값.
        """
        return {
            "id": self.id,
            "repo_full_name": self.repo_full_name,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "state_reason": self.state_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "comments_count": self.comments_count,
            "author_login": self.author_login,
            "author_id": self.author_id,
            "author_type": self.author_type,
            "author_association": self.author_association,
        }


@dataclass(frozen=True)
class SyncCursor:
    """증분 수집이 다음 회차로 넘기는 상태.

    ## ETag와 지문은 한 몸이다

    ETag는 요청 헤더 조합에 종속돼서, `Accept`가 다르면 같은 ETag를 보내도 304가
    아니라 200이 오고 **에러 없이 전체를 다시 받는다**(`docs/findings.md` 함정 5).
    그래서 "어떤 요청에 대한 ETag인지"를 지문으로 함께 들고 다닌다.

    DB에도 같은 조건이 CHECK로 걸려 있지만, 여기서도 막는다. 값을 만드는 시점에
    터지는 편이 flush 시점에 터지는 것보다 원인을 찾기 쉽다.

    Attributes:
        etag: 마지막 응답의 ETag.
        request_fingerprint: 그 ETag를 받은 요청의 지문.
        since_cursor: 다음 요청에 넘길 `since` 값. `updated_at` 축이다.
        last_synced_at: 마지막으로 수집을 끝낸 시각.
    """

    etag: str | None = None
    request_fingerprint: str | None = None
    since_cursor: datetime | None = None
    last_synced_at: datetime | None = None

    def __post_init__(self) -> None:
        """ETag와 지문이 짝을 이루는지 확인한다.

        Raises:
            ValueError: 한쪽만 채워진 경우.
        """
        if (self.etag is None) != (self.request_fingerprint is None):
            raise ValueError(
                "ETag와 요청 지문은 함께 있어야 합니다: "
                f"etag={self.etag!r}, request_fingerprint={self.request_fingerprint!r}"
            )
