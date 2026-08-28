"""지표 응답 모델.

## `null`을 쓰는 자리가 두 가지다

이 응답에는 `null`이 두 가지 뜻으로 나온다. 헷갈리지 않게 자리를 나눠 뒀다.

| 자리 | 뜻 |
|---|---|
| `status="pending"`일 때의 지표 4개 | **아직 계산할 수 없다** (수집 전) |
| `ratio` · `median_seconds` | **비율을 낼 수 없다** (분모가 0) |

앞의 것은 "나중에 다시 물어보면 값이 생긴다"이고, 뒤의 것은 "지금 데이터로는 낼 수
없다"이다. 둘 다 0으로 채우면 **빈 저장소가 가장 건강해 보인다.**

## 건수를 숨기지 않는다

`unanalyzed_count`(미분석) · `no_response_count`(메인테이너 응답 없음) ·
`unchecked_count`(첫 응답 미조사)는 응답에 그대로 실린다. 분모에서 조용히 빠지면
비율은 늘 그럴듯해 보이고, 무엇이 빠졌는지는 어디에도 남지 않는다
(`CLAUDE.md` 2절).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from app.analysis.metrics import (
    Distribution,
    MetricsReport,
    ResponseTimeMetric,
    StaleIssueMetric,
)


class MetricsStatus(StrEnum):
    """지표를 낼 수 있는 상태인지.

    **"저장소가 없다"는 여기에 없다.** 그건 404로 답한다 -- 존재하지 않는 것과
    아직 준비되지 않은 것을 같은 200 응답에 담으면 프론트가 구분할 수 없고,
    오타로 친 저장소 이름이 "수집 중"으로 보인다.
    """

    READY = "ready"
    PENDING = "pending"


class StaleIssuesOut(BaseModel):
    """방치된 이슈 비율.

    Attributes:
        ratio: 방치 비율. **open 이슈가 없으면 `null`**(`0.0`이 아니다).
        stale_count: 분자. 오래됐고 메인테이너 응답이 없다고 확인된 수.
        open_total: 분모. 기간 안에 생성된 open 이슈 수.
        unchecked_count: 나이 조건은 맞지만 첫 응답을 아직 조사하지 않아 판정할
            수 없는 수.
        stale_days: 방치 판정에 쓴 기준(일).
    """

    model_config = ConfigDict(frozen=True)

    ratio: float | None
    stale_count: int
    open_total: int
    unchecked_count: int
    stale_days: int

    @classmethod
    def from_metric(cls, metric: StaleIssueMetric) -> Self:
        """계산 결과를 응답 모델로 옮긴다.

        Args:
            metric: `app.analysis.metrics`가 낸 값.

        Returns:
            응답 모델.
        """
        return cls(
            ratio=metric.ratio,
            stale_count=metric.stale_count,
            open_total=metric.open_total,
            unchecked_count=metric.unchecked_count,
            stale_days=metric.stale_days,
        )


class DistributionOut(BaseModel):
    """LLM 판정 값의 분포.

    `counts`의 키는 enum **값**(`"bug"`, `"feature_request"` ...)이다. 멤버 이름이
    아니라 값을 쓰는 이유는 DB CHECK 제약과 프롬프트 선택지가 전부 같은 값을 쓰기
    때문이다(`app/models/enums.py`). 표현마다 다른 문자열을 쓰면 대조할 때마다
    변환표가 필요해진다.

    Attributes:
        counts: 값별 건수. **0건인 값도 키로 들어 있다.**
        ratios: 값별 비율. **분석 완료가 0건이면 `null`**(전부 `0.0`이 아니다).
        analyzed_count: 분석 완료 건수. 비율의 분모다.
        unanalyzed_count: 미분석 건수. 분모에서 빠졌다는 사실을 드러낸다.
        total: 대상 이슈 수(분석 완료 + 미분석).
    """

    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    ratios: dict[str, float] | None
    analyzed_count: int
    unanalyzed_count: int
    total: int

    @classmethod
    def from_metric(cls, distribution: Distribution[Any]) -> Self:
        """계산 결과를 응답 모델로 옮긴다.

        Args:
            distribution: `app.analysis.metrics`가 낸 분포.

        Returns:
            응답 모델.
        """
        ratios = distribution.ratios
        return cls(
            counts={member.value: count for member, count in distribution.counts.items()},
            ratios=(
                None
                if ratios is None
                else {member.value: ratio for member, ratio in ratios.items()}
            ),
            analyzed_count=distribution.analyzed_count,
            unanalyzed_count=distribution.unanalyzed_count,
            total=distribution.total,
        )


class ResponseTimeOut(BaseModel):
    """메인테이너 응답 속도.

    초 단위로 내보낸다. 시간 단위로 반올림해서 보내면 프론트가 다시 나눌 때
    자릿수를 잃고, "3시간"과 "3.4시간"이 같은 값이 된다.

    Attributes:
        median_seconds: 첫 응답까지 걸린 시간의 중앙값(초).
            **응답이 하나도 없으면 `null`**(`0`이 아니다).
        responded_count: 중앙값에 실제로 들어간 건수.
        no_response_count: 조사했지만 메인테이너 응답이 없었던 건수.
            결측이 아니라 의미 있는 값이다.
        unchecked_count: 첫 응답을 아직 조사하지 않은 건수.
    """

    model_config = ConfigDict(frozen=True)

    median_seconds: float | None
    responded_count: int
    no_response_count: int
    unchecked_count: int

    @classmethod
    def from_metric(cls, metric: ResponseTimeMetric) -> Self:
        """계산 결과를 응답 모델로 옮긴다.

        Args:
            metric: `app.analysis.metrics`가 낸 값.

        Returns:
            응답 모델.
        """
        return cls(
            median_seconds=metric.median_seconds,
            responded_count=metric.responded_count,
            no_response_count=metric.no_response_count,
            unchecked_count=metric.unchecked_count,
        )


class MetricsResponse(BaseModel):
    """지표 엔드포인트의 응답.

    `status`가 `pending`이면 지표 4개가 전부 `null`이다. 0으로 채운 지표를 보내면
    프론트가 **수집이 끝난 빈 저장소와 구분할 수 없고**, 그래프는 정상적으로
    그려진다.

    Attributes:
        repo: 대상 저장소(`"owner/name"`).
        status: 지표를 낼 수 있는 상태인지.
        generated_at: 계산 기준 시각.
        window_days: 집계 기간(일).
        issue_count: 기간 안에 생성된 이슈 수. `pending`이면 `null`.
        stale_issues: 방치된 이슈 비율. `pending`이면 `null`.
        categories: 버그 vs 기능요청 비율. `pending`이면 `null`.
        sentiments: 감정 톤 분포. `pending`이면 `null`.
        response_time: 메인테이너 응답 속도. `pending`이면 `null`.
    """

    model_config = ConfigDict(frozen=True)

    repo: str
    status: MetricsStatus
    generated_at: datetime
    window_days: int
    issue_count: int | None = None
    stale_issues: StaleIssuesOut | None = None
    categories: DistributionOut | None = None
    sentiments: DistributionOut | None = None
    response_time: ResponseTimeOut | None = None

    @classmethod
    def pending(cls, repo: str, *, generated_at: datetime, window_days: int) -> Self:
        """아직 수집하지 않은 저장소의 응답을 만든다.

        Args:
            repo: 대상 저장소.
            generated_at: 응답을 만든 시각.
            window_days: 설정된 집계 기간(일).

        Returns:
            지표가 전부 `null`인 응답.
        """
        return cls(
            repo=repo,
            status=MetricsStatus.PENDING,
            generated_at=generated_at,
            window_days=window_days,
        )

    @classmethod
    def from_report(cls, repo: str, report: MetricsReport) -> Self:
        """계산이 끝난 지표를 응답으로 옮긴다.

        Args:
            repo: 대상 저장소.
            report: `compute_metrics()`의 결과.

        Returns:
            지표 4개가 채워진 응답.
        """
        return cls(
            repo=repo,
            status=MetricsStatus.READY,
            generated_at=report.generated_at,
            window_days=report.window_days,
            issue_count=report.issue_count,
            stale_issues=StaleIssuesOut.from_metric(report.stale_issues),
            categories=DistributionOut.from_metric(report.categories),
            sentiments=DistributionOut.from_metric(report.sentiments),
            response_time=ResponseTimeOut.from_metric(report.response_time),
        )
