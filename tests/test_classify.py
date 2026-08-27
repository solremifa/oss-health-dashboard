"""`app.analysis.classify` 테스트 — 구조화 분류와 실패 경로.

**API를 호출하지 않는다.** 네트워크와 API 키에 의존하는 테스트를 만들지 않는 것이
이 프로젝트의 규약이고(`CLAUDE.md` 8절 · 10절), 대역(`FakeClient`)에 응답을 미리
짜 넣는다. 대역이 흉내 내는 표면은 `messages.create` 하나뿐이다.

여기서 특히 신경 쓰는 것 두 가지:

1. **허용 목록이 정말 한 곳에서 파생되는가.** 프롬프트·JSON 스키마·Pydantic·DB
   CHECK가 어긋나면 "모델이 이상한 값을 냈다"처럼 보인다. enum을 기준으로 네 곳을
   맞대어 본다.
2. **무엇을 재시도하고 무엇을 재시도하지 않는가.** 거절·잘림·API 오류를 재시도하면
   비용만 쓰거나 재시도가 중첩된다. 실제 호출 횟수를 세어 확인한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.classify import (
    DEFAULT_MAX_ATTEMPTS,
    MODEL,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Classification,
    ClassificationTarget,
    allowed_values,
    build_output_config,
    build_user_message,
    classify_issue,
    classify_issues,
    parse_classification,
)
from app.analysis.errors import (
    AnalysisError,
    ClassificationParseError,
    ClassificationRefusedError,
    ClassificationTruncatedError,
)
from app.models import (
    AnalysisRecord,
    IssueAnalysis,
    IssueCategory,
    IssueRecord,
    IssueSentiment,
    IssueState,
    load_analysis,
    upsert_analysis,
    upsert_issue,
)

REPO = "PrefectHQ/fastmcp"
ISSUE_ID = 3_288_000_001
ISSUE_NUMBER = 3288
ANALYZED_AT = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

TITLE = "Client hangs when server closes the stream"
BODY = "Steps to reproduce:\n1. start the server\n2. kill it mid-stream\n"


# ---------------------------------------------------------------------------
# 대역 — messages.create 하나만 흉내 낸다
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """응답의 콘텐츠 블록."""

    type: str
    text: str = ""


@dataclass
class _StopDetails:
    """거절 응답에 붙는 상세."""

    category: str | None = None
    explanation: str | None = None


@dataclass
class _Response:
    """`messages.create`가 돌려주는 응답."""

    content: list[_Block] = field(default_factory=list)
    stop_reason: str = "end_turn"
    stop_details: _StopDetails | None = None


class _FakeMessages:
    """미리 짜 넣은 응답을 순서대로 돌려준다.

    응답이 떨어지면 마지막 것을 계속 돌려준다 -- "계속 깨진 JSON이 온다"를
    한 줄로 표현하기 위해서다. 예외를 넣으면 그 자리에서 raise한다.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        item = self.responses[index]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """`classify`가 기대하는 최소한의 클라이언트."""

    def __init__(self, *responses: Any) -> None:
        self.messages = _FakeMessages(list(responses))

    @property
    def calls(self) -> int:
        return len(self.messages.requests)


def _text(payload: str) -> _Response:
    return _Response(content=[_Block(type="text", text=payload)])


def _ok(category: str = "bug", sentiment: str = "neutral") -> _Response:
    return _text(json.dumps({"category": category, "sentiment": sentiment}))


def _refusal(category: str | None = "cyber", explanation: str | None = "거절") -> _Response:
    # 거절이면 content가 비어 있다. stop_reason을 먼저 봐야 하는 이유다.
    return _Response(
        content=[], stop_reason="refusal", stop_details=_StopDetails(category, explanation)
    )


def _truncated() -> _Response:
    return _Response(
        content=[_Block(type="text", text='{"category": "bu')], stop_reason="max_tokens"
    )


def _api_error() -> anthropic.APIConnectionError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


def _classify(client: FakeClient, **overrides: Any) -> AnalysisRecord:
    kwargs: dict[str, Any] = {
        "issue_id": ISSUE_ID,
        "title": TITLE,
        "body": BODY,
        "now": ANALYZED_AT,
    }
    kwargs.update(overrides)
    return classify_issue(client, **kwargs)


# ---------------------------------------------------------------------------
# 허용 목록 — 프롬프트 · JSON 스키마 · Pydantic · DB CHECK가 한 곳에서 나온다
# ---------------------------------------------------------------------------


def test_schema_enum_comes_from_the_python_enum():
    schema = build_output_config()["format"]["schema"]

    assert schema["properties"]["category"]["enum"] == [m.value for m in IssueCategory]
    assert schema["properties"]["sentiment"]["enum"] == [m.value for m in IssueSentiment]


def test_schema_pins_both_required_and_additional_properties():
    """둘 중 하나만 있으면 필드가 빠지거나 낯선 필드가 섞인 응답이 통과한다."""
    schema = build_output_config()["format"]["schema"]

    assert schema["required"] == ["category", "sentiment"]
    assert schema["additionalProperties"] is False


def test_every_allowed_value_appears_in_the_prompt():
    """스키마에는 있는데 프롬프트에는 없는 선택지가 생기면 모델이 고를 수 없다."""
    for value in allowed_values(IssueCategory) + allowed_values(IssueSentiment):
        assert value in SYSTEM_PROMPT


def test_allowed_values_keep_declaration_order():
    assert allowed_values(IssueCategory) == ["bug", "feature_request", "question", "other"]
    assert allowed_values(IssueSentiment) == ["positive", "neutral", "frustrated"]


def test_missing_option_description_fails_at_import_time():
    """enum 멤버를 늘리고 설명을 빠뜨리면 import 시점에 터져야 한다."""
    from app.analysis.classify import _require_complete

    with pytest.raises(ValueError, match="설명이 빠졌습니다"):
        _require_complete({IssueCategory.BUG: "설명"}, IssueCategory)


def test_output_config_carries_effort_next_to_format():
    """effort와 format은 같은 output_config 안에 들어간다."""
    config = build_output_config()

    assert "effort" in config
    assert config["format"]["type"] == "json_schema"


# ---------------------------------------------------------------------------
# 요청 — 모델과 구조화 출력
# ---------------------------------------------------------------------------


def test_model_id_has_no_date_suffix():
    assert MODEL == "claude-opus-5"


def test_request_uses_structured_output_not_a_prompt_instruction():
    """스키마를 API가 강제한다. 프롬프트로 형식을 부탁하지 않는다."""
    client = FakeClient(_ok())

    _classify(client)

    request = client.messages.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "JSON" not in SYSTEM_PROMPT


def test_body_is_not_truncated():
    """본문을 자르면 재현 절차가 뒤쪽에 있는 이슈에서 판정이 뒤집힌다."""
    long_body = "x" * 12_630
    client = FakeClient(_ok())

    _classify(client, body=long_body)

    assert long_body in client.messages.requests[0]["messages"][0]["content"]


def test_empty_body_is_labelled_not_dropped():
    message = build_user_message(TITLE, "")

    assert TITLE in message
    assert "(본문 없음)" in message


def test_emoji_body_survives():
    """이슈 본문에 이모지가 흔하다(함정 6)."""
    client = FakeClient(_ok())

    _classify(client, body="🚀 안 됩니다")

    assert "🚀 안 됩니다" in client.messages.requests[0]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


def test_successful_classification_records_its_provenance():
    """model과 prompt_version이 함께 저장되어야 나중에 섞인 데이터를 구분할 수 있다."""
    client = FakeClient(_ok(category="feature_request", sentiment="positive"))

    record = _classify(client)

    assert record.issue_id == ISSUE_ID
    assert record.category is IssueCategory.FEATURE_REQUEST
    assert record.sentiment is IssueSentiment.POSITIVE
    assert record.model == "claude-opus-5"
    assert record.prompt_version == PROMPT_VERSION
    assert record.analyzed_at == ANALYZED_AT
    assert client.calls == 1


def test_thinking_blocks_are_skipped():
    """Opus 5는 기본으로 thinking을 한다. 첫 블록이 text라고 가정하면 안 된다."""
    response = _Response(
        content=[
            _Block(type="thinking", text=""),
            _Block(type="text", text=json.dumps({"category": "bug", "sentiment": "neutral"})),
        ]
    )
    client = FakeClient(response)

    record = _classify(client)

    assert record.category is IssueCategory.BUG


# ---------------------------------------------------------------------------
# 파싱·검증 실패 — 재시도한다
# ---------------------------------------------------------------------------


def test_broken_json_is_retried_and_can_succeed():
    client = FakeClient(_text("this is not json"), _ok())

    record = _classify(client)

    assert record.category is IssueCategory.BUG
    assert client.calls == 2


def test_broken_json_until_the_limit_raises():
    client = FakeClient(_text("{"))

    with pytest.raises(ClassificationParseError) as info:
        _classify(client, max_attempts=3)

    assert client.calls == 3
    assert info.value.attempts == 3
    assert info.value.issue_id == ISSUE_ID


def test_a_value_outside_the_enum_is_rejected():
    """모델이 허용 목록 밖의 값을 내면 그대로 저장하지 않는다."""
    client = FakeClient(_ok(category="documentation"))

    with pytest.raises(ClassificationParseError):
        _classify(client, max_attempts=2)

    assert client.calls == 2


def test_an_outside_value_can_recover_on_retry():
    client = FakeClient(_ok(sentiment="angry"), _ok(sentiment="frustrated"))

    record = _classify(client)

    assert record.sentiment is IssueSentiment.FRUSTRATED


def test_a_missing_field_is_rejected():
    client = FakeClient(_text(json.dumps({"category": "bug"})))

    with pytest.raises(ClassificationParseError):
        _classify(client, max_attempts=1)


def test_an_extra_field_is_rejected():
    """스키마에 additionalProperties: False를 걸어 뒀으니 낯선 필드는 어긋남의 신호다."""
    payload = {"category": "bug", "sentiment": "neutral", "confidence": 0.9}

    with pytest.raises(Exception):  # noqa: B017 - ValidationError만 좁혀 잡을 필요가 없다
        Classification.model_validate(payload)


def test_a_json_array_is_not_a_verdict():
    client = FakeClient(_text("[]"))

    with pytest.raises(ClassificationParseError):
        _classify(client, max_attempts=1)


def test_a_response_without_a_text_block_is_retried():
    client = FakeClient(_Response(content=[]), _ok())

    record = _classify(client)

    assert client.calls == 2
    assert record.category is IssueCategory.BUG


def test_parse_classification_reports_why_it_failed():
    with pytest.raises(ValueError, match="JSON으로 해석할 수 없습니다"):
        parse_classification("nope")

    with pytest.raises(ValueError, match="객체가 아닙니다"):
        parse_classification("[1, 2]")


def test_default_attempt_limit_is_bounded():
    """무한 재시도 금지. 상한이 있다는 것 자체가 규약이다."""
    assert DEFAULT_MAX_ATTEMPTS >= 1

    client = FakeClient(_text("{"))
    with pytest.raises(ClassificationParseError):
        _classify(client)

    assert client.calls == DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# 재시도하지 않는 것들
# ---------------------------------------------------------------------------


def test_refusal_is_not_retried():
    """거절이면 content가 비어 있다. stop_reason을 먼저 보지 않으면 원인이 흐려진다."""
    client = FakeClient(_refusal(category="cyber", explanation="설명"))

    with pytest.raises(ClassificationRefusedError) as info:
        _classify(client)

    assert client.calls == 1
    assert info.value.category == "cyber"
    assert info.value.explanation == "설명"


def test_refusal_without_details_still_reports_the_issue():
    client = FakeClient(_Response(content=[], stop_reason="refusal"))

    with pytest.raises(ClassificationRefusedError) as info:
        _classify(client)

    assert info.value.issue_id == ISSUE_ID
    assert info.value.category is None


def test_truncated_response_is_not_retried():
    """같은 지점에서 또 잘린다. 고칠 곳은 횟수가 아니라 요청이다."""
    client = FakeClient(_truncated())

    with pytest.raises(ClassificationTruncatedError):
        _classify(client)

    assert client.calls == 1


def test_api_errors_are_not_retried_or_wrapped():
    """SDK가 이미 재시도했다. 여기서 또 감싸면 재시도가 중첩된다."""
    client = FakeClient(_api_error())

    with pytest.raises(anthropic.APIConnectionError):
        _classify(client)

    assert client.calls == 1


def test_api_errors_are_not_analysis_errors():
    """배치가 AnalysisError만 잡으므로, API 오류가 거기 섞이면 전체가 조용히 미분석이 된다."""
    assert not isinstance(_api_error(), AnalysisError)


# ---------------------------------------------------------------------------
# 배치 — 실패는 ERROR로 남기고 건너뛴다
# ---------------------------------------------------------------------------


def _targets() -> list[ClassificationTarget]:
    return [
        ClassificationTarget(issue_id=1, title="a", body="a"),
        ClassificationTarget(issue_id=2, title="b", body="b"),
    ]


def test_a_failed_issue_is_skipped_and_reported(caplog):
    """실패한 이슈는 미분석으로 남는다. other로 채워 성공한 척하지 않는다."""
    client = FakeClient(_refusal(), _ok())

    with caplog.at_level(logging.ERROR):
        batch = classify_issues(client, _targets(), now=ANALYZED_AT)

    assert [record.issue_id for record in batch.analyses] == [2]
    assert [failure.issue_id for failure in batch.failures] == [1]
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_a_parse_failure_also_leaves_the_issue_unanalyzed():
    client = FakeClient(_text("{"))

    batch = classify_issues(client, _targets(), max_attempts=1, now=ANALYZED_AT)

    assert batch.analyses == []
    assert len(batch.failures) == 2


def test_an_api_error_stops_the_batch():
    """토큰 만료로 489건이 전부 조용히 미분석이 되는 것보다 멈추는 편이 낫다."""
    client = FakeClient(_api_error())

    with pytest.raises(anthropic.APIConnectionError):
        classify_issues(client, _targets(), now=ANALYZED_AT)

    assert client.calls == 1


def test_an_empty_batch_is_not_an_error():
    batch = classify_issues(FakeClient(_ok()), [], now=ANALYZED_AT)

    assert batch.analyses == []
    assert batch.failures == []


# ---------------------------------------------------------------------------
# 저장까지 — enum이 DB CHECK와 같은 곳에서 나온다
# ---------------------------------------------------------------------------


def _issue_record() -> IssueRecord:
    return IssueRecord(
        id=ISSUE_ID,
        repo_full_name=REPO,
        number=ISSUE_NUMBER,
        title=TITLE,
        body=BODY,
        state=IssueState.OPEN,
        state_reason=None,
        created_at=ANALYZED_AT,
        updated_at=ANALYZED_AT,
        closed_at=None,
        comments_count=0,
        author_login="someone",
        author_id=4242,
        author_type="User",
        author_association="NONE",
    )


def test_a_classification_round_trips_through_the_database(session: Session):
    client = FakeClient(_ok(category="question", sentiment="frustrated"))
    record = _classify(client)

    upsert_issue(session, _issue_record())
    upsert_analysis(session, record)
    session.flush()

    stored = load_analysis(session, ISSUE_ID)
    assert stored == record
    assert stored is not None
    assert stored.model == "claude-opus-5"
    assert stored.prompt_version == PROMPT_VERSION


def test_reanalysis_replaces_the_previous_verdict(session: Session):
    """프롬프트를 고쳐 다시 돌리면 이슈당 행은 그대로 하나다."""
    upsert_issue(session, _issue_record())
    upsert_analysis(session, _classify(FakeClient(_ok())))

    updated = _classify(FakeClient(_ok(category="question", sentiment="positive")))
    upsert_analysis(session, updated)
    session.flush()

    assert session.scalar(select(func.count()).select_from(IssueAnalysis)) == 1
    assert load_analysis(session, ISSUE_ID) == updated


def test_an_unanalyzed_issue_has_no_row(session: Session):
    """행이 없다 = 미분석. 지표 응답이 그 건수를 드러낸다."""
    upsert_issue(session, _issue_record())
    session.flush()

    assert load_analysis(session, ISSUE_ID) is None


@pytest.mark.parametrize("category", list(IssueCategory))
def test_every_enum_value_is_storable(session: Session, category: IssueCategory):
    """프롬프트가 제시하는 값 중에 DB CHECK가 거부하는 것이 있으면 안 된다."""
    upsert_issue(session, _issue_record())
    client = FakeClient(_ok(category=category.value))

    upsert_analysis(session, _classify(client))
    session.flush()

    stored = load_analysis(session, ISSUE_ID)
    assert stored is not None
    assert stored.category is category


@pytest.mark.parametrize("sentiment", list(IssueSentiment))
def test_every_sentiment_value_is_storable(session: Session, sentiment: IssueSentiment):
    upsert_issue(session, _issue_record())
    client = FakeClient(_ok(sentiment=sentiment.value))

    upsert_analysis(session, _classify(client))
    session.flush()

    stored = load_analysis(session, ISSUE_ID)
    assert stored is not None
    assert stored.sentiment is sentiment
