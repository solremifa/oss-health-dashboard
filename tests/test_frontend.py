"""대시보드 정적 파일과 응답 스냅샷 테스트.

화면 자체(막대의 길이, 색)는 여기서 검증하지 않는다. 그건 `frontend/format.js`의
순수 함수로 옮겨 두고 `node --test`가 본다(`frontend/format.test.js`). 이 파일이
고정하려는 것은 **파이썬 쪽과 프론트 쪽이 어긋나는 두 지점**이다.

1. **마운트 순서.** `/`에 건 정적 파일 마운트는 앞에서 걸리지 않은 경로를 전부
   받는다. 라우터보다 먼저 붙이면 `/api/...`가 통째로 404가 되는데, 브라우저에서만
   드러나고 기존 API 테스트는 전부 통과한다.

2. **스냅샷 드리프트.** `frontend/fixtures/*.json`은 `pending`과 `null` 지표를
   화면에서 확인하려고 둔 고정 응답이다. 응답 모델이 바뀌면 스냅샷은 조용히
   낡고, 그걸 보면서 "화면이 잘 돈다"고 판단하게 된다. 왕복시켜 비교한다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api import create_app
from app.api.app import FRONTEND_DIR
from app.api.schemas import MetricsResponse, MetricsStatus
from app.config import Settings
from tests.conftest import TEST_REPO

FIXTURE_DIR = FRONTEND_DIR / "fixtures"

# `frontend/app.js`의 FIXTURES 허용 목록과 같아야 한다. 한쪽에만 이름을 늘리면
# 전환기 링크가 404가 되거나, 아무도 안 보는 스냅샷이 남는다.
FIXTURE_NAMES = ("ready", "pending", "empty")

STATIC_FILES = ("index.html", "app.js", "format.js", "styles.css")


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], required_settings: dict[str, str]
) -> Iterator[TestClient]:
    """대시보드가 마운트된 앱의 테스트 클라이언트."""
    app = create_app(
        settings=Settings(**required_settings, target_repo=TEST_REPO),
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        yield client


def read_fixture(name: str) -> dict:
    """스냅샷 파일을 읽는다.

    `encoding="utf-8"`을 명시한다. 이 PC의 기본 인코딩은 cp949라서, 빼면 스냅샷에
    한글이 들어가는 순간 Windows에서만 깨진다(`docs/findings.md` 함정 6).
    """
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 정적 파일 마운트
# ---------------------------------------------------------------------------


def test_루트가_대시보드를_준다(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "OSS 헬스체크 대시보드" in response.text


@pytest.mark.parametrize("filename", STATIC_FILES)
def test_대시보드_파일이_전부_서빙된다(client: TestClient, filename: str) -> None:
    assert client.get(f"/{filename}").status_code == 200


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_스냅샷이_서빙된다(client: TestClient, name: str) -> None:
    """전환기가 `./fixtures/<name>.json`을 fetch한다. 경로가 어긋나면 화면이 빈다."""
    response = client.get(f"/fixtures/{name}.json")

    assert response.status_code == 200
    assert response.json()["repo"]


def test_마운트가_API를_가리지_않는다(client: TestClient) -> None:
    """`/`에 건 마운트가 라우터를 삼키지 않는지.

    이 테스트가 없으면 마운트를 `include_router`보다 먼저 옮겼을 때 API가 통째로
    404가 되는 것을 브라우저에서야 알게 된다.
    """
    assert client.get(f"/api/repos/{TEST_REPO}/metrics").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_대시보드가_없어도_API는_뜬다(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    required_settings: dict[str, str],
    tmp_path: Path,
) -> None:
    """`frontend/`가 없는 설치에서도 기동은 성공해야 한다."""
    monkeypatch.setattr("app.api.app.FRONTEND_DIR", tmp_path / "없는디렉토리")

    app = create_app(
        settings=Settings(**required_settings, target_repo=TEST_REPO),
        session_factory=session_factory,
    )

    with TestClient(app) as client:
        assert client.get(f"/api/repos/{TEST_REPO}/metrics").status_code == 200
        assert client.get("/").status_code == 404


# ---------------------------------------------------------------------------
# 스냅샷이 응답 모델과 어긋나지 않는가
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_스냅샷이_응답모델과_정확히_일치한다(name: str) -> None:
    """검증만으로는 부족해서 왕복시켜 비교한다.

    Pydantic은 기본적으로 **모르는 키를 조용히 버린다.** `model_validate()`만
    부르면 `stale_count`를 `stale_counts`로 잘못 쓴 스냅샷도 통과하고, 화면에서는
    그 자리가 `undefined`가 된다. 직렬화한 결과와 파일을 통째로 비교하면 빠진 키와
    남는 키가 둘 다 걸린다.
    """
    payload = read_fixture(name)

    assert MetricsResponse.model_validate(payload).model_dump(mode="json") == payload


def test_pending_스냅샷은_지표가_전부_null이다() -> None:
    payload = read_fixture("pending")

    assert payload["status"] == MetricsStatus.PENDING
    for field in ("issue_count", "stale_issues", "categories", "sentiments", "response_time"):
        assert payload[field] is None, f"{field}가 null이 아니면 pending 화면을 확인할 수 없다"


def test_empty_스냅샷은_ready인데_비율만_null이다() -> None:
    """`pending`과 다른 상태라는 것이 스냅샷 수준에서 보장돼야 한다.

    둘이 같아지면 전환기로 화면을 비교하는 의미가 사라진다. 여기서 고정하는 것은
    **분모가 0이라 비율을 못 내지만, 건수는 살아 있다**는 모양이다.
    """
    payload = read_fixture("empty")

    assert payload["status"] == MetricsStatus.READY

    # 비율은 낼 수 없다 -- 0.0이 아니라 null이다.
    assert payload["stale_issues"]["ratio"] is None
    assert payload["categories"]["ratios"] is None
    assert payload["sentiments"]["ratios"] is None
    assert payload["response_time"]["median_seconds"] is None

    # 그래도 지표 객체 자체는 있고 건수를 갖는다. pending과 갈리는 지점이다.
    assert payload["issue_count"] == 0
    assert payload["stale_issues"]["open_total"] == 0
    assert set(payload["categories"]["counts"]) == {"bug", "feature_request", "question", "other"}


def test_ready_스냅샷은_지표가_전부_채워져_있다() -> None:
    payload = read_fixture("ready")

    assert payload["status"] == MetricsStatus.READY
    assert payload["stale_issues"]["ratio"] is not None
    assert payload["categories"]["ratios"] is not None
    assert payload["sentiments"]["ratios"] is not None
    assert payload["response_time"]["median_seconds"] is not None

    # 세지 못한 건수가 0이 아니어야 화면에서 그 표시를 확인할 수 있다.
    assert payload["categories"]["unanalyzed_count"] > 0
    assert payload["response_time"]["no_response_count"] > 0
    assert payload["stale_issues"]["unchecked_count"] > 0


def test_스냅샷_파일이_그것뿐이다() -> None:
    """허용 목록에 없는 스냅샷이 남아 있지 않은지.

    `frontend/app.js`는 이름을 허용 목록으로 거른다. 파일만 늘리면 전환기에서
    닿을 수 없는 채로 남아 낡아간다.
    """
    found = {path.stem for path in FIXTURE_DIR.glob("*.json")}

    assert found == set(FIXTURE_NAMES)
