"""GitHub API 응답 검증 · 정규화 (Pydantic v2).

수집 계층의 마지막 단계다. 페이지네이터가 넘긴 원시 dict를 검증된 스키마로 바꾸고,
**PR을 걸러낸다.**

## 왜 PR을 거르는가

GitHub은 Pull Request를 Issue의 하위 타입으로 취급해서 `/issues` 엔드포인트가 PR도
함께 돌려준다. 실측에서 **한 페이지 100건 중 55건이 PR**이었다(`docs/findings.md`
함정 1). 안 거르면 지표 4개가 전부 오염된다 — 특히 "버그 vs 기능요청 비율"은 PR
본문이 섞여 분모부터 틀린다.

`is_pull_request` 같은 플래그를 스키마에 두지 않는다. 지표 4개가 전부 이슈 기반이라
PR 행은 영원히 안 읽히는 죽은 데이터가 된다. **수집 시점에 버린다.**

## 부분 실패를 감추지 않는다

한 건이 검증에 실패했다고 489건 수집을 통째로 날리지 않는다. 대신 **조용히 빼지도
않는다.** 실패한 건을 `IssueBatch.invalid`에 담아 돌려주고 WARNING을 남긴다.
버린 PR 수도 `pull_requests_skipped`로 드러낸다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, field_validator

from app.logging import get_logger
from app.models import IssueRecord, IssueState

logger = get_logger(__name__)

_MAX_REASON_LENGTH: Final = 200


def _require_utc(value: datetime) -> datetime:
    """timezone-aware UTC datetime만 통과시킨다.

    naive datetime을 받아두면 "이 시각이 UTC인가 로컬인가"가 코드 어딘가에서
    한 번은 틀린다. 6개월 경계로 이슈를 자르는 계산이 9시간씩 밀린다.

    Args:
        value: 검증할 datetime.

    Returns:
        UTC로 맞춘 datetime.

    Raises:
        ValueError: timezone 정보가 없는 경우.
    """
    if value.tzinfo is None:
        raise ValueError("timezone 정보가 없는 datetime은 받지 않습니다")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class GitHubUser(BaseModel):
    """이슈·코멘트 작성자.

    `type`을 enum으로 받지 않는다. 현재 관측되는 값은 `User` / `Bot` /
    `Organization`이지만 GitHub이 값을 늘리면(`Mannequin` 등) 검증이 통째로
    실패한다. 메인테이너 판별(#8)에 필요한 건 `== "User"` 비교뿐이라 문자열로
    충분하다.

    Attributes:
        login: 로그인 이름.
        id: 사용자 ID.
        type: 계정 종류. 봇 판별의 1차 기준.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    login: str
    id: int
    type: str


class IssueSchema(BaseModel):
    """검증된 이슈 하나.

    지표 4개와 메인테이너 판별(#8)에 필요한 필드만 남긴다. API는 30개 남짓한 키를
    보내지만 쓰지 않는 것을 들고 다니면 DB 스키마와 마이그레이션까지 따라 커진다.

    `labels`는 **이름만** 담는다. LLM 분류 결과를 대조할 정답지로 쓸 계획이라
    수집·저장은 하되(`docs/findings.md` 3절), 대조 분석 자체는 현재 스코프 밖이다.
    색상·설명 같은 나머지 필드는 그 용도에 쓰이지 않으므로 버린다.

    Attributes:
        id: 전역 이슈 ID.
        number: 저장소 안에서의 이슈 번호.
        title: 제목. LLM 분류 입력.
        body: 본문. LLM 분류 입력. `null`은 빈 문자열로 정규화한다.
        state: `open` 또는 `closed`.
        state_reason: 종료 사유. 없을 수 있다.
        created_at: 생성 시각. **지표 집계의 기준 축**이다.
        updated_at: 갱신 시각. `since` 파라미터가 거르는 축이라 증분 수집에 쓴다.
        closed_at: 종료 시각.
        comments: 코멘트 수.
        user: 작성자. self-reply 판별에 쓴다.
        author_association: 작성자와 저장소의 관계.
        labels: 라벨 이름. 순서를 유지하고 중복을 제거한다.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    number: int
    title: str
    body: str
    state: Literal["open", "closed"]
    state_reason: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    comments: int
    user: GitHubUser
    author_association: str
    # 모델이 frozen이라 list가 아니라 tuple로 받는다.
    labels: tuple[str, ...] = ()

    @field_validator("labels", mode="before")
    @classmethod
    def _extract_label_names(cls, value: object) -> object:
        """라벨 객체 배열에서 이름만 뽑는다.

        GitHub은 보통 `[{"id": .., "name": "bug", ...}]` 형태로 주지만 일부
        엔드포인트·옵션에서는 문자열 배열로 준다. 둘 다 받는다.

        이름을 읽을 수 없는 항목은 **건너뛰지 않고 예외를 올린다.** 조용히 빼면
        정답지에 구멍이 뚫린 채로 나중에 LLM 분류 정확도를 재게 되고, 그 구멍은
        분류가 틀린 것과 구분되지 않는다. 예외로 올리면 해당 이슈가
        `IssueBatch.invalid`에 사유와 함께 남는다.

        Args:
            value: 원본 `labels` 값.

        Returns:
            중복을 제거하고 순서를 유지한 이름 tuple.

        Raises:
            ValueError: 이름을 읽을 수 없거나 빈 문자열인 항목이 있는 경우.
        """
        if value is None:
            return ()
        if not isinstance(value, list):
            # 배열이 아니면 Pydantic이 타입 오류로 보고하게 둔다.
            return value

        names: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, Mapping):
                raw = entry.get("name")
                if not isinstance(raw, str):
                    raise ValueError(f"라벨에서 이름을 읽을 수 없습니다: {entry!r}")
                name = raw
            else:
                raise ValueError(f"라벨 항목의 형식을 알 수 없습니다: {entry!r}")

            if not name:
                raise ValueError("빈 문자열 라벨은 받지 않습니다")
            if name not in names:
                # GitHub 라벨 이름은 저장소 안에서 유일하지만, 중복이 오더라도
                # 여기서 접는다. DB의 PK 위반으로 터뜨릴 이유가 없다.
                names.append(name)

        return tuple(names)

    @field_validator("body", "title", mode="before")
    @classmethod
    def _null_to_empty(cls, value: object) -> object:
        """`null`을 빈 문자열로 정규화한다.

        DB 제약에 NULL이 들어가는 컬럼을 두지 않기 위한 것이다. NULL끼리는 서로
        다르다고 보아 UNIQUE가 조용히 무력화된다(`CLAUDE.md` 7절). 실측 45건에는
        `body: null`이 없었지만 스키마상 nullable이라 관측만으로는 근거가 부족하다.

        Args:
            value: 원본 값.

        Returns:
            `None`이면 빈 문자열, 아니면 원본 그대로.
        """
        return "" if value is None else value

    def to_record(self, repo_full_name: str) -> IssueRecord:
        """저장 계층이 받는 값 객체로 바꾼다.

        이 변환이 **수집 쪽에** 있는 이유는 의존 방향 때문이다. `models`는 어떤
        레이어도 import하지 않으므로 `IssueSchema`를 알 수 없고, 반대로 수집 쪽은
        `models`의 스키마를 참조해도 된다(`CLAUDE.md` 5절).

        저장소 이름은 인자로 받는다. API 응답에도 `repository_url`이 들어 있지만
        "어느 저장소를 긁고 있는가"는 요청을 보낸 쪽이 이미 아는 값이라, 응답을
        파싱해서 되짚을 이유가 없다.

        Args:
            repo_full_name: `"owner/name"` 형식의 대상 저장소.

        Returns:
            저장 계층에 넘길 값 객체.
        """
        return IssueRecord(
            id=self.id,
            repo_full_name=repo_full_name,
            number=self.number,
            title=self.title,
            body=self.body,
            state=IssueState(self.state),
            state_reason=self.state_reason,
            created_at=self.created_at,
            updated_at=self.updated_at,
            closed_at=self.closed_at,
            # API의 `comments`는 개수다. DB에서는 코멘트 테이블과 헷갈리지 않도록
            # `comments_count`로 이름을 바꾼다.
            comments_count=self.comments,
            author_login=self.user.login,
            author_id=self.user.id,
            author_type=self.user.type,
            author_association=self.author_association,
            labels=self.labels,
        )


class CommentSchema(BaseModel):
    """검증된 코멘트 하나.

    메인테이너 첫 응답 판별(#8)에 필요한 세 가지를 모두 담는다 — `user.type`(봇 제외),
    `author_association`(제3자 제외), `user.login`(self-reply 제외).

    Attributes:
        id: 코멘트 ID.
        body: 본문. `null`은 빈 문자열로 정규화한다.
        created_at: 작성 시각. 응답 속도 계산의 끝점.
        user: 작성자.
        author_association: 작성자와 저장소의 관계.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    body: str
    created_at: UtcDatetime
    user: GitHubUser
    author_association: str

    @field_validator("body", mode="before")
    @classmethod
    def _null_to_empty(cls, value: object) -> object:
        """`null`을 빈 문자열로 정규화한다.

        Args:
            value: 원본 값.

        Returns:
            `None`이면 빈 문자열, 아니면 원본 그대로.
        """
        return "" if value is None else value


@dataclass(frozen=True)
class InvalidItem:
    """검증에 실패해 건너뛴 아이템.

    Attributes:
        identifier: 이슈 번호나 ID. 그것조차 읽을 수 없으면 `"<unknown>"`.
        reason: 실패 사유 요약.
    """

    identifier: str
    reason: str


@dataclass(frozen=True)
class IssueBatch:
    """이슈 목록 파싱 결과.

    성공한 것만 돌려주지 않는다. **버린 것의 수를 함께 돌려준다.** 수집은 부분
    실패가 정상인 작업이라, 실패를 결과 객체에 명시적으로 담지 않으면 나중에
    "왜 489건이 아니라 487건이지"에 답할 수 없다.

    Attributes:
        issues: 검증을 통과한 이슈.
        pull_requests_skipped: PR이라 버린 아이템 수.
        invalid: 검증에 실패해 건너뛴 아이템.
    """

    issues: list[IssueSchema] = field(default_factory=list)
    pull_requests_skipped: int = 0
    invalid: list[InvalidItem] = field(default_factory=list)


@dataclass(frozen=True)
class CommentBatch:
    """코멘트 목록 파싱 결과.

    Attributes:
        comments: 검증을 통과한 코멘트.
        invalid: 검증에 실패해 건너뛴 아이템.
    """

    comments: list[CommentSchema] = field(default_factory=list)
    invalid: list[InvalidItem] = field(default_factory=list)


def is_pull_request(item: Mapping[str, Any]) -> bool:
    """아이템이 Pull Request인지 판별한다.

    **값이 아니라 키의 존재 여부로 판별한다**(`docs/findings.md` 함정 1).
    PR에는 `pull_request` 키가 붙고 순수 이슈에는 키 자체가 없다. 값으로 판별하면
    (`item.get("pull_request")` 같은) 형태에 따라 흔들린다.

    Args:
        item: `/issues` 응답의 아이템 하나.

    Returns:
        PR이면 `True`.
    """
    return "pull_request" in item


def _identify(item: Mapping[str, Any]) -> str:
    """검증에 실패한 아이템을 로그에서 찾을 수 있게 식별자를 뽑는다.

    Args:
        item: 원시 아이템.

    Returns:
        `"#123"` 또는 `"id=456"`. 둘 다 없으면 `"<unknown>"`.
    """
    number = item.get("number")
    if isinstance(number, int):
        return f"#{number}"
    identifier = item.get("id")
    if isinstance(identifier, int):
        return f"id={identifier}"
    return "<unknown>"


def _summarize(error: ValidationError) -> str:
    """`ValidationError`를 한 줄로 줄인다.

    Args:
        error: Pydantic 검증 오류.

    Returns:
        `필드: 사유` 형식을 세미콜론으로 이은 문자열.
    """
    parts = [
        f"{'.'.join(str(location) for location in detail['loc']) or '<root>'}: {detail['msg']}"
        for detail in error.errors()
    ]
    summary = "; ".join(parts)
    if len(summary) > _MAX_REASON_LENGTH:
        return summary[:_MAX_REASON_LENGTH] + "..."
    return summary


def parse_issues(items: Iterable[Any]) -> IssueBatch:
    """`/issues` 응답 아이템을 검증하고 PR을 걸러낸다.

    Args:
        items: 페이지네이터가 넘긴 원시 아이템.

    Returns:
        검증된 이슈와 함께, 버린 PR 수와 실패한 아이템을 담은 결과.
    """
    issues: list[IssueSchema] = []
    invalid: list[InvalidItem] = []
    pull_requests_skipped = 0

    for item in items:
        if not isinstance(item, Mapping):
            invalid.append(InvalidItem("<unknown>", f"객체가 아닙니다: {type(item).__name__}"))
            logger.warning("이슈 아이템이 객체가 아닙니다: %r", item)
            continue

        if is_pull_request(item):
            pull_requests_skipped += 1
            continue

        try:
            issues.append(IssueSchema.model_validate(item))
        except ValidationError as exc:
            identifier = _identify(item)
            reason = _summarize(exc)
            invalid.append(InvalidItem(identifier, reason))
            logger.warning("이슈 검증에 실패해 건너뜁니다 (%s): %s", identifier, reason)

    logger.debug(
        "이슈 %d건 검증, PR %d건 제외, 실패 %d건",
        len(issues),
        pull_requests_skipped,
        len(invalid),
    )
    return IssueBatch(issues=issues, pull_requests_skipped=pull_requests_skipped, invalid=invalid)


def parse_comments(items: Iterable[Any]) -> CommentBatch:
    """코멘트 응답 아이템을 검증한다.

    코멘트 엔드포인트에는 PR이 섞이지 않으므로 필터가 없다.

    Args:
        items: 페이지네이터가 넘긴 원시 아이템.

    Returns:
        검증된 코멘트와 실패한 아이템을 담은 결과.
    """
    comments: list[CommentSchema] = []
    invalid: list[InvalidItem] = []

    for item in items:
        if not isinstance(item, Mapping):
            invalid.append(InvalidItem("<unknown>", f"객체가 아닙니다: {type(item).__name__}"))
            logger.warning("코멘트 아이템이 객체가 아닙니다: %r", item)
            continue

        try:
            comments.append(CommentSchema.model_validate(item))
        except ValidationError as exc:
            identifier = _identify(item)
            reason = _summarize(exc)
            invalid.append(InvalidItem(identifier, reason))
            logger.warning("코멘트 검증에 실패해 건너뜁니다 (%s): %s", identifier, reason)

    return CommentBatch(comments=comments, invalid=invalid)
