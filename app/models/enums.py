"""DB에 저장하는 열거형과, 그것을 컬럼 타입으로 바꾸는 헬퍼.

## 왜 `Enum(...)`을 직접 쓰지 않고 헬퍼를 두는가

SQLAlchemy의 `Enum`은 인자 세 개를 **전부** 맞춰야 의도대로 동작한다. 하나라도
빠지면 에러 없이 다른 물건이 만들어진다:

| 빠뜨린 인자 | 실제로 만들어지는 것 |
|---|---|
| `native_enum=False` | PostgreSQL에서 네이티브 ENUM 타입 — 값 추가에 `ALTER TYPE`이 필요해진다 |
| `create_constraint=True` | **CHECK 없는 맨 VARCHAR.** 1.4부터 기본값이 `False`다 |
| `values_callable=...` | 값이 아니라 **멤버 이름**이 저장된다 (`"open"`이 아니라 `"OPEN"`) |

셋 다 조용히 통과하고 나중에야 드러나는 종류라, 매번 손으로 적는 대신 한 곳에서
만든다. 허용 값 목록도 여기서 파생되므로 프롬프트·스키마·DB CHECK가 어긋나지 않는다.
"""

from __future__ import annotations

from enum import Enum as PyEnum

from sqlalchemy import Enum as SaEnum


class IssueState(PyEnum):
    """이슈의 열림/닫힘 상태.

    GitHub이 값을 늘릴 여지가 없는 축이라 enum으로 받는다. 반대로 `state_reason`
    (`completed` / `duplicate` / `not_planned` / `null`)은 GitHub이 늘려온 이력이
    있어 문자열로 둔다 — 값이 하나 추가되는 순간 CHECK 제약이 수집을 통째로
    막아버리는 것보다 낫다.
    """

    OPEN = "open"
    CLOSED = "closed"


def sa_enum(enum_class: type[PyEnum], *, name: str) -> SaEnum:
    """Python enum을 CHECK 제약이 붙은 VARCHAR 컬럼 타입으로 만든다.

    Args:
        enum_class: 컬럼에 담을 Python enum 클래스.
        name: 제약 이름에 쓰일 타입 이름.

    Returns:
        `native_enum=False` · `create_constraint=True` · `values_callable`이 모두
        지정된 컬럼 타입.
    """
    return SaEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda enum: [member.value for member in enum],
    )
