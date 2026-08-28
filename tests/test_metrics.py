"""지표 4개 계산 테스트.

이 파일이 고정하려는 것은 **조용히 틀릴 수 있는 네 지점**이다. 전부 코드가
에러 없이 돌면서 다른 숫자를 내놓는 종류라, 테스트로 못 박아두지 않으면
"대시보드에 그래프가 그려졌다"만으로는 맞는지 알 수 없다.

1. **6개월 경계값** — 경계를 포함으로 볼지 배제로 볼지에 따라 경계에 걸친 이슈가
   들어왔다 나갔다 한다. 경계 근처에서만 틀리므로 대충 만든 표본으로는 안 잡힌다.
2. **분모 0** — `None`이 아니라 `0.0`을 돌려주면 "셀 이슈가 없다"가 "방치된 이슈가
   없다"로 둔갑해 **빈 저장소가 건강해 보인다.**
3. **메인테이너 응답 없음** — 중앙값에서 빼되 건수를 드러낸다. 0으로 채우면 응답이
   느릴수록 중앙값이 0에 가까워져 지표가 거꾸로 움직인다.
4. **미분석 건수** — 분모에서 조용히 빼면 분포는 늘 그럴듯해 보인다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.analysis.metrics import (
    Distribution,
    category_distribution,
    compute_metrics,
    response_time,
    select_window,
    sentiment_distribution,
    stale_issues,
    window_start,
)
from app.models import IssueCategory, IssueFacts, IssueSentiment, IssueState

# 고정된 기준 시각. `datetime.now()`에 기대면 경계 테스트가 실행 시점마다 달라진다.
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
WINDOW_DAYS = 180
STALE_DAYS = 90

# 2026-02-25 12:00 UTC. 이 시각에 생성된 이슈는 **포함**이다.
WINDOW_START = NOW - timedelta(days=WINDOW_DAYS)
STALE_CUTOFF = NOW - timedelta(days=STALE_DAYS)

_next_id = 0


def make_facts(**overrides: Any) -> IssueFacts:
    """기본값이 채워진 `IssueFacts`를 만든다.

    `issue_id`는 자동으로 붙인다. 테스트가 보려는 축만 덮어쓰고 나머지는 늘
    유효한 값으로 두어, "다른 필드 때문에 결과가 달라진 것"과 헷갈리지 않게 한다.
    """
    global _next_id
    _next_id += 1

    values = {
        "issue_id": _next_id,
        "created_at": NOW - timedelta(days=10),
        "state": IssueState.CLOSED,
        "first_response_checked": False,
        "responded_at": None,
        "category": None,
        "sentiment": None,
    }
    values.update(overrides)
    return IssueFacts(**values)


def responded_after(delta: timedelta, **overrides: Any) -> IssueFacts:
    """생성 후 `delta`만큼 지나 메인테이너가 응답한 이슈."""
    created_at = overrides.pop("created_at", NOW - timedelta(days=10))
    return make_facts(
        created_at=created_at,
        first_response_checked=True,
        responded_at=created_at + delta,
        **overrides,
    )


def analyzed(category: IssueCategory, sentiment: IssueSentiment, **overrides: Any) -> IssueFacts:
    """LLM 분석이 끝난 이슈."""
    return make_facts(category=category, sentiment=sentiment, **overrides)


# ---------------------------------------------------------------------------
# 6개월 경계값
# ---------------------------------------------------------------------------


def test_window_start_is_now_minus_window_days():
    assert window_start(NOW, WINDOW_DAYS) == datetime(2026, 2, 25, 12, 0, tzinfo=UTC)


def test_issue_created_exactly_on_the_boundary_is_included():
    """경계는 포함이다. `docs/findings.md`의 `created:>=` 쿼리와 같은 규칙이다."""
    on_boundary = make_facts(created_at=WINDOW_START)

    assert select_window([on_boundary], now=NOW, window_days=WINDOW_DAYS) == [on_boundary]


def test_issue_created_one_microsecond_before_the_boundary_is_excluded():
    """경계 바로 앞은 제외된다. 이 한 칸이 포함/배제 규칙을 고정한다."""
    just_outside = make_facts(created_at=WINDOW_START - timedelta(microseconds=1))

    assert select_window([just_outside], now=NOW, window_days=WINDOW_DAYS) == []


def test_window_keeps_input_order_and_drops_only_old_issues():
    inside_new = make_facts(created_at=NOW - timedelta(days=1))
    outside = make_facts(created_at=NOW - timedelta(days=400))
    inside_old = make_facts(created_at=WINDOW_START + timedelta(seconds=1))

    selected = select_window([inside_new, outside, inside_old], now=NOW, window_days=WINDOW_DAYS)

    assert selected == [inside_new, inside_old]


def test_window_ignores_updated_at_because_facts_do_not_carry_it():
    """집계 축은 `created_at`이다.

    수집은 `since`(=`updated_at`)로 넓게 받으므로 2024년에 열린 이슈가 섞여
    들어온다(`docs/findings.md` 함정 2). 그 이슈는 최근에 갱신됐더라도 기간
    밖이다 -- `IssueFacts`에 `updated_at`이 아예 없다는 것이 그 규칙을 구조로
    못 박은 것이다.
    """
    old_but_recently_updated = make_facts(created_at=datetime(2024, 12, 5, tzinfo=UTC))

    assert select_window([old_but_recently_updated], now=NOW, window_days=WINDOW_DAYS) == []
    assert not hasattr(old_but_recently_updated, "updated_at")


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="naive datetime"):
        select_window([], now=NOW.replace(tzinfo=None), window_days=WINDOW_DAYS)


def test_naive_created_at_is_rejected():
    naive = IssueFacts(
        issue_id=1,
        created_at=datetime(2026, 8, 1, 12, 0),
        state=IssueState.OPEN,
    )

    with pytest.raises(ValueError, match="naive datetime"):
        select_window([naive], now=NOW, window_days=WINDOW_DAYS)


def test_non_positive_window_days_is_rejected():
    with pytest.raises(ValueError, match="window_days"):
        select_window([], now=NOW, window_days=0)


# ---------------------------------------------------------------------------
# 방치된 이슈 비율
# ---------------------------------------------------------------------------


def test_stale_needs_open_and_old_and_no_maintainer_response():
    stale = make_facts(
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=120),
        first_response_checked=True,
    )
    answered = responded_after(
        timedelta(hours=2),
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=120),
    )
    too_new = make_facts(
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=10),
        first_response_checked=True,
    )
    closed = make_facts(
        state=IssueState.CLOSED,
        created_at=NOW - timedelta(days=120),
        first_response_checked=True,
    )

    metric = stale_issues([stale, answered, too_new, closed], now=NOW, stale_days=STALE_DAYS)

    # closed는 분모에도 들어가지 않는다. 분모는 "기간 안의 open 이슈 전체"다.
    assert metric.open_total == 3
    assert metric.stale_count == 1
    assert metric.ratio == pytest.approx(1 / 3)


def test_stale_boundary_is_inclusive_at_exactly_stale_days():
    """`now - created_at >= STALE_DAYS`. 정확히 기준일에 걸린 이슈는 방치다."""
    on_boundary = make_facts(
        state=IssueState.OPEN,
        created_at=STALE_CUTOFF,
        first_response_checked=True,
    )
    one_microsecond_newer = make_facts(
        state=IssueState.OPEN,
        created_at=STALE_CUTOFF + timedelta(microseconds=1),
        first_response_checked=True,
    )

    metric = stale_issues([on_boundary, one_microsecond_newer], now=NOW, stale_days=STALE_DAYS)

    assert metric.open_total == 2
    assert metric.stale_count == 1


def test_unchecked_issues_are_not_counted_as_stale_but_are_exposed():
    """미조사를 "응답 없음"으로 치지 않는다.

    치면 수집이 덜 끝났을 뿐인데 방치 비율이 부풀고, 조사가 진행될수록 숫자가
    거꾸로 내려간다. 대신 판정할 수 없었던 건수를 드러낸다.
    """
    unchecked = make_facts(
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=120),
        first_response_checked=False,
    )
    confirmed_stale = make_facts(
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=120),
        first_response_checked=True,
    )

    metric = stale_issues([unchecked, confirmed_stale], now=NOW, stale_days=STALE_DAYS)

    assert metric.open_total == 2
    assert metric.stale_count == 1
    assert metric.unchecked_count == 1
    assert metric.ratio == pytest.approx(0.5)


def test_stale_ratio_is_none_when_there_are_no_open_issues():
    """분모 0. `0.0`이 아니라 `None`이다.

    `0.0`으로 돌려주면 "방치된 이슈가 없다"로 읽혀 **이슈가 하나도 없는 저장소가
    가장 건강해 보인다.**
    """
    only_closed = [make_facts(state=IssueState.CLOSED) for _ in range(3)]

    metric = stale_issues(only_closed, now=NOW, stale_days=STALE_DAYS)

    assert metric.open_total == 0
    assert metric.stale_count == 0
    assert metric.ratio is None


def test_stale_ratio_is_none_for_empty_input():
    metric = stale_issues([], now=NOW, stale_days=STALE_DAYS)

    assert metric.ratio is None
    assert metric.unchecked_count == 0


def test_non_positive_stale_days_is_rejected():
    with pytest.raises(ValueError, match="stale_days"):
        stale_issues([], now=NOW, stale_days=0)


# ---------------------------------------------------------------------------
# 분포 (버그 vs 기능요청 · 감정 톤)
# ---------------------------------------------------------------------------


def test_category_distribution_counts_every_member_even_when_zero():
    facts = [
        analyzed(IssueCategory.BUG, IssueSentiment.NEUTRAL),
        analyzed(IssueCategory.BUG, IssueSentiment.FRUSTRATED),
        analyzed(IssueCategory.FEATURE_REQUEST, IssueSentiment.POSITIVE),
    ]

    distribution = category_distribution(facts)

    # 0건인 값도 키로 남는다. 호출하는 쪽이 0을 채우게 두면 "0건"과 "그런 분류가
    # 사라짐"이 구분되지 않는다.
    assert distribution.counts == {
        IssueCategory.BUG: 2,
        IssueCategory.FEATURE_REQUEST: 1,
        IssueCategory.QUESTION: 0,
        IssueCategory.OTHER: 0,
    }
    assert distribution.ratios[IssueCategory.BUG] == pytest.approx(2 / 3)
    assert distribution.ratios[IssueCategory.QUESTION] == 0.0


def test_unanalyzed_issues_are_excluded_from_the_denominator_but_reported():
    """미분석을 분모에서 조용히 빼지 않는다. 건수를 드러낸다."""
    facts = [
        analyzed(IssueCategory.BUG, IssueSentiment.NEUTRAL),
        analyzed(IssueCategory.FEATURE_REQUEST, IssueSentiment.NEUTRAL),
        make_facts(),  # 미분석
        make_facts(),  # 미분석
    ]

    distribution = category_distribution(facts)

    assert distribution.analyzed_count == 2
    assert distribution.unanalyzed_count == 2
    assert distribution.total == 4
    # 분모는 분석 완료 건수(2)다. 대상 전체(4)로 나누면 비율이 조용히 절반이 된다.
    assert distribution.ratios[IssueCategory.BUG] == pytest.approx(0.5)


def test_distribution_ratios_are_none_when_nothing_was_analyzed():
    """분모 0. 미분석만 있으면 비율을 낼 수 없다고 말한다."""
    distribution = category_distribution([make_facts(), make_facts()])

    assert distribution.analyzed_count == 0
    assert distribution.unanalyzed_count == 2
    assert distribution.ratios is None
    # 건수는 그대로 남는다. 비율만 못 내는 것이지 아무것도 모르는 것이 아니다.
    assert distribution.counts[IssueCategory.BUG] == 0


def test_distribution_ratios_are_none_for_empty_input():
    distribution = sentiment_distribution([])

    assert distribution.total == 0
    assert distribution.ratios is None


def test_sentiment_distribution_uses_the_sentiment_axis():
    facts = [
        analyzed(IssueCategory.BUG, IssueSentiment.FRUSTRATED),
        analyzed(IssueCategory.BUG, IssueSentiment.FRUSTRATED),
        analyzed(IssueCategory.BUG, IssueSentiment.NEUTRAL),
    ]

    distribution = sentiment_distribution(facts)

    assert distribution.counts == {
        IssueSentiment.POSITIVE: 0,
        IssueSentiment.NEUTRAL: 1,
        IssueSentiment.FRUSTRATED: 2,
    }


def test_distribution_rejects_counts_that_do_not_add_up():
    with pytest.raises(ValueError, match="분포 합"):
        Distribution(counts={IssueCategory.BUG: 1}, analyzed_count=2, unanalyzed_count=0)


# ---------------------------------------------------------------------------
# 메인테이너 응답 속도
# ---------------------------------------------------------------------------


def test_median_of_odd_number_of_responses():
    facts = [
        responded_after(timedelta(hours=1)),
        responded_after(timedelta(hours=5)),
        responded_after(timedelta(hours=3)),
    ]

    metric = response_time(facts)

    assert metric.median == timedelta(hours=3)
    assert metric.responded_count == 3
    assert metric.median_seconds == pytest.approx(3 * 3600)


def test_median_of_even_number_of_responses_averages_the_middle_two():
    facts = [
        responded_after(timedelta(hours=1)),
        responded_after(timedelta(hours=2)),
        responded_after(timedelta(hours=4)),
        responded_after(timedelta(hours=9)),
    ]

    metric = response_time(facts)

    assert metric.median == timedelta(hours=3)


def test_issues_without_a_maintainer_response_are_excluded_and_counted():
    """응답 없음을 0으로 채우지 않는다.

    채웠다면 아래 중앙값은 2시간이 아니라 0에 가까워진다. 응답이 느린 저장소일수록
    지표가 좋아지는 셈이라, 이 지표에서 가장 위험한 실수다.
    """
    facts = [
        responded_after(timedelta(hours=1)),
        responded_after(timedelta(hours=3)),
        *[make_facts(first_response_checked=True) for _ in range(5)],
    ]

    metric = response_time(facts)

    assert metric.median == timedelta(hours=2)
    assert metric.responded_count == 2
    assert metric.no_response_count == 5


def test_unchecked_issues_are_counted_separately_from_no_response():
    """ "아직 조사 안 함"과 "조사했는데 응답 없음"은 다른 값이다."""
    facts = [
        responded_after(timedelta(hours=2)),
        make_facts(first_response_checked=True),
        make_facts(first_response_checked=False),
        make_facts(first_response_checked=False),
    ]

    metric = response_time(facts)

    assert metric.responded_count == 1
    assert metric.no_response_count == 1
    assert metric.unchecked_count == 2


def test_median_is_none_when_no_issue_has_a_response():
    """분모 0. 중앙값을 낼 대상이 없으면 `None`이다 (`timedelta(0)`이 아니다)."""
    facts = [
        make_facts(first_response_checked=True),
        make_facts(first_response_checked=False),
    ]

    metric = response_time(facts)

    assert metric.median is None
    assert metric.median_seconds is None
    assert metric.no_response_count == 1
    assert metric.unchecked_count == 1


def test_median_is_none_for_empty_input():
    metric = response_time([])

    assert metric.median is None
    assert metric.responded_count == 0


# ---------------------------------------------------------------------------
# compute_metrics — 네 지표가 같은 모집단을 본다
# ---------------------------------------------------------------------------


def test_compute_metrics_cuts_the_window_once_for_all_four_metrics():
    inside = analyzed(
        IssueCategory.BUG,
        IssueSentiment.FRUSTRATED,
        state=IssueState.OPEN,
        created_at=NOW - timedelta(days=120),
        first_response_checked=True,
    )
    outside = analyzed(
        IssueCategory.FEATURE_REQUEST,
        IssueSentiment.POSITIVE,
        state=IssueState.OPEN,
        created_at=WINDOW_START - timedelta(seconds=1),
        first_response_checked=True,
    )

    report = compute_metrics(
        [inside, outside], now=NOW, window_days=WINDOW_DAYS, stale_days=STALE_DAYS
    )

    assert report.issue_count == 1
    assert report.stale_issues.open_total == 1
    assert report.stale_issues.stale_count == 1
    assert report.categories.analyzed_count == 1
    assert report.categories.counts[IssueCategory.FEATURE_REQUEST] == 0
    assert report.sentiments.counts[IssueSentiment.FRUSTRATED] == 1
    assert report.response_time.no_response_count == 1


def test_compute_metrics_records_the_conditions_it_used():
    report = compute_metrics([], now=NOW, window_days=WINDOW_DAYS, stale_days=STALE_DAYS)

    assert report.generated_at == NOW
    assert report.window_days == WINDOW_DAYS
    assert report.stale_issues.stale_days == STALE_DAYS


def test_compute_metrics_on_empty_input_yields_none_ratios_not_zeros():
    """분모 0이 네 지표에 동시에 걸리는 경우. 어디서도 터지지 않고 `None`이 나온다."""
    report = compute_metrics([], now=NOW, window_days=WINDOW_DAYS, stale_days=STALE_DAYS)

    assert report.issue_count == 0
    assert report.stale_issues.ratio is None
    assert report.categories.ratios is None
    assert report.sentiments.ratios is None
    assert report.response_time.median is None


# ---------------------------------------------------------------------------
# IssueFacts — 뭉개진 상태를 값 만드는 시점에 막는다
# ---------------------------------------------------------------------------


def test_facts_reject_response_time_without_a_completed_check():
    with pytest.raises(ValueError, match="조사하지 않은"):
        IssueFacts(
            issue_id=1,
            created_at=NOW,
            state=IssueState.OPEN,
            first_response_checked=False,
            responded_at=NOW,
        )


def test_facts_reject_a_response_that_predates_the_issue():
    with pytest.raises(ValueError, match="이릅니다"):
        IssueFacts(
            issue_id=1,
            created_at=NOW,
            state=IssueState.OPEN,
            first_response_checked=True,
            responded_at=NOW - timedelta(seconds=1),
        )


def test_facts_reject_half_filled_analysis():
    with pytest.raises(ValueError, match="함께"):
        IssueFacts(
            issue_id=1,
            created_at=NOW,
            state=IssueState.OPEN,
            category=IssueCategory.BUG,
        )


def test_facts_expose_analyzed_and_responded_as_derived_flags():
    unchecked = make_facts()
    checked_without_response = make_facts(first_response_checked=True)
    with_response = responded_after(timedelta(hours=1))

    assert unchecked.responded is False
    assert checked_without_response.responded is False
    assert with_response.responded is True
    assert with_response.analyzed is False
    assert analyzed(IssueCategory.BUG, IssueSentiment.NEUTRAL).analyzed is True
