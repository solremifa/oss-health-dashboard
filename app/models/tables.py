"""SQLAlchemy ORM 테이블 정의.

## 이 스키마가 지키려는 것

**데이터 무결성의 최종 판정자는 DB 제약이다**(`CLAUDE.md` 7절). 저장 전에 SELECT로
확인하는 방식은 확인과 삽입 사이에 다른 트랜잭션이 끼어들면 샌다. 그래서 지켜야 할
불변 조건은 전부 제약으로 박고, 코드는 위반을 **처리만** 한다.

## 테이블 구성

| 테이블 | 역할 |
|---|---|
| `issues` | 순수 이슈만. **PR은 저장하지 않는다** |
| `issue_labels` | 이슈에 붙은 라벨 이름. LLM 분류 결과와 대조할 정답지 |
| `issue_comments` | 첫 응답 판정의 근거가 되는 코멘트 |
| `issue_first_responses` | 메인테이너 첫 응답 판정 결과 (#8) |
| `sync_state` | 증분 수집 커서와 ETag (#7) |

## PR 행을 두지 않는다

`/issues` 응답의 절반 이상이 PR이지만(`docs/findings.md` 함정 1) `is_pull_request`
플래그 컬럼도 두지 않는다. 지표 4개가 전부 이슈 기반이라 PR 행은 **영원히 안 읽히는
죽은 데이터**가 되고, 대신 모든 집계 쿼리에 `WHERE is_pull_request = false`를
빠짐없이 붙여야 하는 부담만 남는다. 한 곳이라도 빠뜨리면 조용히 틀린 숫자가 나온다.

## 왜 `repo_full_name`을 들고 다니는가

대상 저장소는 현재 한 곳이지만 `TARGET_REPO`는 환경변수라 바뀔 수 있다. 이슈
`number`는 **저장소 안에서만** 유일하므로, 저장소를 바꿔 돌리는 순간 다른 저장소의
#100과 충돌한다. 저장소 테이블을 따로 만들 만큼의 요구는 없어서(스코프는 저장소
한 곳) 컬럼 하나로 범위만 정확히 잡아둔다.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import IssueState, sa_enum
from app.models.types import UtcDateTime

# GitHub의 login·association 같은 식별자 길이. 넉넉하게 잡되 무제한으로 두지 않는다.
_IDENTIFIER_LENGTH = 255
_SHORT_TOKEN_LENGTH = 64


class Issue(Base):
    """수집한 이슈 하나. PR은 여기에 들어오지 않는다.

    작성자 정보를 별도 `users` 테이블로 정규화하지 않고 컬럼으로 펼친다. 필요한
    것은 self-reply 판별(`author_login`)과 봇 판별(`author_type`)뿐이고, 사용자
    자체를 조회하거나 갱신할 일이 없어서다. 정규화하면 조인만 늘고 얻는 것이 없다.

    Attributes:
        id: GitHub 전역 이슈 ID. 저장소를 옮겨도 바뀌지 않아 PK로 쓴다.
        repo_full_name: `"owner/name"`. `number`의 유효 범위를 정한다.
        number: 저장소 안에서의 이슈 번호.
        title: 제목. LLM 분류 입력.
        body: 본문. API의 `null`은 빈 문자열로 정규화해서 넣는다.
        state: 열림/닫힘.
        state_reason: 종료 사유. 없을 수 있다.
        created_at: 생성 시각. **지표 집계의 기준 축**.
        updated_at: 갱신 시각. 증분 수집(`since`)이 거르는 축.
        closed_at: 종료 시각.
        comments_count: API가 알려준 코멘트 수.
        author_login: 작성자 로그인. self-reply 판별에 쓴다.
        author_id: 작성자 ID.
        author_type: `User` / `Bot` / `Organization` 등.
        author_association: 작성자와 저장소의 관계.
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    repo_full_name: Mapped[str] = mapped_column(String(_IDENTIFIER_LENGTH), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL이다. API는 body에 null을 보낼 수 있지만 빈 문자열로 정규화해서 넣는다.
    # 제약에 NULL이 섞이면 NULL끼리는 서로 다르다고 보아 조건이 조용히 무력화된다.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[IssueState] = mapped_column(
        sa_enum(IssueState, name="issue_state"), nullable=False
    )
    state_reason: Mapped[str | None] = mapped_column(String(_SHORT_TOKEN_LENGTH))

    created_at: Mapped[UtcDateTime] = mapped_column(UtcDateTime, nullable=False, index=True)
    updated_at: Mapped[UtcDateTime] = mapped_column(UtcDateTime, nullable=False, index=True)
    closed_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime)

    comments_count: Mapped[int] = mapped_column(Integer, nullable=False)

    author_login: Mapped[str] = mapped_column(String(_IDENTIFIER_LENGTH), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_type: Mapped[str] = mapped_column(String(_SHORT_TOKEN_LENGTH), nullable=False)
    author_association: Mapped[str] = mapped_column(String(_SHORT_TOKEN_LENGTH), nullable=False)

    __table_args__ = (
        # 같은 저장소 안에서 이슈 번호는 유일하다. GitHub id와는 별개의 축이라
        # 둘 다 건다 -- 한쪽만 맞고 다른 쪽이 어긋난 행은 수집 버그다.
        UniqueConstraint("repo_full_name", "number"),
        CheckConstraint("comments_count >= 0", name="comments_count_non_negative"),
        CheckConstraint("number > 0", name="number_positive"),
    )


class IssueLabel(Base):
    """이슈에 붙은 라벨 이름 하나.

    ## 왜 정규화 테이블인가 (JSON 컬럼이 아니라)

    JSON 배열 컬럼 하나로 두면 저장은 간단하지만 **"bug 라벨이 붙은 이슈"를 세는
    쿼리가 DB마다 달라진다** -- SQLite는 `json_each`, PostgreSQL은 `jsonb` 연산자다.
    이 프로젝트는 SQLite로 시작해 PostgreSQL로 옮기기로 되어 있어서(`CLAUDE.md` 3절)
    이관 시점에 쿼리를 전부 다시 써야 한다. 행으로 풀어두면 양쪽에서 같은 SQL이 돈다.

    중복 라벨도 PK가 막아준다. JSON 배열이었다면 코드가 매번 걸러야 한다.

    ## 지금은 저장까지만 한다

    LLM 분류 결과와 대조할 **정답지**로 쓸 계획이지만(`docs/findings.md` 3절),
    대조 분석 자체는 이번 스코프가 아니다. 수집·저장만 한다.

    Attributes:
        issue_id: 라벨이 붙은 이슈.
        name: 라벨 이름. GitHub 라벨은 저장소 안에서 이름이 유일하다.
    """

    __tablename__ = "issue_labels"

    issue_id: Mapped[int] = mapped_column(
        BigInteger,
        # 이슈가 사라지면 라벨도 의미가 없다. 고아 행을 남기지 않는다.
        ForeignKey("issues.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    name: Mapped[str] = mapped_column(String(_IDENTIFIER_LENGTH), primary_key=True)

    __table_args__ = (
        # 빈 문자열 라벨은 GitHub에 존재할 수 없다. 들어왔다면 파싱이 틀린 것이다.
        CheckConstraint("length(name) > 0", name="name_not_empty"),
    )


class IssueComment(Base):
    """이슈에 달린 코멘트 하나.

    전량을 보존하려는 테이블이 아니다. 메인테이너 첫 응답을 판정하려고 **실제로
    받아본 코멘트**만 담는다(실측상 이슈 대부분이 첫 페이지 100건 안에 들어간다).
    판정 근거를 남겨두면 판별 규칙이 바뀌었을 때 **API를 다시 호출하지 않고**
    다시 계산할 수 있다.

    Attributes:
        id: GitHub 코멘트 ID.
        issue_id: 달린 이슈.
        body: 본문. null은 빈 문자열로 정규화해서 넣는다.
        created_at: 작성 시각. 응답 속도 계산의 끝점.
        author_login: 작성자 로그인. self-reply 판별.
        author_id: 작성자 ID.
        author_type: 봇 판별의 1차 기준.
        author_association: 제3자 판별.
    """

    __tablename__ = "issue_comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    issue_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[UtcDateTime] = mapped_column(UtcDateTime, nullable=False)

    author_login: Mapped[str] = mapped_column(String(_IDENTIFIER_LENGTH), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_type: Mapped[str] = mapped_column(String(_SHORT_TOKEN_LENGTH), nullable=False)
    author_association: Mapped[str] = mapped_column(String(_SHORT_TOKEN_LENGTH), nullable=False)


class IssueFirstResponse(Base):
    """메인테이너 첫 응답 판정 결과 (#8).

    ## 왜 `issues`의 컬럼으로 합치지 않는가

    합치면 **"아직 조사 안 함"과 "조사했는데 메인테이너 응답이 없음"이 둘 다 NULL**이
    되어 구분할 수 없다(`CLAUDE.md` 7절). 둘은 전혀 다른 상태다 -- 앞은 수집이 덜 된
    것이고, 뒤는 지표에 보고해야 할 **의미 있는 값**이다.

    이 테이블에서는 두 상태가 다음과 같이 갈린다:

    - **행이 없다** -> 아직 조사하지 않았다.
    - **행이 있고 `responded_at IS NULL`** -> 조사했고, 메인테이너 응답이 없었다.
      중앙값 계산에서 제외하되 건수를 별도로 보고한다. 0으로 채우지 않는다.

    Attributes:
        issue_id: 조사 대상 이슈. 이슈당 한 행이라 PK를 겸한다.
        responded_at: 첫 메인테이너 응답 시각. 응답이 없었으면 `None`.
        comment_id: 판정 근거가 된 코멘트 ID. 응답이 없었으면 `None`.
        responder_login: 응답한 메인테이너. 응답이 없었으면 `None`.
        checked_at: 조사한 시각. 판별 규칙을 바꿨을 때 재조사 대상을 고르는 축.
    """

    __tablename__ = "issue_first_responses"

    issue_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("issues.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )

    responded_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime)
    # issue_comments.id로 FK를 걸지 않는다. 이 컬럼은 "무엇을 근거로 판정했는가"를
    # 남기는 감사 흔적이고, 코멘트 본문과는 수명이 다르다. FK를 걸면 코멘트를
    # 정리하는 순간 CASCADE로 판정 기록까지 사라지거나 RESTRICT로 정리가 막힌다.
    comment_id: Mapped[int | None] = mapped_column(BigInteger)
    responder_login: Mapped[str | None] = mapped_column(String(_IDENTIFIER_LENGTH))

    checked_at: Mapped[UtcDateTime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        # 세 컬럼은 함께 채워지거나 함께 비어야 한다. "응답 시각은 있는데 누가
        # 응답했는지는 모른다"는 상태는 판정 로직의 버그이지 데이터가 아니다.
        CheckConstraint(
            "(responded_at IS NULL AND comment_id IS NULL AND responder_login IS NULL)"
            " OR (responded_at IS NOT NULL AND comment_id IS NOT NULL"
            " AND responder_login IS NOT NULL)",
            name="response_fields_all_or_none",
        ),
    )


class SyncState(Base):
    """증분 수집 상태 (#7).

    ## ETag를 요청 지문과 함께 저장하는 이유

    ETag는 **요청 헤더 조합에 종속된다.** `Accept`가 다르면 같은 ETag를 보내도
    304가 아니라 200이 오고, **에러 없이 전체 본문을 다시 받는다**
    (`docs/findings.md` 함정 5). 증분 수집이 작동하는 것처럼 보이면서 실제로는 매번
    전체 재수집으로 퇴화하고, 로그를 봐도 정상으로 보인다.

    그래서 ETag 단독으로는 저장하지 않는다. "어떤 요청에 대한 ETag인지"를 지문으로
    함께 남기고, 다음 수집에서 지문이 다르면 ETag를 폐기한다. 제약으로도 못 박아
    한쪽만 있는 행이 생기지 않게 한다.

    Attributes:
        repo_full_name: 대상 저장소.
        resource: 수집 대상 종류(`"issues"` 등). 저장소당 여러 커서를 둔다.
        etag: 마지막 응답의 ETag. 없으면 `None`.
        request_fingerprint: 그 ETag를 받은 요청의 지문. 없으면 `None`.
        since_cursor: 다음 수집에 넘길 `since` 값(=`updated_at` 축).
        last_synced_at: 마지막으로 수집을 끝낸 시각.
    """

    __tablename__ = "sync_state"

    repo_full_name: Mapped[str] = mapped_column(String(_IDENTIFIER_LENGTH), primary_key=True)
    resource: Mapped[str] = mapped_column(String(_SHORT_TOKEN_LENGTH), primary_key=True)

    etag: Mapped[str | None] = mapped_column(String(_IDENTIFIER_LENGTH))
    request_fingerprint: Mapped[str | None] = mapped_column(String(_SHORT_TOKEN_LENGTH))

    since_cursor: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime)
    last_synced_at: Mapped[UtcDateTime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        # 지문 없는 ETag는 쓸 수 없고, ETag 없는 지문은 의미가 없다.
        CheckConstraint(
            "(etag IS NULL) = (request_fingerprint IS NULL)",
            name="etag_requires_fingerprint",
        ),
    )
