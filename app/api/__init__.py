"""FastAPI 앱 조립, 라우터, 의존성, 요청/응답 모델.

라우터는 조립·검증·응답만 담당한다. 계산은 `analysis/`에, 조회는
`models/`에 위임하고 스스로 로직을 갖지 않는다.
"""

from app.api.app import create_app
from app.api.schemas import MetricsResponse, MetricsStatus

__all__ = ["MetricsResponse", "MetricsStatus", "create_app"]
