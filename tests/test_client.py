"""`app.collectors.client` 테스트.

**레이트리밋 재시도는 정상 운영에서 거의 발동하지 않는다.** 요청 예산이 시간당
한도의 10%라(`docs/findings.md` 5절) 실제로 돌려봐도 429를 만날 일이 없고, 따라서
"돌려보니 되더라"로는 이 코드가 한 줄도 검증되지 않는다. 그래서 여기서는
`httpx2.MockTransport`로 429와 `Retry-After`를 인위적으로 주입하고, `sleep`을
기록용 대역으로 갈아끼워 **정말 그만큼 기다렸다가 재시도했는지**를 값으로 단언한다.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx2
import pytest

from app.collectors.client import GitHubClient
from app.collectors.errors import GitHubAPIError, RateLimitError, RetryLimitExceededError

# now()를 고정한다. X-RateLimit-Reset은 epoch 절대 시각이라 현재 시각이 흔들리면
# 기대하는 대기 시간도 흔들린다.
FIXED_NOW = 1_787_574_721.0


class _Recorder:
    """미리 짜 넣은 응답을 순서대로 돌려주는 MockTransport 핸들러.

    응답 목록이 요청 수보다 짧으면 마지막 항목을 계속 돌려준다. "계속 500을
    준다" 같은 시나리오를 짧게 쓰기 위한 것이다. 항목이 예외면 그 예외를
    올린다(연결 실패 재현용).
    """

    def __init__(self, responses: list[httpx2.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)


def _build(
    responses: list[httpx2.Response | Exception],
    **kwargs: object,
) -> tuple[GitHubClient, _Recorder, list[float]]:
    """클라이언트 + 요청 기록 + 대기 기록을 함께 만든다."""
    recorder = _Recorder(responses)
    slept: list[float] = []
    sleep: Callable[[float], None] = slept.append
    client = GitHubClient(
        "ghp_test_token",
        transport=httpx2.MockTransport(recorder),
        sleep=sleep,
        now=lambda: FIXED_NOW,
        backoff_base_seconds=1.0,
        **kwargs,  # type: ignore[arg-type]
    )
    return client, recorder, slept


def _ok(payload: object = None) -> httpx2.Response:
    return httpx2.Response(200, json=payload if payload is not None else {"ok": True})


# ---------------------------------------------------------------------------
# 정상 경로
# ---------------------------------------------------------------------------


def test_get_returns_response_without_retrying():
    client, recorder, slept = _build([_ok({"number": 1})])

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.status_code == 200
    assert response.json() == {"number": 1}
    assert recorder.call_count == 1
    assert slept == []


def test_request_carries_auth_and_pinned_accept_headers():
    client, recorder, _ = _build([_ok()])

    with client:
        client.get("/repos/o/r/issues")

    headers = recorder.requests[0].headers
    assert headers["authorization"] == "Bearer ghp_test_token"
    # Accept가 흔들리면 ETag가 무력화된다(함정 5). 고정됐는지 확인한다.
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"


def test_per_request_headers_are_passed_through():
    """조건부 요청(#7)이 If-None-Match를 얹을 수 있어야 한다."""
    client, recorder, _ = _build([_ok()])

    with client:
        client.get("/repos/o/r/issues", headers={"If-None-Match": 'W/"abc"'})

    assert recorder.requests[0].headers["if-none-match"] == 'W/"abc"'


def test_not_modified_is_returned_as_a_normal_response():
    """304는 오류가 아니다. 재시도하지 않고 그대로 돌려준다."""
    client, recorder, slept = _build([httpx2.Response(304)])

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.status_code == 304
    assert recorder.call_count == 1
    assert slept == []


# ---------------------------------------------------------------------------
# 레이트리밋 — 429 + Retry-After 주입
# ---------------------------------------------------------------------------


def test_429_with_retry_after_waits_that_long_then_succeeds():
    client, recorder, slept = _build(
        [
            httpx2.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            _ok({"number": 42}),
        ]
    )

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.json() == {"number": 42}
    assert recorder.call_count == 2
    # 백오프(1초)가 아니라 헤더가 시킨 7초를 기다려야 한다.
    assert slept == [pytest.approx(7.0)]


def test_429_retried_repeatedly_until_success():
    client, recorder, slept = _build(
        [
            httpx2.Response(429, headers={"Retry-After": "2"}),
            httpx2.Response(429, headers={"Retry-After": "3"}),
            _ok(),
        ]
    )

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.status_code == 200
    assert recorder.call_count == 3
    assert slept == [pytest.approx(2.0), pytest.approx(3.0)]


def test_retry_after_accepts_http_date_format():
    """명세상 Retry-After는 HTTP-date도 허용한다. 프록시가 끼면 실제로 온다."""
    client, _, slept = _build(
        [
            # FIXED_NOW = 2026-08-24T12:32:01Z 기준 30초 뒤.
            httpx2.Response(429, headers={"Retry-After": "Mon, 24 Aug 2026 12:32:31 GMT"}),
            _ok(),
        ]
    )

    with client:
        client.get("/repos/o/r/issues")

    assert slept == [pytest.approx(30.0, abs=1.0)]


def test_unparseable_retry_after_falls_back_to_backoff_not_zero():
    """해석 못 하는 값을 0초로 뭉개면 재시도 폭풍이 된다."""
    # HTTP 헤더 값은 ASCII만 담을 수 있으므로 숫자도 날짜도 아닌 ASCII 문자열을 쓴다.
    client, _, slept = _build([httpx2.Response(429, headers={"Retry-After": "soon"}), _ok()])

    with client:
        client.get("/repos/o/r/issues")

    assert slept == [pytest.approx(1.0)]


def test_403_with_exhausted_quota_waits_until_reset():
    """1차 레이트리밋 소진은 429가 아니라 403으로 온다."""
    client, recorder, slept = _build(
        [
            httpx2.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(FIXED_NOW) + 45),
                },
                json={"message": "API rate limit exceeded"},
            ),
            _ok(),
        ]
    )

    with client:
        client.get("/repos/o/r/issues")

    assert recorder.call_count == 2
    assert slept == [pytest.approx(45.0)]


def test_rate_limit_wait_longer_than_cap_raises_instead_of_sleeping():
    """리셋까지 한 시간 남았는데 요청 함수 안에서 한 시간을 자면 안 된다.

    기다릴지 포기할지는 호출자가 정할 문제라 예외로 올린다.
    """
    client, recorder, slept = _build(
        [
            httpx2.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(FIXED_NOW) + 3600),
                },
            )
        ],
        max_wait_seconds=120.0,
    )

    with client, pytest.raises(RateLimitError) as excinfo:
        client.get("/repos/o/r/issues")

    assert excinfo.value.wait_seconds == pytest.approx(3600.0)
    assert excinfo.value.reset_at is not None
    assert recorder.call_count == 1
    assert slept == []  # 자지 않았다


def test_plain_403_is_not_treated_as_rate_limit():
    """권한 부족도 403이다. 기다린다고 풀리지 않으므로 즉시 실패해야 한다."""
    client, recorder, slept = _build(
        [httpx2.Response(403, json={"message": "Resource not accessible by personal access token"})]
    )

    with client, pytest.raises(GitHubAPIError) as excinfo:
        client.get("/repos/o/r/issues")

    assert excinfo.value.status_code == 403
    assert recorder.call_count == 1
    assert slept == []


# ---------------------------------------------------------------------------
# 5xx · 연결 실패 · 재시도 상한
# ---------------------------------------------------------------------------


def test_5xx_is_retried_with_exponential_backoff():
    client, recorder, slept = _build(
        [httpx2.Response(502), httpx2.Response(503), _ok()],
    )

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.status_code == 200
    assert recorder.call_count == 3
    assert slept == [pytest.approx(1.0), pytest.approx(2.0)]


def test_connection_error_is_retried_then_succeeds():
    client, recorder, slept = _build(
        [httpx2.ConnectError("connection refused"), _ok()],
    )

    with client:
        response = client.get("/repos/o/r/issues")

    assert response.status_code == 200
    assert recorder.call_count == 2
    assert slept == [pytest.approx(1.0)]


def test_timeout_is_retried():
    client, recorder, _ = _build([httpx2.ReadTimeout("timed out"), _ok()])

    with client:
        client.get("/repos/o/r/issues")

    assert recorder.call_count == 2


def test_retry_limit_exceeded_raises_and_stops():
    """무한 재시도 금지. 상한에 닿으면 마지막 원인을 들고 올라간다."""
    client, recorder, slept = _build([httpx2.Response(500)], max_retries=3)

    with client, pytest.raises(RetryLimitExceededError) as excinfo:
        client.get("/repos/o/r/issues")

    # 최초 1회 + 재시도 3회 = 4회에서 멈춘다.
    assert recorder.call_count == 4
    assert excinfo.value.attempts == 4
    assert "500" in excinfo.value.last_error
    assert slept == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(4.0)]


def test_retry_limit_exceeded_on_persistent_connection_failure():
    client, recorder, _ = _build([httpx2.ConnectError("no route to host")], max_retries=2)

    with client, pytest.raises(RetryLimitExceededError) as excinfo:
        client.get("/repos/o/r/issues")

    assert recorder.call_count == 3
    assert "ConnectError" in excinfo.value.last_error


def test_retry_limit_exceeded_logs_an_error(caplog):
    """조용히 포기하지 않는다 — ERROR 로그가 남아야 한다."""
    client, _, _ = _build([httpx2.Response(500)], max_retries=1)

    with caplog.at_level("ERROR"), client, pytest.raises(RetryLimitExceededError):
        client.get("/repos/o/r/issues")

    assert any(record.levelname == "ERROR" for record in caplog.records)


# ---------------------------------------------------------------------------
# 재시도하면 안 되는 오류
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 404, 410, 422])
def test_non_retryable_4xx_fails_immediately(status: int):
    """같은 답을 다섯 번 더 받고 레이트리밋만 태우는 일을 막는다."""
    client, recorder, slept = _build([httpx2.Response(status, json={"message": "Not Found"})])

    with client, pytest.raises(GitHubAPIError) as excinfo:
        client.get("/repos/o/r/issues")

    assert excinfo.value.status_code == status
    assert recorder.call_count == 1
    assert slept == []


def test_error_message_survives_non_json_body():
    """오류 본문이 JSON이 아니어도(프록시의 HTML 오류 페이지 등) 터지지 않는다."""
    client, _, _ = _build([httpx2.Response(404, text="<html>nope</html>")])

    with client, pytest.raises(GitHubAPIError) as excinfo:
        client.get("/repos/o/r/issues")

    assert "nope" in str(excinfo.value)
