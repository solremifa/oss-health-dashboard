"""수집 계층 예외.

수집은 **부분 실패가 정상인 작업**이라 실패를 조용히 삼키지 않는 것이 중요하다.
여기 있는 예외들은 "무엇이 왜 실패했는지"를 호출자가 로그로 남길 수 있을 만큼의
정보를 들고 올라간다.
"""

from __future__ import annotations

from datetime import datetime


class CollectorError(Exception):
    """수집 계층의 모든 예외가 상속하는 기반 클래스."""


class GitHubAPIError(CollectorError):
    """재시도해도 결과가 달라지지 않는 GitHub API 오류.

    401(토큰 문제), 404(대상 없음), 422(요청이 틀림) 같은 것들이다. 이런 응답을
    재시도하면 같은 답을 다섯 번 더 받고 그만큼 레이트리밋만 태운다.

    Attributes:
        status_code: 응답 상태 코드.
        url: 요청한 URL.
    """

    def __init__(self, status_code: int, url: str, message: str) -> None:
        """예외를 만든다.

        Args:
            status_code: 응답 상태 코드.
            url: 요청한 URL.
            message: 응답 본문에서 뽑아낸 설명.
        """
        super().__init__(f"GitHub API {status_code} for {url}: {message}")
        self.status_code = status_code
        self.url = url


class RateLimitError(CollectorError):
    """레이트리밋 해제까지 기다려야 하는 시간이 허용치를 넘은 경우.

    GitHub의 1차 레이트리밋은 시간 단위로 리셋된다. 리셋까지 40분 남은 상태에서
    요청 함수 안에서 40분을 자버리면 호출자는 멈춘 것인지 도는 것인지 알 수 없다.
    기다릴지 포기할지는 **호출자가 정할 문제**라 예외로 올린다.

    Attributes:
        wait_seconds: 리셋까지 남은 것으로 계산된 시간(초).
        reset_at: 리셋 시각. 헤더에서 알아낼 수 없었으면 `None`.
        url: 요청한 URL.
    """

    def __init__(self, wait_seconds: float, reset_at: datetime | None, url: str) -> None:
        """예외를 만든다.

        Args:
            wait_seconds: 리셋까지 남은 것으로 계산된 시간(초).
            reset_at: 리셋 시각. 알 수 없으면 `None`.
            url: 요청한 URL.
        """
        reset_text = reset_at.isoformat() if reset_at is not None else "unknown"
        super().__init__(
            f"레이트리밋 대기 시간이 허용치를 초과했습니다: {wait_seconds:.0f}초 "
            f"(reset={reset_text}, url={url})"
        )
        self.wait_seconds = wait_seconds
        self.reset_at = reset_at
        self.url = url


class PaginationLimitError(CollectorError):
    """페이지 상한까지 받았는데도 `rel="next"`가 계속 나오는 경우.

    여기서 조용히 멈추면 **일부만 수집한 데이터로 지표를 계산하게 된다.** 방치율도
    응답 속도도 표본이 잘린 채 그럴듯한 숫자로 나오고, 틀렸다는 사실조차 드러나지
    않는다. 그래서 잘라내지 않고 예외를 올린다.

    Attributes:
        max_pages: 허용한 페이지 수.
        url: 페이지네이션을 시작한 URL.
    """

    def __init__(self, max_pages: int, url: str) -> None:
        """예외를 만든다.

        Args:
            max_pages: 허용한 페이지 수.
            url: 페이지네이션을 시작한 URL.
        """
        super().__init__(
            f"페이지 상한({max_pages})을 넘겼는데도 다음 페이지가 남아 있습니다: {url}. "
            "일부만 수집한 채로 진행하지 않습니다."
        )
        self.max_pages = max_pages
        self.url = url


class PaginationLoopError(CollectorError):
    """`rel="next"`가 방금 요청한 URL을 그대로 가리키는 경우.

    상한에 닿을 때까지 두면 같은 요청을 수십 번 반복하며 레이트리밋만 태운다.
    한 바퀴 만에 알아채고 멈춘다.

    Attributes:
        url: 자기 자신을 가리킨 URL.
    """

    def __init__(self, url: str) -> None:
        """예외를 만든다.

        Args:
            url: 자기 자신을 가리킨 URL.
        """
        super().__init__(f'rel="next"가 방금 요청한 URL을 그대로 가리킵니다: {url}')
        self.url = url


class RetryLimitExceededError(CollectorError):
    """재시도 상한까지 시도했는데도 실패한 경우.

    무한 재시도는 하지 않는다. 상한에 닿으면 마지막 실패 원인을 들고 올라간다.

    Attributes:
        attempts: 실제로 시도한 총 횟수(최초 시도 포함).
        url: 요청한 URL.
        last_error: 마지막 시도의 실패 원인을 담은 설명.
    """

    def __init__(self, attempts: int, url: str, last_error: str) -> None:
        """예외를 만든다.

        Args:
            attempts: 실제로 시도한 총 횟수(최초 시도 포함).
            url: 요청한 URL.
            last_error: 마지막 시도의 실패 원인을 담은 설명.
        """
        super().__init__(
            f"재시도 상한을 초과했습니다: {attempts}회 시도 후 실패 "
            f"(url={url}, 마지막 원인={last_error})"
        )
        self.attempts = attempts
        self.url = url
        self.last_error = last_error
