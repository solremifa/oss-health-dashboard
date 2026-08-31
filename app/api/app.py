"""FastAPI 앱 조립.

## 팩토리 함수인 이유

모듈 전역에 `app = FastAPI()`를 두면 **import하는 순간 `.env`와 DB 연결이
필요해진다.** 테스트가 인메모리 DB로 갈아 끼울 자리도 없고, 설정을 준비하기 전에
`ValidationError`로 죽는다. 그래서 만드는 시점을 호출자가 정하게 하고, 설정과 세션
팩토리를 주입할 수 있게 열어 뒀다.

테스트는 `create_db_engine("sqlite://")`로 만든 엔진의 세션 팩토리를 넘긴다.
그 엔진은 인메모리 URL에 **`StaticPool`을 붙인다** -- 기본 풀은 연결마다 서로 다른
빈 DB를 열어서, `TestClient`가 다른 스레드에서 조회하는 순간 `no such table`이
난다(`app/models/db.py` · `CLAUDE.md` 10절).

## 대시보드를 이 앱이 함께 내보내는 이유

`frontend/`를 `/`에 마운트한다. 정적 파일 서버를 따로 띄우면 **오리진이 갈라져
CORS 설정이 필요해지고**, `file://`로 열면 브라우저가 같은 폴더의 JSON `fetch`
자체를 막는다. 둘 다 지표와 무관한 설정을 늘리는 쪽이라, 한 프로세스가 API와
화면을 같이 내보낸다.

**API 라우터를 먼저 붙이고 그다음에 마운트한다.** `/`에 건 마운트는 앞에서 걸리지
않은 경로를 전부 받으므로, 순서가 바뀌면 `/api/...`와 `/docs`가 정적 파일 쪽으로
넘어가 404가 난다.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import router
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger
from app.models import create_db_engine, create_session_factory

logger = get_logger(__name__)

TITLE = "OSS Health Dashboard API"

# app/api/app.py -> app/api -> app -> 저장소 루트
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """앱을 만들어 라우터를 붙인다.

    Args:
        settings: 쓸 설정. 생략하면 환경변수/`.env`에서 읽는다.
        session_factory: 쓸 세션 팩토리. 생략하면 `DATABASE_URL`로 엔진을 만든다.

    Returns:
        요청을 받을 준비가 된 앱.
    """
    resolved_settings = settings if settings is not None else get_settings()
    configure_logging(resolved_settings.log_level)

    if session_factory is None:
        # 접속 문자열 하나로만 대상을 정한다. 경로 하드코딩 금지(`CLAUDE.md` 3절).
        engine = create_db_engine(resolved_settings.database_url)
        session_factory = create_session_factory(engine)

    app = FastAPI(title=TITLE, version="0.1.0")
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.include_router(router)
    _mount_frontend(app)

    logger.info("API를 준비했습니다. 대상 저장소=%s", resolved_settings.target_repo)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """대시보드 정적 파일을 `/`에 붙인다.

    `frontend/`가 없어도 앱은 뜬다. 화면 없이 API만 쓰는 경우(휠로 설치했거나
    `/docs`만 볼 때)까지 기동 실패로 만들 이유가 없다. 대신 조용히 넘어가지 않고
    경고를 남긴다 -- 있어야 할 것이 없는데 로그가 깨끗하면 `/`가 404일 때 원인을
    찾을 단서가 사라진다.

    Args:
        app: 마운트할 앱.
    """
    if not FRONTEND_DIR.is_dir():
        logger.warning("대시보드를 찾지 못해 API만 제공합니다: %s", FRONTEND_DIR)
        return

    # html=True여야 `/`가 index.html을 준다. 없으면 디렉토리 목록도 아니고 404다.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    logger.info("대시보드를 /에 붙였습니다: %s", FRONTEND_DIR)
