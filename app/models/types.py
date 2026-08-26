"""커스텀 컬럼 타입.

## 왜 맨 `DateTime`을 쓰지 않는가

SQLite에는 시간대를 담는 타입이 없다. `DateTime(timezone=True)`로 선언해도 값은
문자열로 들어가고 **읽을 때는 naive datetime으로 돌아온다.** 넣은 값과 꺼낸 값의
타입이 달라지는 것이다.

그대로 두면 이런 식으로 조용히 틀린다:

- 저장할 때는 aware, 읽을 때는 naive → `created_at >= cutoff` 비교가
  `TypeError`로 터지거나, 양쪽 다 naive가 되어 **9시간 밀린 채 성립한다.**
- 6개월 경계로 이슈를 자르는 계산이 경계 근처에서만 틀리므로 테스트를 통과한다.

그래서 경계에서 강제한다 — **쓸 때 naive를 거부하고, 읽을 때 UTC를 붙여 돌려준다.**
PostgreSQL(`TIMESTAMPTZ`)에서는 어차피 aware로 돌아오므로 이 타입을 그대로 써도
동작이 같다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """timezone-aware UTC datetime만 오가는 datetime 컬럼.

    naive datetime을 쓰려고 하면 조용히 UTC로 가정하지 않고 예외를 올린다.
    "이 시각이 UTC인가 로컬인가"를 추측하는 코드가 한 곳이라도 생기면 결국
    한 번은 틀리기 때문이다.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """저장 직전에 UTC aware인지 확인한다.

        Args:
            value: 저장할 값.
            dialect: 사용 중인 DB 방언.

        Returns:
            UTC로 맞춘 datetime. `None`이면 그대로.

        Raises:
            TypeError: datetime이 아닌 값인 경우.
            ValueError: timezone 정보가 없는 경우.
        """
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"datetime이 아닌 값은 저장할 수 없습니다: {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime은 저장할 수 없습니다. UTC로 aware하게 만들어 넘기세요."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """읽어온 값에 UTC를 붙여 돌려준다.

        SQLite는 시간대를 저장하지 못해 naive로 돌려준다. 저장 시점에 UTC로
        맞춰 넣었으므로 여기서 UTC를 붙이는 것은 추측이 아니다.

        Args:
            value: DB에서 읽은 값.
            dialect: 사용 중인 DB 방언.

        Returns:
            UTC aware datetime. `None`이면 그대로.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
