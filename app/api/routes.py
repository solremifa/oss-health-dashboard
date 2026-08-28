"""지표 라우터.

라우터는 **스스로 계산하지 않는다.** 조회는 `models/`, 계산은 `analysis/`에
맡기고 여기서는 셋을 잇고 상태를 판정할 뿐이다(`CLAUDE.md` 5절).

## 404와 `pending`을 뭉개지 않는다

세 상태를 구분한다. 뒤의 둘을 하나로 합치면 **오타로 친 저장소 이름이 "수집 중"
으로 보이고**, 사용자는 기다리면 값이 나올 거라고 믿는다.

| 상태 | 응답 |
|---|---|
| 이 대시보드가 모르는 저장소 | **404** |
| 아는 저장소인데 아직 수집 전 | 200 + `status="pending"` |
| 수집이 끝남 | 200 + `status="ready"` |

**"안다"의 근거는 두 가지다** -- 설정된 `TARGET_REPO`이거나, `sync_state`에 커서가
남아 있는 저장소다. 뒤쪽은 "전에 수집한 적이 있다"는 뜻이라, `TARGET_REPO`를 바꿔
돌린 뒤에도 예전 저장소의 지표를 계속 볼 수 있다.

**GitHub에 그 저장소가 실재하는지는 묻지 않는다.** 확인하려면 요청 하나를 더
써야 하고, 그 요청은 레이트리밋을 쓰면서 이 API를 GitHub 장애에 묶는다. 여기서
404는 "GitHub에 없다"가 아니라 **"이 대시보드가 다루는 저장소가 아니다"**이다.

## `pending` 판정은 `last_synced_at`으로 한다

이슈 건수가 0인지로 판정하면, **수집이 끝났는데 기간 안 이슈가 없는 저장소가
영원히 "수집 중"으로 보인다.** 커서 행의 존재와 `last_synced_at`은 "수집을 한 번
끝냈다"를 뜻하므로 그쪽을 본다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.analysis.metrics import compute_metrics
from app.api.deps import NowDep, SessionDep, SettingsDep
from app.api.schemas import MetricsResponse
from app.collectors.sync import ISSUES_RESOURCE
from app.logging import get_logger
from app.models import load_issue_facts, load_sync_cursor

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get(
    "/repos/{owner}/{repo}/metrics",
    response_model=MetricsResponse,
    summary="저장소 지표 4개",
    responses={status.HTTP_404_NOT_FOUND: {"description": "이 대시보드가 다루는 저장소가 아님"}},
)
def read_metrics(
    owner: str,
    repo: str,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
) -> MetricsResponse:
    """저장소 하나의 지표 4개를 돌려준다.

    Args:
        owner: 저장소 owner.
        repo: 저장소 이름.
        settings: 기동 시점에 검증된 설정.
        session: 요청 동안 쓸 세션.
        now: 집계 기준 시각.

    Returns:
        지표 4개. 아직 수집 전이면 `status="pending"`이고 지표는 전부 `null`이다.

    Raises:
        HTTPException: 이 대시보드가 다루지 않는 저장소인 경우 404.
    """
    repo_full_name = f"{owner}/{repo}"
    cursor = load_sync_cursor(session, repo_full_name, ISSUES_RESOURCE)

    if cursor is None and repo_full_name != settings.target_repo:
        logger.info("모르는 저장소에 대한 지표 요청입니다: %s", repo_full_name)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{repo_full_name}은 이 대시보드가 다루는 저장소가 아닙니다. "
                f"현재 대상은 {settings.target_repo}입니다."
            ),
        )

    if cursor is None or cursor.last_synced_at is None:
        logger.info("아직 수집 전인 저장소입니다: %s", repo_full_name)
        return MetricsResponse.pending(
            repo_full_name, generated_at=now, window_days=settings.window_days
        )

    report = compute_metrics(
        load_issue_facts(session, repo_full_name),
        now=now,
        window_days=settings.window_days,
        stale_days=settings.stale_days,
    )
    return MetricsResponse.from_report(repo_full_name, report)
