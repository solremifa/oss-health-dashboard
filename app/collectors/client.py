"""GitHub REST API HTTP 클라이언트.

`httpx2.Client`를 감싸 **인증 헤더 고정 · 레이트리밋 대기 · 백오프 재시도 · 재시도
상한**을 한 곳에 모은다. 응답 해석(페이지네이션, 스키마 검증)은 이 모듈의 책임이
아니라서 `httpx2.Response`를 그대로 돌려준다.

PyGithub을 쓰지 않는 이유가 바로 이 파일이다. 커서 페이지네이션·조건부 요청·
레이트리밋 백오프는 이 프로젝트에서 설명할 가치가 있는 부분인데 PyGithub은 그걸
전부 내부에 감춘다. 자세한 배경은 `CLAUDE.md` 3절 참고.

## 테스트 가능성을 위해 주입받는 것

`sleep`과 `now`를 인자로 받는다. 레이트리밋 재시도는 정상 운영에서 거의 발동하지
않아(요청 예산이 시간당 한도의 10%) 실제로 돌려보는 것으로는 검증되지 않는다.
`httpx2.MockTransport`로 429를 주입하고 `sleep`을 기록용 대역으로 바꿔야
"정말 기다렸다가 재시도했는지"를 확인할 수 있다. `docs/findings.md` 5절 참고.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Final

import httpx2
from pydantic import SecretStr

from app.collectors.errors import GitHubAPIError, RateLimitError, RetryLimitExceededError
from app.logging import get_logger

logger = get_logger(__name__)

API_BASE_URL: Final = "https://api.github.com"

# Accept 헤더는 고정한다. ETag가 요청 헤더 조합에 종속돼서, Accept가 흔들리면
# 같은 ETag로도 304가 아니라 200이 오고 **에러 없이 전체를 다시 받는다**.
# 증분 수집(#7)이 작동하는 것처럼 보이면서 매번 전체 재수집으로 퇴화한다.
# docs/findings.md 함정 5.
ACCEPT_HEADER: Final = "application/vnd.github+json"
API_VERSION_HEADER: Final = "2022-11-28"
USER_AGENT: Final = "oss-health-dashboard"

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_MAX_RETRIES: Final = 5
DEFAULT_BACKOFF_BASE_SECONDS: Final = 1.0
DEFAULT_MAX_WAIT_SECONDS: Final = 120.0

_NOT_MODIFIED: Final = 304
_FORBIDDEN: Final = 403
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR_FLOOR: Final = 500


def _parse_retry_after(value: str | None, now_epoch: float) -> float | None:
    """`Retry-After` 헤더를 초 단위로 바꾼다.

    HTTP 명세상 이 헤더는 초(delta-seconds) 또는 HTTP-date 두 형식을 모두 허용한다.
    GitHub은 초를 보내지만, 프록시가 끼면 날짜 형식이 올 수 있어 둘 다 받는다.

    Args:
        value: 헤더 값. 헤더가 없으면 `None`.
        now_epoch: 현재 시각(epoch 초). HTTP-date를 상대 시간으로 바꿀 때 쓴다.

    Returns:
        기다려야 할 초. 헤더가 없거나 해석할 수 없으면 `None`.
    """
    if value is None:
        return None

    stripped = value.strip()
    try:
        return float(int(stripped))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        # 해석 못 하는 값을 0초로 뭉개면 재시도 폭풍이 된다. 모른다고 답한다.
        logger.warning("Retry-After 헤더를 해석하지 못했습니다: %r", value)
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() - now_epoch


def _parse_reset_at(value: str | None) -> datetime | None:
    """`X-RateLimit-Reset` 헤더(epoch 초)를 timezone-aware UTC로 바꾼다.

    Args:
        value: 헤더 값. 헤더가 없으면 `None`.

    Returns:
        리셋 시각. 헤더가 없거나 숫자가 아니면 `None`.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value.strip()), tz=UTC)
    except ValueError:
        logger.warning("X-RateLimit-Reset 헤더를 해석하지 못했습니다: %r", value)
        return None


def _is_rate_limited(response: httpx2.Response) -> bool:
    """레이트리밋 때문에 거절된 응답인지 판별한다.

    GitHub은 2차(secondary) 레이트리밋에 429를, 1차 레이트리밋 소진에는 403을
    쓴다. **403을 전부 레이트리밋으로 보면 안 된다** — 권한 부족도 403이고,
    그건 기다린다고 풀리지 않는다. `X-RateLimit-Remaining: 0`이나 `Retry-After`가
    함께 왔을 때만 레이트리밋으로 본다.

    Args:
        response: 판별할 응답.

    Returns:
        레이트리밋으로 거절된 응답이면 `True`.
    """
    if response.status_code == _TOO_MANY_REQUESTS:
        return True
    if response.status_code != _FORBIDDEN:
        return False
    quota_exhausted = response.headers.get("X-RateLimit-Remaining") == "0"
    return quota_exhausted or "Retry-After" in response.headers


def _error_message(response: httpx2.Response) -> str:
    """오류 응답에서 사람이 읽을 설명을 뽑는다.

    Args:
        response: 오류 응답.

    Returns:
        API가 준 `message` 필드. JSON이 아니면 본문 앞부분.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
    return response.text[:200]


class GitHubClient:
    """재시도와 레이트리밋 대기를 처리하는 GitHub REST API 클라이언트.

    `with` 문으로 쓰거나 `close()`를 직접 부른다.

    Attributes:
        max_retries: 최초 시도 이후 허용하는 재시도 횟수. 초과하면
            `RetryLimitExceededError`를 올린다.
    """

    def __init__(
        self,
        token: SecretStr | str,
        *,
        base_url: str = API_BASE_URL,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        """클라이언트를 만든다.

        Args:
            token: GitHub PAT. 미인증 레이트리밋(60 req/시간)으로는 수집이
                불가능하다.
            base_url: API 베이스 URL.
            transport: 테스트에서 `httpx2.MockTransport`를 끼우기 위한 자리.
            timeout: 요청 타임아웃(초).
            max_retries: 최초 시도 이후 허용하는 재시도 횟수.
            backoff_base_seconds: 지수 백오프의 기준 시간(초).
            max_wait_seconds: 레이트리밋 대기의 허용 상한(초). 넘으면 자지 않고
                `RateLimitError`를 올린다.
            sleep: 대기 함수. 테스트에서 실제로 자지 않도록 갈아끼운다.
            now: 현재 epoch 초를 주는 함수. 테스트에서 고정한다.
        """
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        self.max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._max_wait_seconds = max_wait_seconds
        self._sleep = sleep
        self._now = now
        self._client = httpx2.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            # 저장소 이름이 바뀌면 301이 온다. 따라가지 않으면 수집이 조용히 0건이 된다.
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": ACCEPT_HEADER,
                "X-GitHub-Api-Version": API_VERSION_HEADER,
                "User-Agent": USER_AGENT,
            },
        )

    def __enter__(self) -> GitHubClient:
        """컨텍스트 매니저 진입.

        Returns:
            자기 자신.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """컨텍스트 매니저 종료. 연결을 닫는다.

        Args:
            exc_type: 발생한 예외의 타입.
            exc: 발생한 예외.
            traceback: 트레이스백.
        """
        self.close()

    def close(self) -> None:
        """HTTP 연결을 닫는다."""
        self._client.close()

    def _backoff_seconds(self, attempt: int) -> float:
        """지수 백오프 대기 시간을 계산한다.

        지터를 넣지 않는다. 이 프로그램은 단일 프로세스가 API 하나를 순차로
        호출하므로 여러 클라이언트가 같은 시각에 몰리는 문제가 없고, 지터가
        없어야 테스트에서 대기 시간을 값으로 단언할 수 있다.

        Args:
            attempt: 방금 실패한 시도의 번호(1부터).

        Returns:
            기다릴 시간(초).
        """
        return self._backoff_base_seconds * (2 ** (attempt - 1))

    def _rate_limit_wait(self, response: httpx2.Response, url: str, attempt: int) -> float:
        """레이트리밋 응답을 보고 기다릴 시간을 정한다.

        Args:
            response: 레이트리밋으로 거절된 응답.
            url: 요청한 URL. 예외 메시지에 쓴다.
            attempt: 방금 실패한 시도의 번호(1부터). 헤더가 없을 때의 대비책.

        Returns:
            기다릴 시간(초).

        Raises:
            RateLimitError: 기다려야 할 시간이 `max_wait_seconds`를 넘는 경우.
        """
        now_epoch = self._now()
        reset_at = _parse_reset_at(response.headers.get("X-RateLimit-Reset"))

        # Retry-After가 있으면 그게 가장 정확하다. 2차 레이트리밋은 이것만 온다.
        wait = _parse_retry_after(response.headers.get("Retry-After"), now_epoch)
        if wait is None and reset_at is not None:
            wait = reset_at.timestamp() - now_epoch
        if wait is None:
            # 헤더가 아무것도 없으면 최소한 백오프는 한다. 즉시 재시도는 금지.
            wait = self._backoff_seconds(attempt)

        wait = max(wait, 0.0)
        if wait > self._max_wait_seconds:
            raise RateLimitError(wait, reset_at, url)
        return wait

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        """GET 요청을 보내고, 필요하면 기다렸다가 재시도한다.

        재시도하는 것과 하지 않는 것을 나눈다:

        - **재시도한다** — 레이트리밋(429 / 레이트리밋성 403), 5xx, 연결·타임아웃 오류.
        - **재시도하지 않는다** — 401·404·422 등 나머지 4xx. 같은 답을 다섯 번 더
          받고 레이트리밋만 태우게 된다.

        304는 오류가 아니라 정상 응답으로 그대로 돌려준다. 조건부 요청을 보낸
        호출자가 "안 바뀌었다"를 판단해야 한다.

        Args:
            url: 절대 URL 또는 베이스 URL 기준 경로. 페이지네이션의 `rel="next"`
                URL을 통째로 넘길 수 있도록 절대 URL을 받는다.
            params: 쿼리 파라미터.
            headers: 이 요청에만 추가할 헤더(`If-None-Match` 등).

        Returns:
            상태 코드가 400 미만인 응답(304 포함).

        Raises:
            GitHubAPIError: 재시도해도 소용없는 4xx 응답인 경우.
            RateLimitError: 레이트리밋 대기 시간이 허용치를 넘은 경우.
            RetryLimitExceededError: 재시도 상한까지 시도했는데도 실패한 경우.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx2.RequestError as exc:
                # 연결 실패·타임아웃. 응답 자체가 없으므로 헤더를 볼 수 없다.
                last_error = f"{type(exc).__name__}: {exc}"
                wait = self._backoff_seconds(attempt)
            else:
                if response.status_code < 400:
                    if response.status_code == _NOT_MODIFIED:
                        logger.debug("304 Not Modified: %s", url)
                    return response

                if _is_rate_limited(response):
                    wait = self._rate_limit_wait(response, url, attempt)
                    last_error = f"HTTP {response.status_code} (rate limited)"
                elif response.status_code >= _SERVER_ERROR_FLOOR:
                    wait = self._backoff_seconds(attempt)
                    last_error = f"HTTP {response.status_code}"
                else:
                    raise GitHubAPIError(response.status_code, url, _error_message(response))

            if attempt > self.max_retries:
                logger.error(
                    "재시도 상한(%d회) 초과로 요청을 포기합니다: %s (마지막 원인=%s)",
                    self.max_retries,
                    url,
                    last_error,
                )
                raise RetryLimitExceededError(attempt, url, last_error)

            logger.warning(
                "요청 실패, %.1f초 후 재시도합니다 (%d/%d): %s (원인=%s)",
                wait,
                attempt,
                self.max_retries,
                url,
                last_error,
            )
            self._sleep(wait)
