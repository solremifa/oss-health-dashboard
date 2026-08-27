"""Anthropic 구조화 출력으로 이슈를 분류한다.

지표 4개 중 둘(**버그 vs 기능요청 비율**, **감정 톤 분포**)은 자연어로만 읽히는
축이라 LLM이 맡는다. 나머지 둘은 집계로 나온다.

## 허용 값은 한 곳에서만 정의한다

`category`와 `sentiment`의 허용 목록은 `app.models.enums`가 단일 출처이고,
**네 곳이 전부 거기서 파생된다**:

1. 프롬프트에 적히는 선택지 -- `_render_options()`
2. API 구조화 출력의 JSON 스키마 -- `build_output_config()`
3. 응답을 검증하는 Pydantic 모델 -- `Classification`
4. DB의 CHECK 제약 -- `sa_enum(...)` (`app/models/tables.py`)

한 곳만 손으로 고치면 나머지와 어긋나는데, 그 어긋남은 "모델이 이상한 값을 냈다"
처럼 보인다. 실제 원인은 스키마와 프롬프트가 다른 말을 하고 있는 것이다.

값에 붙는 설명(`_CATEGORY_GUIDE`)도 enum 멤버가 늘면 함께 채워야 한다. 빠뜨리면
**import 시점에 터진다** -- 설명 없는 선택지를 프롬프트에 흘려보내는 것보다 낫다.

## 프롬프트로 "JSON만 내놔"라고 하지 않는다

`output_config.format`이 스키마를 강제한다. 프롬프트로 형식을 부탁하는 방식은
지켜지는지 확인할 방법이 없고, 안 지켜져도 파싱 실패로만 드러난다
(`CLAUDE.md` 8절).

## 무엇을 재시도하는가

파싱·검증 실패만 재시도한다. **API 오류는 재시도하지 않는다** -- SDK가 이미
재시도하므로 다시 감싸면 재시도가 중첩되고, 레이트리밋이 그만큼 길어진다.
거절(`refusal`)과 잘림(`max_tokens`)도 재시도하지 않는다. 자세한 구분은
`app/analysis/errors.py`에 있다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum as PyEnum
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.analysis.errors import (
    AnalysisError,
    ClassificationParseError,
    ClassificationRefusedError,
    ClassificationTruncatedError,
)
from app.logging import get_logger
from app.models import AnalysisRecord, IssueCategory, IssueSentiment

logger = get_logger(__name__)

# 날짜 접미사를 붙이지 않는다(`CLAUDE.md` 8절).
MODEL: Final = "claude-opus-5"

# 프롬프트나 스키마를 고치면 이 값을 올린다. 결과와 함께 저장되므로, 올리지 않으면
# 조건이 다른 판정이 한 테이블에 섞이고 나중에 구분할 수 없다.
PROMPT_VERSION: Final = "1"

# 라벨 두 개짜리 응답이라 본문 자체는 짧다. 여유를 두는 것은 Opus 5가 기본으로
# adaptive thinking을 하기 때문이다 -- 분류 난이도에 맞춰 effort를 낮게 두되,
# 잘려서 파싱이 깨지는 쪽이 훨씬 비싸므로 상한은 넉넉히 잡는다.
MAX_TOKENS: Final = 4096

# 제목과 본문을 읽고 라벨 두 개를 고르는 일이다. 높은 effort가 정확도를 크게
# 올리지 않는 반면 비용은 그대로 늘어난다.
EFFORT: Final = "low"

# 구조화 출력을 쓰면 파싱 실패가 거의 나지 않는다. 상한은 "안 나는 일이 났을 때
# 한 번 더"에 가깝고, 무한 재시도를 막는 것이 본래 목적이다.
DEFAULT_MAX_ATTEMPTS: Final = 3

_CATEGORY_GUIDE: Final[dict[IssueCategory, str]] = {
    IssueCategory.BUG: "동작이 문서나 기대와 다르게 어긋난다는 보고",
    IssueCategory.FEATURE_REQUEST: "없는 기능을 추가하거나 기존 동작을 바꿔 달라는 요청",
    IssueCategory.QUESTION: "사용법이나 설계 의도를 묻는 질문. 코드 변경을 요구하지 않는다",
    IssueCategory.OTHER: "위 셋 중 어디에도 들어맞지 않는 경우",
}

_SENTIMENT_GUIDE: Final[dict[IssueSentiment, str]] = {
    IssueSentiment.POSITIVE: "고마움이나 칭찬이 드러나는 어조",
    IssueSentiment.NEUTRAL: "감정 표현 없이 사실만 적은 어조. 대부분의 버그 리포트가 여기 해당한다",
    IssueSentiment.FRUSTRATED: "막혀서 답답하거나 불만이 드러나는 어조",
}


def _require_complete(guide: Mapping[Any, str], enum_class: type[PyEnum]) -> None:
    """설명이 enum의 모든 멤버를 덮는지 확인한다.

    멤버를 추가하고 설명을 빠뜨리면 프롬프트에는 선택지가 안 보이는데 스키마에는
    있는 상태가 된다. 모델은 고를 수 없는 값을 받게 되고, 그 결과는 "분류가
    이상하다"로만 나타난다. import 시점에 막는다.

    Args:
        guide: 멤버별 설명.
        enum_class: 덮어야 할 enum.

    Raises:
        ValueError: 설명이 빠진 멤버가 있는 경우.
    """
    missing = [member.value for member in enum_class if member not in guide]
    if missing:
        raise ValueError(f"{enum_class.__name__}의 설명이 빠졌습니다: {missing}")


_require_complete(_CATEGORY_GUIDE, IssueCategory)
_require_complete(_SENTIMENT_GUIDE, IssueSentiment)


def allowed_values(enum_class: type[PyEnum]) -> list[str]:
    """enum의 허용 값 목록을 만든다.

    프롬프트·JSON 스키마·검증이 같은 목록을 보게 하는 지점이다.

    Args:
        enum_class: 대상 enum.

    Returns:
        선언 순서를 유지한 값 목록.
    """
    return [member.value for member in enum_class]


def _render_options(guide: Mapping[Any, str]) -> str:
    """프롬프트에 넣을 선택지 목록을 만든다.

    Args:
        guide: 멤버별 설명.

    Returns:
        `- value: 설명` 줄들을 이어붙인 문자열.
    """
    return "\n".join(f"- {member.value}: {text}" for member, text in guide.items())


SYSTEM_PROMPT: Final = f"""당신은 오픈소스 저장소의 이슈를 분류합니다.
이슈의 제목과 본문을 읽고 두 가지를 판정하세요.

[category] 이슈가 무엇을 요구하는지
{_render_options(_CATEGORY_GUIDE)}

[sentiment] 작성자의 어조
{_render_options(_SENTIMENT_GUIDE)}

판단 기준:
- 본문에 코드나 로그가 붙어 있다는 사실만으로 bug로 보지 마세요. 무엇을 요구하는지가 기준입니다.
- 강한 표현이 없으면 neutral입니다. 버그를 보고한다는 것 자체는 불만이 아닙니다.
- 확신이 서지 않으면 other와 neutral을 고르세요. 억지로 맞추면 분포가 왜곡됩니다."""


def build_output_config() -> dict[str, Any]:
    """`output_config` 값을 만든다. 허용 값은 enum에서 파생시킨다.

    `additionalProperties: False`와 `required`를 함께 넣는다. 둘 중 하나만 있으면
    스키마가 강제하는 범위가 좁아져, 필드가 빠지거나 낯선 필드가 섞인 응답이
    검증을 통과한다.

    Returns:
        `effort`와 `format`이 들어 있는 `output_config` 값.
    """
    return {
        "effort": EFFORT,
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": allowed_values(IssueCategory)},
                    "sentiment": {"type": "string", "enum": allowed_values(IssueSentiment)},
                },
                "required": ["category", "sentiment"],
                "additionalProperties": False,
            },
        },
    }


def build_user_message(title: str, body: str) -> str:
    """분류할 이슈 본문을 담은 사용자 메시지를 만든다.

    본문을 자르지 않는다. 실측 표본에서 본문 길이는 최대 12,630자였고, 잘라내면
    재현 절차나 요청 내용이 뒤쪽에 있는 이슈에서 판정이 뒤집힌다.

    Args:
        title: 이슈 제목.
        body: 이슈 본문. 빈 문자열일 수 있다.

    Returns:
        사용자 메시지 텍스트.
    """
    return f"제목:\n{title}\n\n본문:\n{body if body else '(본문 없음)'}"


class Classification(BaseModel):
    """모델이 돌려준 판정을 검증한 값.

    `extra="forbid"`를 쓴다. 스키마에 `additionalProperties: False`를 걸어 두었으니
    낯선 필드는 오지 않아야 하는데, 오면 스키마와 실제가 어긋났다는 뜻이다.
    그건 조용히 무시할 것이 아니라 드러나야 한다.

    Attributes:
        category: 버그 / 기능요청 / 질문 / 기타.
        sentiment: 긍정 / 중립 / 불만.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: IssueCategory
    sentiment: IssueSentiment


class MessagesResource(Protocol):
    """`client.messages`에서 이 모듈이 실제로 쓰는 부분."""

    def create(self, **kwargs: Any) -> Any:
        """메시지를 생성한다.

        Args:
            **kwargs: Anthropic Messages API 요청 파라미터.

        Returns:
            API 응답 객체.
        """
        ...


class AnthropicClient(Protocol):
    """이 모듈이 클라이언트에 기대하는 최소한의 모양.

    `anthropic.Anthropic`을 직접 타입으로 박지 않는 이유는 테스트 때문이다.
    **테스트는 API를 호출하지 않는다**(`CLAUDE.md` 8절 · 10절). 대역이 흉내 내야
    하는 표면이 `messages.create` 하나뿐이라는 사실을 타입으로 적어두면, 대역이
    SDK 전체를 흉내 낼 필요가 없다는 것이 코드에 드러난다.
    """

    messages: MessagesResource


def parse_classification(text: str) -> Classification:
    """응답 본문을 판정 값으로 바꾼다.

    Args:
        text: 응답의 텍스트 블록 내용.

    Returns:
        검증된 판정.

    Raises:
        ValueError: JSON이 아니거나 객체가 아닌 경우.
        ValidationError: 필드가 빠졌거나 허용 목록 밖의 값인 경우.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON으로 해석할 수 없습니다: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"객체가 아닙니다: {type(payload).__name__}")

    return Classification.model_validate(payload)


def _response_text(response: Any, issue_id: int) -> str | None:
    """응답에서 텍스트 블록을 꺼낸다. **`stop_reason`을 먼저 본다.**

    거절이면 `content`가 비어 있고, `max_tokens`면 잘린 본문이 온다. 본문부터
    읽으면 둘 다 "빈 응답"이나 "깨진 JSON"으로 나타나 원인이 흐려진다.

    Args:
        response: API 응답.
        issue_id: 대상 이슈. 예외 메시지에 쓴다.

    Returns:
        텍스트 블록의 내용. 텍스트 블록이 없으면 `None`.

    Raises:
        ClassificationRefusedError: 모델이 판정을 거절한 경우.
        ClassificationTruncatedError: 응답이 `max_tokens`에 걸려 잘린 경우.
    """
    stop_reason = getattr(response, "stop_reason", None)

    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise ClassificationRefusedError(
            issue_id,
            getattr(details, "category", None),
            getattr(details, "explanation", None),
        )

    if stop_reason == "max_tokens":
        raise ClassificationTruncatedError(issue_id)

    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def classify_issue(
    client: AnthropicClient,
    *,
    issue_id: int,
    title: str,
    body: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> AnalysisRecord:
    """이슈 하나를 분류한다.

    이슈를 ORM 객체가 아니라 값으로 받는다. 이 함수는 DB를 모른다 -- 세션을
    만지는 것은 배치 러너뿐이다(`CLAUDE.md` 5절).

    Args:
        client: Anthropic 클라이언트. 테스트에서는 대역을 넘긴다.
        issue_id: 대상 이슈의 GitHub 전역 ID.
        title: 이슈 제목.
        body: 이슈 본문.
        max_attempts: 파싱·검증 실패를 포함한 총 시도 횟수 상한.
        now: 기록할 분석 시각. 테스트에서 고정하기 위해 주입받는다.

    Returns:
        저장 계층에 넘길 분류 결과.

    Raises:
        ClassificationRefusedError: 모델이 판정을 거절한 경우.
        ClassificationTruncatedError: 응답이 잘린 경우.
        ClassificationParseError: 상한까지 유효한 판정을 받지 못한 경우.
        anthropic.APIError: API 오류. **여기서 잡지 않는다.**
    """
    analyzed_at = now if now is not None else datetime.now(UTC)
    request = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": build_output_config(),
        "messages": [{"role": "user", "content": build_user_message(title, body)}],
    }

    last_reason = "시도하지 않음"
    for attempt in range(1, max_attempts + 1):
        # API 오류는 잡지 않는다. SDK가 이미 재시도했고, 여기서 또 감싸면
        # 재시도가 중첩된다. 토큰 만료 같은 문제는 배치를 멈추는 편이 맞다.
        response = client.messages.create(**request)

        text = _response_text(response, issue_id)
        if text is None:
            last_reason = "텍스트 블록이 없습니다"
        else:
            try:
                classification = parse_classification(text)
            except (ValueError, ValidationError) as exc:
                last_reason = str(exc).replace("\n", " ")[:200]
            else:
                return AnalysisRecord(
                    issue_id=issue_id,
                    category=classification.category,
                    sentiment=classification.sentiment,
                    model=MODEL,
                    prompt_version=PROMPT_VERSION,
                    analyzed_at=analyzed_at,
                )

        logger.warning(
            "이슈 %d의 분류 응답을 해석하지 못해 다시 시도합니다 (%d/%d): %s",
            issue_id,
            attempt,
            max_attempts,
            last_reason,
        )

    raise ClassificationParseError(issue_id, max_attempts, last_reason)


@dataclass(frozen=True)
class ClassificationTarget:
    """분류할 이슈 하나의 입력.

    Attributes:
        issue_id: GitHub 전역 이슈 ID.
        title: 제목.
        body: 본문.
    """

    issue_id: int
    title: str
    body: str


@dataclass(frozen=True)
class ClassificationFailure:
    """분류에 실패해 미분석으로 남은 이슈.

    Attributes:
        issue_id: 대상 이슈.
        reason: 실패 사유.
    """

    issue_id: int
    reason: str


@dataclass(frozen=True)
class ClassificationBatch:
    """분류 한 회차의 결과.

    성공한 것만 담지 않는다. **실패한 이슈를 목록으로 돌려준다.** 미분석 건수를
    분모에서 조용히 빼지 않으려면 그 수가 어딘가에는 남아야 한다
    (`CLAUDE.md` 2절).

    Attributes:
        analyses: 저장할 분류 결과.
        failures: 분류에 실패해 미분석으로 남은 이슈.
    """

    analyses: list[AnalysisRecord] = field(default_factory=list)
    failures: list[ClassificationFailure] = field(default_factory=list)


def classify_issues(
    client: AnthropicClient,
    targets: Iterable[ClassificationTarget],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> ClassificationBatch:
    """여러 이슈를 분류한다. 실패한 건은 ERROR로 남기고 건너뛴다.

    ## 무엇을 건너뛰고 무엇에 멈추는가

    `AnalysisError`만 잡는다. 그건 "이 이슈 하나가 안 된다"는 뜻이라 나머지를
    계속 처리하는 것이 맞다. 반면 **API 오류는 잡지 않고 올린다** -- 토큰이
    만료됐거나 서비스가 죽은 상황에서 계속 돌면 전체가 조용히 미분석으로 남고,
    로그에는 실패 489줄만 쌓인다. 멈춰서 원인을 보여주는 편이 낫다.

    Args:
        client: Anthropic 클라이언트.
        targets: 분류할 이슈들.
        max_attempts: 이슈 하나당 시도 횟수 상한.
        now: 기록할 분석 시각. 테스트에서 고정하기 위해 주입받는다.

    Returns:
        성공한 분류와 실패한 이슈를 함께 담은 결과.

    Raises:
        anthropic.APIError: API 오류. 배치를 멈추고 그대로 올린다.
    """
    analyses: list[AnalysisRecord] = []
    failures: list[ClassificationFailure] = []

    for target in targets:
        try:
            analyses.append(
                classify_issue(
                    client,
                    issue_id=target.issue_id,
                    title=target.title,
                    body=target.body,
                    max_attempts=max_attempts,
                    now=now,
                )
            )
        except AnalysisError as exc:
            # 미분석으로 남긴다. 지어낸 값이나 other로 채우지 않는다.
            logger.error("이슈 %d를 미분석으로 남깁니다: %s", target.issue_id, exc)
            failures.append(ClassificationFailure(target.issue_id, str(exc)))

    logger.info("분류 %d건 성공, %d건 미분석", len(analyses), len(failures))
    return ClassificationBatch(analyses=analyses, failures=failures)
