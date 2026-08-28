"""라우터가 받아 쓰는 의존성.

## 왜 `app.state`에서 꺼내는가

설정과 세션 팩토리를 모듈 전역에 두면 **import 시점에 `.env`와 DB가 필요해진다.**
테스트가 환경변수를 준비하기도 전에 `ValidationError`로 죽고, 인메모리 DB로 갈아
끼울 자리도 없다. 그래서 `create_app()`이 만들어 `app.state`에 얹고, 여기서는
요청마다 꺼내 쓰기만 한다.

## 시각도 의존성이다

`now`를 라우터 안에서 `datetime.now()`로 만들면 응답의 숫자가 **실행 시각에 따라
달라져** 테스트가 정확한 값을 확인할 수 없다. 6개월 경계에 걸친 이슈가 있는지
없는지로 지표가 흔들리는 것이 정확히 이 프로젝트가 피하려는 종류의 실패다
(`app/analysis/metrics.py` 참고). 의존성으로 두면 테스트가 고정할 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings


def get_settings(request: Request) -> Settings:
    """이 앱이 기동할 때 검증한 설정을 돌려준다.

    Args:
        request: 처리 중인 요청.

    Returns:
        `create_app()`이 얹어 둔 설정.
    """
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """요청 하나에 세션 하나를 열고 끝나면 닫는다.

    **커밋하지 않는다.** 이 API는 읽기만 하고, 트랜잭션 경계를 정하는 것은 원래
    호출자의 몫이다(`CLAUDE.md` 7절).

    Args:
        request: 처리 중인 요청.

    Yields:
        요청 동안 쓸 세션.
    """
    with request.app.state.session_factory() as session:
        yield session


def get_now() -> datetime:
    """지표 계산의 기준 시각.

    Returns:
        timezone-aware한 현재 시각(UTC).
    """
    return datetime.now(UTC)


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
NowDep = Annotated[datetime, Depends(get_now)]
