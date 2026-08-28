"""지표 4개 계산 — 순수 함수. DB도 시계도 모른다.

| 지표 | 정의 |
|---|---|
| 방치된 이슈 비율 | open AND `now - created_at >= STALE_DAYS` AND 응답 없음 **/** open 전체 |
| 버그 vs 기능요청 비율 | LLM `category` 분포 **/** 분석 완료 건수 |
| 메인테이너 응답 속도 | `median(첫 메인테이너 응답 시각 - created_at)` |
| 이슈 감정 톤 분포 | LLM `sentiment` 분포 |

## 집계 축은 `created_at`이다

수집은 `since`(=`updated_at`)로 넓게 받는다. 오래전에 열렸지만 최근에 코멘트가
달린 이슈까지 딸려오기 때문에(`docs/findings.md` 함정 2), **집계 시점에
`created_at`으로 다시 자른다.** 두 시각의 역할이 다르다:

- `updated_at` -> 무엇을 **가져올지** 정하는 축 (증분 수집)
- `created_at` -> 무엇을 **셀지** 정하는 축 (여기)

이 모듈은 `updated_at`을 아예 받지 않는다. 받을 수 있게 두면 언젠가 쓰인다.

## `now`를 주입받는다

`datetime.now()`를 안에서 부르면 "6개월 경계"와 "방치 판정"이 **실행할 때마다
달라지는 값**에 걸린다. 경계 근처의 동작은 그 순간에만 재현되므로 테스트로 고정할
수 없다. 그래서 시각은 호출자가 넘긴다.

naive datetime은 거부한다. aware와 naive를 비교하면 `TypeError`가 나거나, 둘 다
naive면 **시간대만큼 밀린 채로 성립한다**(`app/models/types.py` 참고). 경계에서만
틀리는 종류라 조용히 지나간다.

## 분모가 0이면 비율은 `None`이다

`0.0`이 아니다. "방치된 이슈가 하나도 없다"와 "셀 이슈 자체가 없다"는 전혀 다른
말인데, 둘 다 0으로 만들면 대시보드에 **건강한 저장소로 표시된다.** 비율을 낼 수
없으면 낼 수 없다고 말한다.

## 세지 못한 것을 숨기지 않는다

- **메인테이너 응답 없음**은 중앙값에서 빼되 건수를 드러낸다. 0으로 채우면 응답
  속도가 실제보다 빨라지고, 조용히 버리면 "빠르게 응답하는 저장소"로 보인다.
- **미분석 이슈**를 분모에서 조용히 빼지 않는다. 분포는 분석 완료 건수로 나누되,
  미분석 건수를 함께 내보낸다.
- **첫 응답을 아직 조사하지 않은 이슈**도 마찬가지다. 미조사를 "응답 없음"으로
  치면 방치 비율이 부풀고, 그냥 빼면 줄어든다. 어느 쪽도 고르지 않고 건수를
  따로 드러낸다.

## 종합 헬스 스코어를 만들지 않는다

지표 4개를 하나의 점수로 합치려면 가중치를 정해야 하는데, 그 가중치에는 근거가
없다. 근거 없는 숫자는 근거 없다는 사실까지 숨긴다. 4개를 그대로 보여준다
(`CLAUDE.md` 2절).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum as PyEnum
from statistics import median

from app.models import IssueCategory, IssueFacts, IssueSentiment, IssueState


def _require_aware(value: datetime, *, label: str) -> datetime:
    """timezone-aware datetime인지 확인한다.

    Args:
        value: 확인할 시각.
        label: 오류 메시지에 넣을 이름.

    Returns:
        받은 값 그대로.

    Raises:
        ValueError: timezone 정보가 없는 경우.
    """
    if value.tzinfo is None:
        raise ValueError(
            f"{label}에 naive datetime을 쓸 수 없습니다. UTC로 aware하게 만들어 넘기세요."
        )
    return value


def window_start(now: datetime, window_days: int) -> datetime:
    """집계 기간의 시작 시각을 구한다.

    Args:
        now: 기준 시각. timezone-aware여야 한다.
        window_days: 집계 기간(일).

    Returns:
        `now - window_days`.

    Raises:
        ValueError: `now`가 naive이거나 `window_days`가 양수가 아닌 경우.
    """
    _require_aware(now, label="now")
    if window_days <= 0:
        raise ValueError(f"window_days는 양수여야 합니다: {window_days}")
    return now - timedelta(days=window_days)


def select_window(
    facts: Iterable[IssueFacts], *, now: datetime, window_days: int
) -> list[IssueFacts]:
    """집계 기간 안에 **생성된** 이슈만 고른다.

    경계는 **포함**이다(`created_at >= now - window_days`). `docs/findings.md`가
    쓴 Search 쿼리(`created:>=...`)와 같은 규칙이라, 실측 건수와 이 코드의 결과를
    직접 비교할 수 있다. 경계를 한쪽으로만 정해두지 않으면 "489건"이 맞는지
    틀리는지 확인할 방법이 없어진다.

    Args:
        facts: 자를 대상. 순서는 상관없다.
        now: 기준 시각.
        window_days: 집계 기간(일).

    Returns:
        기간 안에 생성된 이슈. 입력 순서를 유지한다.

    Raises:
        ValueError: `now`나 `created_at`이 naive이거나 `window_days`가 양수가
            아닌 경우.
    """
    start = window_start(now, window_days)
    selected = []
    for fact in facts:
        _require_aware(fact.created_at, label=f"issue_id={fact.issue_id}의 created_at")
        if fact.created_at >= start:
            selected.append(fact)
    return selected


@dataclass(frozen=True)
class StaleIssueMetric:
    """방치된 이슈 비율.

    분자와 분모의 대상이 다르다는 점에 주의한다. 분모는 **기간 안의 open 이슈
    전체**이고, 분자는 그중 오래됐고 메인테이너 응답이 없는 것이다.

    Attributes:
        open_total: 분모. 기간 안에 생성된 open 이슈 수.
        stale_count: 분자. 오래됐고 메인테이너 응답이 없다고 **확인된** 수.
        unchecked_count: 나이 조건은 만족하지만 첫 응답을 아직 조사하지 않아
            판정할 수 없는 수. 조사가 끝나면 이만큼까지 `stale_count`가 늘 수 있다.
        stale_days: 방치 판정에 쓴 기준(일).
    """

    open_total: int
    stale_count: int
    unchecked_count: int
    stale_days: int

    @property
    def ratio(self) -> float | None:
        """방치 비율. **셀 open 이슈가 없으면 `None`이다** (`0.0`이 아니다)."""
        if self.open_total == 0:
            return None
        return self.stale_count / self.open_total


def stale_issues(
    facts: Iterable[IssueFacts], *, now: datetime, stale_days: int
) -> StaleIssueMetric:
    """방치된 이슈 비율을 센다.

    "방치"는 세 조건을 모두 만족하는 상태다 -- 아직 열려 있고, 생성된 지
    `stale_days` 이상 지났고, 메인테이너 응답이 없다.

    **첫 응답을 조사하지 않은 이슈는 분자에 넣지 않는다.** 미조사를 "응답 없음"으로
    치면 수집이 덜 끝났을 뿐인데 방치 비율이 부풀고, 수집이 진행될수록 숫자가
    거꾸로 내려간다. 대신 그 건수를 `unchecked_count`로 드러낸다.

    Args:
        facts: **이미 기간으로 자른** 이슈들.
        now: 기준 시각.
        stale_days: 방치 판정 기준(일).

    Returns:
        분자·분모와, 판정할 수 없었던 건수.

    Raises:
        ValueError: `now`가 naive이거나 `stale_days`가 양수가 아닌 경우.
    """
    _require_aware(now, label="now")
    if stale_days <= 0:
        raise ValueError(f"stale_days는 양수여야 합니다: {stale_days}")

    cutoff = now - timedelta(days=stale_days)
    open_total = 0
    stale_count = 0
    unchecked_count = 0

    for fact in facts:
        if fact.state is not IssueState.OPEN:
            continue
        open_total += 1
        if fact.created_at > cutoff:
            continue
        if not fact.first_response_checked:
            unchecked_count += 1
        elif not fact.responded:
            stale_count += 1

    return StaleIssueMetric(
        open_total=open_total,
        stale_count=stale_count,
        unchecked_count=unchecked_count,
        stale_days=stale_days,
    )


@dataclass(frozen=True)
class Distribution[MemberT: PyEnum]:
    """LLM 판정 값의 분포.

    `counts`는 **enum의 모든 멤버를 담는다.** 0건인 값도 키가 있다. 없는 키를
    호출하는 쪽이 `0`으로 채우게 두면, "0건"과 "그런 분류가 없어짐"이 구분되지
    않는다.

    Attributes:
        counts: 값별 건수. 분석에 성공한 이슈만 센다.
        analyzed_count: 분석 완료 건수. 비율의 분모다.
        unanalyzed_count: 미분석 건수. **분모에서 조용히 빠진 것이 아니라 드러난다.**
    """

    counts: Mapping[MemberT, int]
    analyzed_count: int
    unanalyzed_count: int

    def __post_init__(self) -> None:
        """건수 합이 분모와 맞는지 확인한다.

        어긋나면 집계가 틀린 것이고, 그 어긋남은 비율에서 아주 작은 차이로만
        나타나 눈에 띄지 않는다.

        Raises:
            ValueError: `counts`의 합이 `analyzed_count`와 다른 경우.
        """
        total = sum(self.counts.values())
        if total != self.analyzed_count:
            raise ValueError(f"분포 합({total})과 분석 완료 건수({self.analyzed_count})가 다릅니다")

    @property
    def total(self) -> int:
        """대상 이슈 수. 분석 완료와 미분석을 합친 값이다."""
        return self.analyzed_count + self.unanalyzed_count

    @property
    def ratios(self) -> dict[MemberT, float] | None:
        """값별 비율. **분석 완료 건수가 0이면 `None`이다** (`0.0`이 아니다)."""
        if self.analyzed_count == 0:
            return None
        return {member: count / self.analyzed_count for member, count in self.counts.items()}


def _distribution[MemberT: PyEnum](
    facts: Iterable[IssueFacts],
    *,
    members: Sequence[MemberT],
    pick: Callable[[IssueFacts], MemberT | None],
) -> Distribution[MemberT]:
    """판정 값 하나를 골라 분포를 만든다.

    `category`와 `sentiment`는 세는 방법이 같고 고르는 필드만 다르다. 두 벌로
    적어두면 한쪽만 고치는 일이 생긴다.

    Args:
        facts: **이미 기간으로 자른** 이슈들.
        members: 셀 값 전체. 0건인 값도 키로 남긴다.
        pick: 이슈에서 셀 값을 꺼내는 함수. 미분석이면 `None`을 돌려준다.

    Returns:
        값별 건수와 미분석 건수.
    """
    counts: dict[MemberT, int] = dict.fromkeys(members, 0)
    unanalyzed_count = 0

    for fact in facts:
        member = pick(fact)
        if member is None:
            unanalyzed_count += 1
            continue
        counts[member] += 1

    return Distribution(
        counts=counts,
        analyzed_count=sum(counts.values()),
        unanalyzed_count=unanalyzed_count,
    )


def category_distribution(facts: Iterable[IssueFacts]) -> Distribution[IssueCategory]:
    """버그 vs 기능요청 비율(= `category` 분포)을 센다.

    Args:
        facts: **이미 기간으로 자른** 이슈들.

    Returns:
        분류별 건수와 미분석 건수.
    """
    return _distribution(facts, members=list(IssueCategory), pick=lambda fact: fact.category)


def sentiment_distribution(facts: Iterable[IssueFacts]) -> Distribution[IssueSentiment]:
    """감정 톤 분포를 센다.

    Args:
        facts: **이미 기간으로 자른** 이슈들.

    Returns:
        감정 톤별 건수와 미분석 건수.
    """
    return _distribution(facts, members=list(IssueSentiment), pick=lambda fact: fact.sentiment)


@dataclass(frozen=True)
class ResponseTimeMetric:
    """메인테이너 응답 속도.

    평균이 아니라 중앙값이다. 응답 시간은 몇 달 걸린 이슈 몇 건이 평균을 통째로
    끌고 가는 분포라, 평균은 "보통 얼마나 걸리는가"에 답하지 못한다.

    Attributes:
        median: 응답이 있었던 이슈들의 `응답 시각 - 생성 시각` 중앙값.
            응답이 하나도 없으면 `None`.
        responded_count: 중앙값에 실제로 들어간 건수.
        no_response_count: 조사했지만 메인테이너 응답이 없었던 건수.
            **결측이 아니라 의미 있는 값이다.** 0으로 채우지 않고 따로 센다.
        unchecked_count: 첫 응답을 아직 조사하지 않은 건수.
    """

    median: timedelta | None
    responded_count: int
    no_response_count: int
    unchecked_count: int

    @property
    def median_seconds(self) -> float | None:
        """중앙값을 초로 환산한 값. 응답이 없으면 `None`."""
        if self.median is None:
            return None
        return self.median.total_seconds()


def response_time(facts: Iterable[IssueFacts]) -> ResponseTimeMetric:
    """메인테이너 첫 응답까지 걸린 시간의 중앙값을 구한다.

    **응답이 없었던 이슈를 0으로 채우지 않는다.** 채우면 응답이 느린 저장소일수록
    중앙값이 0에 가까워져 지표가 거꾸로 움직인다. 중앙값에서 빼되 건수를 따로
    돌려준다(`docs/findings.md` 함정 3).

    Args:
        facts: **이미 기간으로 자른** 이슈들.

    Returns:
        중앙값과, 중앙값에 넣지 않은 건수들.
    """
    durations: list[timedelta] = []
    no_response_count = 0
    unchecked_count = 0

    for fact in facts:
        if not fact.first_response_checked:
            unchecked_count += 1
            continue
        if fact.responded_at is None:
            no_response_count += 1
            continue
        durations.append(fact.responded_at - fact.created_at)

    return ResponseTimeMetric(
        # median()은 빈 목록에 StatisticsError를 낸다. 분모 0을 예외가 아니라
        # "낼 수 없음"으로 다루는 것이 이 모듈의 규칙이라 여기서 갈라놓는다.
        median=median(durations) if durations else None,
        responded_count=len(durations),
        no_response_count=no_response_count,
        unchecked_count=unchecked_count,
    )


@dataclass(frozen=True)
class MetricsReport:
    """지표 4개와, 그 숫자가 어떤 조건에서 나왔는지.

    조건을 함께 담는 이유는 **같은 저장소라도 기간과 기준이 다르면 다른 숫자가
    나오기 때문**이다. `WINDOW_DAYS`를 바꾼 뒤 어제 스크린샷과 비교하려면 값
    옆에 조건이 붙어 있어야 한다.

    Attributes:
        generated_at: 계산 기준 시각.
        window_days: 집계 기간(일).
        issue_count: 기간 안에 생성된 이슈 수. 분포의 분모가 아니라 대상 전체다.
        stale_issues: 방치된 이슈 비율.
        categories: 버그 vs 기능요청 비율.
        sentiments: 감정 톤 분포.
        response_time: 메인테이너 응답 속도.
    """

    generated_at: datetime
    window_days: int
    issue_count: int
    stale_issues: StaleIssueMetric
    categories: Distribution[IssueCategory]
    sentiments: Distribution[IssueSentiment]
    response_time: ResponseTimeMetric


def compute_metrics(
    facts: Iterable[IssueFacts],
    *,
    now: datetime,
    window_days: int,
    stale_days: int,
) -> MetricsReport:
    """기간으로 자르고 지표 4개를 한 번에 계산한다.

    **자르는 일을 여기서 한 번만 한다.** 지표마다 각자 자르게 두면 한 곳에서
    경계를 다르게 쓰는 순간 네 숫자가 서로 다른 모집단을 가리키는데, 그 어긋남은
    합계가 맞지 않는 식으로도 드러나지 않는다.

    Args:
        facts: 저장소에서 읽어온 이슈 전체. 기간 밖의 것이 섞여 있어도 된다.
        now: 기준 시각. timezone-aware여야 한다.
        window_days: 집계 기간(일).
        stale_days: 방치 판정 기준(일).

    Returns:
        지표 4개와 계산 조건.

    Raises:
        ValueError: 시각이 naive이거나 기간·기준이 양수가 아닌 경우.
    """
    windowed = select_window(facts, now=now, window_days=window_days)
    return MetricsReport(
        generated_at=now,
        window_days=window_days,
        issue_count=len(windowed),
        stale_issues=stale_issues(windowed, now=now, stale_days=stale_days),
        categories=category_distribution(windowed),
        sentiments=sentiment_distribution(windowed),
        response_time=response_time(windowed),
    )
