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

from app.models.enums import IssueCategory, IssueSentiment, IssueState


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
class AnalysisRecord:
    """LLM 분류 결과 한 건.

    `category`와 `sentiment`를 문자열이 아니라 enum으로 들고 다닌다. 허용 값은
    `app.models.enums`가 단일 출처이고, 프롬프트·API 스키마·이 값 객체·DB CHECK가
    전부 거기서 파생된다(`CLAUDE.md` 8절).

    `model`과 `prompt_version`은 선택 항목이 아니다. 둘 없이 저장하면 조건이 다른
    결과가 한 테이블에 섞이고 나중에 구분할 수 없다.

    Attributes:
        issue_id: 분석 대상 이슈.
        category: 버그 / 기능요청 / 질문 / 기타.
        sentiment: 긍정 / 중립 / 불만.
        model: 판정에 쓴 모델 ID.
        prompt_version: 판정에 쓴 프롬프트 버전.
        analyzed_at: 분석한 시각.
    """

    issue_id: int
    category: IssueCategory
    sentiment: IssueSentiment
    model: str
    prompt_version: str
    analyzed_at: datetime

    def __post_init__(self) -> None:
        """출처 정보가 비어 있지 않은지 확인한다.

        Raises:
            ValueError: `model`이나 `prompt_version`이 빈 문자열인 경우.
        """
        if not self.model:
            raise ValueError("model은 비어 있을 수 없습니다")
        if not self.prompt_version:
            raise ValueError("prompt_version은 비어 있을 수 없습니다")

    def column_values(self) -> dict[str, Any]:
        """`issue_analyses` 테이블에 넣을 컬럼 값을 만든다.

        Returns:
            컬럼 이름 -> 값.
        """
        return {
            "issue_id": self.issue_id,
            "category": self.category,
            "sentiment": self.sentiment,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "analyzed_at": self.analyzed_at,
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


@dataclass(frozen=True)
class IssueFacts:
    """지표 계산에 필요한 사실만 모은 **조회 결과** 하나.

    ## 왜 저장 계층이 이 모양을 정의하는가

    나머지 record들은 저장 계층이 **받는** 모양이고, 이건 저장 계층이 **내주는**
    모양이다. 방향만 반대일 뿐 이유는 같다 -- 레이어 의존은 한 방향이라
    (`analysis → models`) `models`가 `analysis`의 타입을 알 수 없다. 조회 결과의
    모양을 저장 계층이 정의하고 분석 계층이 거기에 맞춘다.

    세 테이블(`issues` · `issue_first_responses` · `issue_analyses`)에서 온 값이
    한 줄에 섞여 있지만, **어느 테이블에서 왔는지는 지표에 중요하지 않다.**
    중요한 것은 "무엇을 아직 모르는가"이고 그건 아래 세 상태로 구분된다.

    ## `None`이 두 가지를 뜻하지 않게 한다

    | 상태 | 표현 |
    |---|---|
    | 첫 응답을 아직 조사하지 않음 | `first_response_checked is False` |
    | 조사했고 메인테이너 응답이 없었음 | `checked is True` + `responded_at is None` |
    | 조사했고 응답이 있었음 | `checked is True` + `responded_at` 있음 |

    가운데 상태는 결측이 아니라 **보고해야 할 값**이다. 중앙값에서 빼되 건수를
    드러낸다. 0으로 채우지 않는다(`CLAUDE.md` 2절).

    분석 결과도 같다 -- `category`가 `None`이면 **미분석**이고, 그 건수는 지표
    응답에 드러난다. 분모에서 조용히 빼지 않는다.

    Attributes:
        issue_id: 이슈의 GitHub 전역 ID.
        created_at: 생성 시각. **집계 기간을 자르는 축이다**(`updated_at`이 아니다).
        state: 열림/닫힘. 방치 판정의 조건 중 하나다.
        first_response_checked: 첫 응답을 조사했는지 여부.
            `issue_first_responses`에 행이 있으면 `True`.
        responded_at: 첫 메인테이너 응답 시각. 조사했지만 응답이 없었으면 `None`.
        category: LLM 분류. 미분석이면 `None`.
        sentiment: LLM 감정 톤. 미분석이면 `None`.
    """

    issue_id: int
    created_at: datetime
    state: IssueState
    first_response_checked: bool = False
    responded_at: datetime | None = None
    category: IssueCategory | None = None
    sentiment: IssueSentiment | None = None

    def __post_init__(self) -> None:
        """구분해야 할 상태들이 뭉개진 값인지 확인한다.

        전부 "값을 만든 쪽의 버그"이지 데이터가 아니다. 지표 계산 중에 이상한
        숫자로 나타나는 것보다 값을 만드는 시점에 터지는 편이 원인을 찾기 쉽다.

        Raises:
            ValueError: 조사하지 않았는데 응답 시각이 있거나, 응답 시각이 생성
                시각보다 이르거나, `category`와 `sentiment` 중 한쪽만 채워진 경우.
        """
        if self.responded_at is not None and not self.first_response_checked:
            raise ValueError(
                "조사하지 않은 이슈에 응답 시각이 있을 수 없습니다: "
                f"issue_id={self.issue_id}, responded_at={self.responded_at!r}"
            )
        if self.responded_at is not None and self.responded_at < self.created_at:
            raise ValueError(
                "응답 시각이 이슈 생성 시각보다 이릅니다: "
                f"issue_id={self.issue_id}, created_at={self.created_at!r}, "
                f"responded_at={self.responded_at!r}"
            )
        if (self.category is None) != (self.sentiment is None):
            raise ValueError(
                "분류와 감정 톤은 함께 있거나 함께 없어야 합니다: "
                f"issue_id={self.issue_id}, category={self.category!r}, "
                f"sentiment={self.sentiment!r}"
            )

    @property
    def analyzed(self) -> bool:
        """LLM 분석이 끝났는지 여부."""
        return self.category is not None

    @property
    def responded(self) -> bool:
        """메인테이너 응답이 확인됐는지 여부.

        **조사하지 않은 이슈도 `False`다.** 이 값만으로 "응답 없음"을 세면
        미조사가 섞이므로, 세는 쪽은 `first_response_checked`를 함께 본다.
        """
        return self.responded_at is not None
