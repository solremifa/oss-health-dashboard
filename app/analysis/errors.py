"""분석 계층 예외.

## 무엇을 재시도하고 무엇을 재시도하지 않는가

여기 있는 예외는 전부 **재시도가 소용없는 상태**를 뜻한다. 재시도로 풀리는 것은
예외가 되기 전에 `classify.py` 안에서 이미 다시 시도된다.

- **깨진 JSON / 허용 목록 밖의 값** -> 같은 요청을 다시 보낸다. 상한까지 실패하면
  `ClassificationParseError`.
- **`stop_reason == "refusal"`** -> 재시도하지 않는다. 같은 입력이면 같은 판단이 돌아온다.
- **`stop_reason == "max_tokens"`** -> 재시도하지 않는다. 고칠 곳은 요청이지 횟수가 아니다.
- **API 오류(429 · 5xx · 연결 실패)** -> **여기서 감싸지 않는다.** SDK가 이미 재시도하므로
  다시 감싸면 재시도가 중첩된다.

마지막 항목이 중요하다. API 오류를 이 예외로 바꿔 잡으면 "이슈 하나 실패"로 보이지만
실제로는 토큰이 만료됐거나 서비스가 죽은 것이고, 그대로 두면 489건이 전부 조용히
미분석으로 남는다. 그래서 API 오류는 배치 루프를 뚫고 올라가게 둔다.
"""

from __future__ import annotations


class AnalysisError(Exception):
    """분석 계층의 모든 예외가 상속하는 기반 클래스.

    배치 러너는 이 타입만 잡아 해당 이슈를 건너뛴다. SDK가 올리는 API 오류는
    여기에 속하지 않으므로 루프를 멈추고 위로 올라간다.
    """


class ClassificationRefusedError(AnalysisError):
    """모델이 판정을 거절한 경우(`stop_reason == "refusal"`).

    거절하면 `content`가 비어 있으므로 **`stop_reason`을 먼저 확인해야 한다.**
    본문부터 읽으면 "빈 응답"이나 IndexError로 나타나 원인이 흐려진다.

    재시도하지 않는다. 같은 입력에 같은 판단이 돌아올 뿐이고, 재시도는 비용만 쓴다.

    Attributes:
        issue_id: 대상 이슈.
        category: 거절 분류. API가 주지 않았으면 `None`.
        explanation: 거절 사유 설명. 없으면 `None`.
    """

    def __init__(self, issue_id: int, category: str | None, explanation: str | None) -> None:
        """예외를 만든다.

        Args:
            issue_id: 대상 이슈.
            category: 거절 분류.
            explanation: 거절 사유 설명.
        """
        detail = explanation or "설명 없음"
        super().__init__(
            f"이슈 {issue_id}의 분류를 모델이 거절했습니다 "
            f"(category={category or 'unknown'}): {detail}"
        )
        self.issue_id = issue_id
        self.category = category
        self.explanation = explanation


class ClassificationTruncatedError(AnalysisError):
    """응답이 `max_tokens`에 걸려 잘린 경우.

    잘린 본문은 JSON으로 파싱되지 않는다. 그런데 이걸 파싱 실패로 취급해 재시도하면
    같은 지점에서 또 잘린다 -- **고칠 곳은 횟수가 아니라 요청**이므로 즉시 올린다.

    Attributes:
        issue_id: 대상 이슈.
    """

    def __init__(self, issue_id: int) -> None:
        """예외를 만든다.

        Args:
            issue_id: 대상 이슈.
        """
        super().__init__(
            f"이슈 {issue_id}의 응답이 max_tokens에 걸려 잘렸습니다. "
            "재시도해도 같은 지점에서 잘리므로 요청의 max_tokens를 늘려야 합니다."
        )
        self.issue_id = issue_id


class ClassificationParseError(AnalysisError):
    """재시도 상한까지 유효한 판정을 받지 못한 경우.

    구조화 출력을 쓰면 거의 나지 않지만, 나면 그 이슈는 **미분석으로 남는다.**
    지어낸 값이나 `other`로 채우지 않는다 -- 분포가 조용히 왜곡되고, 왜곡됐다는
    사실이 어디에도 남지 않는다.

    Attributes:
        issue_id: 대상 이슈.
        attempts: 실제로 시도한 횟수.
        last_reason: 마지막 시도의 실패 사유.
    """

    def __init__(self, issue_id: int, attempts: int, last_reason: str) -> None:
        """예외를 만든다.

        Args:
            issue_id: 대상 이슈.
            attempts: 실제로 시도한 횟수.
            last_reason: 마지막 시도의 실패 사유.
        """
        super().__init__(
            f"이슈 {issue_id}의 분류 응답을 {attempts}회 시도 동안 해석하지 못했습니다 "
            f"(마지막 원인={last_reason})"
        )
        self.issue_id = issue_id
        self.attempts = attempts
        self.last_reason = last_reason
