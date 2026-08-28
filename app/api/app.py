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
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import router
from app.config import Settings, get_settings
from app.logging import configure_logging, get_logger
from app.models import create_db_engine, create_session_factory

logger = get_logger(__name__)

TITLE = "OSS Health Dashboard API"


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

    logger.info("API를 준비했습니다. 대상 저장소=%s", resolved_settings.target_repo)
    return app
