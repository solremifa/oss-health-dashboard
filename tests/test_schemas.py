"""`app.collectors.schemas` 테스트.

표본과 값은 `docs/findings.md` 3절의 실측 기록에서 가져왔다. `/issues` 한 페이지
100건 중 55건이 PR이었고, 첫 코멘트 작성자 10명 중 5명이 봇이었다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.schemas import (
    CommentSchema,
    IssueSchema,
    is_pull_request,
    parse_comments,
    parse_issues,
)


def _issue(**overrides: Any) -> dict[str, Any]:
    """실제 응답 모양을 흉내 낸 이슈 아이템."""
    item: dict[str, Any] = {
        "id": 3_288_000_001,
        "number": 3288,
        "title": "Client hangs when server closes the stream",
        "body": "Steps to reproduce:\n1. start the server\n",
        "state": "closed",
        "state_reason": "completed",
        "created_at": "2026-03-01T09:15:00Z",
        "updated_at": "2026-03-03T11:20:00Z",
        "closed_at": "2026-03-03T11:20:00Z",
        "comments": 4,
        "user": {"login": "someone", "id": 4242, "type": "User"},
        "author_association": "NONE",
        # 쓰지 않는 키가 30개 남짓 함께 온다. 무시되어야 한다.
        "node_id": "I_kwDOA",
        "locked": False,
        "reactions": {"total_count": 0},
        "timeline_url": "https://api.github.com/repos/o/r/issues/3288/timeline",
    }
    item.update(overrides)
    return item


def _comment(**overrides: Any) -> dict[str, Any]:
    """실제 응답 모양을 흉내 낸 코멘트 아이템."""
    item: dict[str, Any] = {
        "id": 900_001,
        "body": "Thanks for the report — fixed on main.",
        "created_at": "2026-03-01T12:00:00Z",
        "user": {"login": "jlowin", "id": 153, "type": "User"},
        "author_association": "MEMBER",
        "html_url": "https://github.com/o/r/issues/3288#issuecomment-900001",
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# PR 필터 — 함정 1
# ---------------------------------------------------------------------------


def test_pull_request_is_detected_by_key_presence_not_value():
    """값이 아니라 키의 존재 여부로 판별한다."""
    assert is_pull_request({"pull_request": {"url": "https://api.github.com/..."}}) is True
    # 키가 있으면 값이 무엇이든 PR로 본다. 순수 이슈에는 키 자체가 없다.
    assert is_pull_request({"pull_request": None}) is True
    assert is_pull_request({"number": 1}) is False


def test_pull_requests_are_dropped_and_counted():
    """실측에서 한 페이지 100건 중 55건이 PR이었다. 안 거르면 지표가 전부 오염된다."""
    items = [
        _issue(number=1),
        _issue(number=2, pull_request={"url": "https://api.github.com/..."}),
        _issue(number=3, pull_request={"url": "https://api.github.com/..."}),
        _issue(number=4),
    ]

    batch = parse_issues(items)

    assert [issue.number for issue in batch.issues] == [1, 4]
    assert batch.pull_requests_skipped == 2
    assert batch.invalid == []


def test_no_pull_request_flag_is_kept_on_the_schema():
    """PR 행은 영원히 안 읽히는 죽은 데이터라 플래그조차 두지 않는다."""
    assert "is_pull_request" not in IssueSchema.model_fields
    assert "pull_request" not in IssueSchema.model_fields


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


def test_valid_issue_is_parsed_with_the_fields_the_metrics_need():
    batch = parse_issues([_issue()])

    assert len(batch.issues) == 1
    issue = batch.issues[0]
    assert issue.number == 3288
    assert issue.state == "closed"
    assert issue.comments == 4
    assert issue.user.login == "someone"
    assert issue.author_association == "NONE"


def test_unused_response_keys_are_ignored():
    """API는 30개 남짓한 키를 보낸다. 쓰지 않는 것을 들고 다니지 않는다."""
    issue = parse_issues([_issue()]).issues[0]

    assert not hasattr(issue, "timeline_url")
    assert not hasattr(issue, "reactions")


def test_empty_input_gives_an_empty_batch():
    batch = parse_issues([])

    assert batch.issues == []
    assert batch.pull_requests_skipped == 0
    assert batch.invalid == []


# ---------------------------------------------------------------------------
# body: null · 이모지 — 함정 6
# ---------------------------------------------------------------------------


def test_null_body_becomes_empty_string():
    """제약에 NULL이 들어가는 컬럼을 두지 않기 위해서다.

    실측 45건에는 `body: null`이 없었지만 스키마상 nullable이라 관측만으로는
    근거가 부족하다.
    """
    issue = parse_issues([_issue(body=None)]).issues[0]

    assert issue.body == ""


def test_null_title_becomes_empty_string():
    issue = parse_issues([_issue(title=None)]).issues[0]

    assert issue.title == ""


def test_emoji_body_survives_intact():
    """실측 45건 중 10건에 non-ASCII가 있었다."""
    body = "🚀 서버가 죽습니다\n\n再現手順: `uv run server` ①②③"
    issue = parse_issues([_issue(body=body)]).issues[0]

    assert issue.body == body


def test_null_comment_body_becomes_empty_string():
    comment = parse_comments([_comment(body=None)]).comments[0]

    assert comment.body == ""


# ---------------------------------------------------------------------------
# 타임스탬프 — naive 금지
# ---------------------------------------------------------------------------


def test_timestamps_are_timezone_aware_utc():
    issue = parse_issues([_issue()]).issues[0]

    assert issue.created_at == datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
    assert issue.created_at.tzinfo is not None
    assert issue.created_at.utcoffset() == timedelta(0)


def test_non_utc_offset_is_converted_to_utc():
    issue = parse_issues([_issue(created_at="2026-03-01T18:15:00+09:00")]).issues[0]

    assert issue.created_at == datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
    assert issue.created_at.utcoffset() == timedelta(0)


def test_naive_timestamp_is_rejected():
    """naive를 받아두면 6개월 경계 계산이 어딘가에서 9시간씩 밀린다."""
    batch = parse_issues([_issue(created_at="2026-03-01T09:15:00")])

    assert batch.issues == []
    assert len(batch.invalid) == 1
    assert batch.invalid[0].identifier == "#3288"
    assert "timezone" in batch.invalid[0].reason


def test_optional_closed_at_may_be_null():
    issue = parse_issues([_issue(state="open", closed_at=None, state_reason=None)]).issues[0]

    assert issue.closed_at is None
    assert issue.state_reason is None


def test_utc_datetime_type_is_used_for_every_timestamp():
    """한 필드만 빠뜨려도 그 축의 계산이 조용히 틀린다."""
    aware = "2026-03-01T09:15:00+00:00"
    issue = parse_issues([_issue(created_at=aware, updated_at=aware, closed_at=aware)]).issues[0]

    for value in (issue.created_at, issue.updated_at, issue.closed_at):
        assert value is not None
        assert value.tzinfo is not None


# ---------------------------------------------------------------------------
# 실패 경로 — 조용히 빼지 않는다
# ---------------------------------------------------------------------------


def test_missing_required_field_is_collected_not_raised():
    """한 건이 깨졌다고 489건 수집을 통째로 날리지 않는다."""
    broken = _issue(number=7)
    del broken["created_at"]
    items = [_issue(number=1), broken, _issue(number=2)]

    batch = parse_issues(items)

    assert [issue.number for issue in batch.issues] == [1, 2]
    assert len(batch.invalid) == 1
    assert batch.invalid[0].identifier == "#7"
    assert "created_at" in batch.invalid[0].reason


def test_missing_user_is_collected_as_invalid():
    broken = _issue(number=8)
    del broken["user"]

    batch = parse_issues([broken])

    assert batch.issues == []
    assert batch.invalid[0].identifier == "#8"


def test_unknown_state_is_rejected():
    batch = parse_issues([_issue(state="merged")])

    assert batch.issues == []
    assert "state" in batch.invalid[0].reason


def test_item_that_is_not_an_object_is_rejected_without_substring_confusion():
    """문자열에 `in`을 쓰면 부분 문자열 검사가 된다.

    `"pull_request" in "...pull_request..."`가 True가 되어 조용히 PR로 분류된다.
    객체가 아닌 아이템은 그 전에 걸러야 한다.
    """
    batch = parse_issues(["pull_request", 42, None])

    assert batch.issues == []
    assert batch.pull_requests_skipped == 0
    assert len(batch.invalid) == 3
    assert all(item.identifier == "<unknown>" for item in batch.invalid)


def test_invalid_item_without_number_falls_back_to_id():
    broken = _issue()
    del broken["number"]
    del broken["created_at"]

    batch = parse_issues([broken])

    assert batch.invalid[0].identifier == "id=3288000001"


def test_broken_item_logs_a_warning(caplog):
    """조용히 빼지 않는다."""
    broken = _issue(number=9)
    del broken["created_at"]

    with caplog.at_level("WARNING"):
        parse_issues([broken])

    assert any("#9" in record.getMessage() for record in caplog.records)


def test_mixed_batch_reports_every_category():
    """성공·PR·실패를 한 번에 세어 돌려준다."""
    broken = _issue(number=99)
    del broken["user"]
    items = [
        _issue(number=1),
        _issue(number=2, pull_request={"url": "u"}),
        broken,
        _issue(number=3),
        _issue(number=4, pull_request={"url": "u"}),
    ]

    batch = parse_issues(items)

    assert len(batch.issues) == 2
    assert batch.pull_requests_skipped == 2
    assert len(batch.invalid) == 1


# ---------------------------------------------------------------------------
# 코멘트 — 함정 3이 쓸 필드가 남아 있는지
# ---------------------------------------------------------------------------


def test_comment_keeps_the_three_fields_maintainer_detection_needs():
    """봇 제외(`user.type`) · 제3자 제외(`author_association`) · self-reply 제외(`login`)."""
    comment = parse_comments([_comment()]).comments[0]

    assert comment.user.type == "User"
    assert comment.author_association == "MEMBER"
    assert comment.user.login == "jlowin"


def test_bot_comment_is_parsed_not_filtered_here():
    """판별은 #8의 몫이다. 여기서는 판별에 필요한 값이 살아 있는지만 본다.

    실측에서 봇의 `author_association`이 전부 `CONTRIBUTOR`였다 — association만으로는
    봇을 못 거른다는 근거이므로 두 값이 함께 남아야 한다.
    """
    items = [
        _comment(
            id=1,
            user={"login": "coderabbitai[bot]", "id": 1, "type": "Bot"},
            author_association="CONTRIBUTOR",
        ),
        _comment(
            id=2,
            user={"login": "sharabash", "id": 2, "type": "User"},
            author_association="NONE",
        ),
    ]

    batch = parse_comments(items)

    assert len(batch.comments) == 2
    assert batch.comments[0].user.type == "Bot"
    assert batch.comments[0].author_association == "CONTRIBUTOR"
    assert batch.comments[1].user.type == "User"
    assert batch.comments[1].author_association == "NONE"


def test_broken_comment_is_collected_not_raised():
    broken = _comment(id=5)
    del broken["created_at"]

    batch = parse_comments([_comment(id=4), broken])

    assert [comment.id for comment in batch.comments] == [4]
    assert batch.invalid[0].identifier == "id=5"


def test_comment_timestamp_is_utc():
    comment = parse_comments([_comment(created_at="2026-03-01T21:00:00+09:00")]).comments[0]

    assert comment.created_at == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_schemas_are_frozen():
    """검증 계층의 값은 아래 레이어에서 손대지 않는다."""
    issue = parse_issues([_issue()]).issues[0]

    assert IssueSchema.model_config["frozen"] is True
    assert CommentSchema.model_config["frozen"] is True
    assert issue.model_config["frozen"] is True


# ---------------------------------------------------------------------------
# labels — LLM 분류 결과를 대조할 정답지 (수집·저장까지만)
# ---------------------------------------------------------------------------


def test_label_objects_become_names():
    """API는 라벨을 객체 배열로 준다. 정답지에 필요한 것은 이름뿐이다."""
    item = _issue(
        labels=[
            {"id": 1, "name": "bug", "color": "d73a4a", "description": "Something is broken"},
            {"id": 2, "name": "server", "color": "0e8a16"},
        ]
    )

    issue = parse_issues([item]).issues[0]

    assert issue.labels == ("bug", "server")


def test_label_strings_are_accepted_too():
    """일부 응답 형태는 문자열 배열로 온다. 둘 다 받는다."""
    issue = parse_issues([_issue(labels=["bug", "enhancement"])]).issues[0]

    assert issue.labels == ("bug", "enhancement")


def test_labels_default_to_empty():
    """실측 45건 중 1건은 라벨이 비어 있었다. 정상이다."""
    item = _issue()
    item.pop("labels", None)

    assert parse_issues([item]).issues[0].labels == ()
    assert parse_issues([_issue(labels=[])]).issues[0].labels == ()
    assert parse_issues([_issue(labels=None)]).issues[0].labels == ()


def test_duplicate_labels_are_folded_keeping_order():
    """중복은 여기서 접는다. DB의 PK 위반으로 터뜨릴 이유가 없다."""
    issue = parse_issues([_issue(labels=["bug", "server", "bug"])]).issues[0]

    assert issue.labels == ("bug", "server")


def test_label_without_a_name_is_reported_not_skipped():
    """이름을 못 읽는 라벨을 조용히 빼면 정답지에 구멍이 뚫린다.

    그 구멍은 나중에 "LLM이 틀린 것"과 구분되지 않는다. 해당 이슈를 `invalid`에
    담아 드러낸다.
    """
    batch = parse_issues([_issue(number=7, labels=[{"id": 1, "color": "d73a4a"}])])

    assert batch.issues == []
    assert len(batch.invalid) == 1
    assert batch.invalid[0].identifier == "#7"
    assert "labels" in batch.invalid[0].reason


def test_empty_label_name_is_reported():
    """빈 문자열 라벨은 GitHub에 존재할 수 없다. DB CHECK 전에 여기서 잡는다."""
    batch = parse_issues([_issue(number=8, labels=[{"id": 1, "name": ""}])])

    assert batch.issues == []
    assert len(batch.invalid) == 1


def test_labels_are_immutable():
    """모델이 frozen이라 라벨도 tuple로 들고 있어야 한다."""
    issue = parse_issues([_issue(labels=["bug"])]).issues[0]

    assert isinstance(issue.labels, tuple)
