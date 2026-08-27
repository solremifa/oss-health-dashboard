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
class CommentRecord:
    """코멘트 한 건을 저장하기 위한 값.

    전량 보존이 목적이 아니다. 메인테이너 첫 응답을 **무엇을 근거로 판정했는지**
    남겨두면, 판별 규칙이 바뀌었을 때 API를 다시 호출하지 않고 다시 계산할 수 있다.

    Attributes:
        id: GitHub 코멘트 ID.
        issue_id: 코멘트가 달린 이슈.
        body: 본문. 빈 문자열은 되지만 `None`은 안 된다.
        created_at: 작성 시각. 응답 속도 계산의 끝점이다.
        author_login: 작성자 로그인. self-reply 판별에 쓴다.
        author_id: 작성자 ID.
        author_type: 계정 종류. 봇 판별의 1차 기준이다.
        author_association: 작성자와 저장소의 관계. 제3자 판별에 쓴다.
    """

    id: int
    issue_id: int
    body: str
    created_at: datetime
    author_login: str
    author_id: int
    author_type: str
    author_association: str

    def column_values(self) -> dict[str, Any]:
        """`issue_comments` 테이블에 넣을 컬럼 값을 만든다.

        Returns:
            컬럼 이름 -> 값.
        """
        return {
            "id": self.id,
            "issue_id": self.issue_id,
            "body": self.body,
            "created_at": self.created_at,
            "author_login": self.author_login,
            "author_id": self.author_id,
            "author_type": self.author_type,
            "author_association": self.author_association,
        }


@dataclass(frozen=True)
class FirstResponseRecord:
    """메인테이너 첫 응답 판정 결과 한 건.

    ## 이 값이 존재한다는 것 자체가 정보다

    "아직 조사하지 않았다"와 "조사했는데 메인테이너 응답이 없었다"는 전혀 다른
    상태인데, `issues`의 컬럼으로 합치면 둘 다 NULL이 되어 구분할 수 없다
    (`CLAUDE.md` 7절). 그래서 판정 결과를 **행 하나**로 남긴다:

    - 행이 없다 -> 아직 조사하지 않았다.
    - 행이 있고 `responded_at is None` -> 조사했고, 메인테이너 응답이 없었다.

    뒤쪽은 결측이 아니라 **보고해야 할 값**이다. 중앙값에서 제외하되 건수를
    따로 드러낸다. 0으로 채우지 않는다.

    Attributes:
        issue_id: 조사 대상 이슈. 이슈당 한 행이다.
        checked_at: 조사한 시각. 판별 규칙을 바꿨을 때 재조사 대상을 고르는 축이다.
        responded_at: 첫 메인테이너 응답 시각. 응답이 없었으면 `None`.
        comment_id: 판정 근거가 된 코멘트 ID. 응답이 없었으면 `None`.
        responder_login: 응답한 메인테이너. 응답이 없었으면 `None`.
    """

    issue_id: int
    checked_at: datetime
    responded_at: datetime | None = None
    comment_id: int | None = None
    responder_login: str | None = None

    def __post_init__(self) -> None:
        """응답 세 필드가 함께 채워졌거나 함께 비었는지 확인한다.

        DB에도 같은 조건이 CHECK로 걸려 있지만 여기서도 막는다. "응답 시각은
        있는데 누가 응답했는지는 모른다"는 상태는 데이터가 아니라 판정 로직의
        버그이고, 값을 만드는 시점에 터지는 편이 flush 시점보다 원인을 찾기 쉽다.

        Raises:
            ValueError: 셋 중 일부만 채워진 경우.
        """
        filled = {
            self.responded_at is not None,
            self.comment_id is not None,
            self.responder_login is not None,
        }
        if len(filled) != 1:
            raise ValueError(
                "응답 시각·코멘트 ID·응답자는 함께 있거나 함께 없어야 합니다: "
                f"responded_at={self.responded_at!r}, comment_id={self.comment_id!r}, "
                f"responder_login={self.responder_login!r}"
            )

    @property
    def responded(self) -> bool:
        """메인테이너 응답이 있었는지 여부."""
        return self.responded_at is not None

    def column_values(self) -> dict[str, Any]:
        """`issue_first_responses` 테이블에 넣을 컬럼 값을 만든다.

        Returns:
            컬럼 이름 -> 값.
        """
        return {
            "issue_id": self.issue_id,
            "responded_at": self.responded_at,
            "comment_id": self.comment_id,
            "responder_login": self.responder_login,
            "checked_at": self.checked_at,
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
