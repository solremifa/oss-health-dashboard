"""ASGI 진입점.

`uvicorn app.api.main:app`이 찾는 이름을 여기 하나만 둔다. 이 모듈은 import되는
순간 설정을 읽으므로 **`.env`가 비어 있으면 기동 자체가 실패한다.** 의도한 동작이다
-- 수집이 절반쯤 진행된 뒤 401을 받고 죽는 것보다 시작하기 전에 죽는 쪽이 낫다
(`app/config.py`).
"""

from __future__ import annotations

from app.api.app import create_app

app = create_app()
