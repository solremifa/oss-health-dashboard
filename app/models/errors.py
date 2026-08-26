"""저장 계층 예외."""

from __future__ import annotations


class RepositoryError(Exception):
    """저장 계층의 모든 예외가 상속하는 기반 클래스."""


class ConflictingRecordError(RepositoryError):
    """UNIQUE 위반을 처리하려 했는데 갱신할 행을 찾지 못한 경우.

    upsert는 "PK가 겹쳤다"를 전제로 INSERT 실패를 UPDATE로 바꾼다. 그런데 겹친 것이
    PK가 아니라 **다른 UNIQUE 제약**이면 갱신 대상이 0행이 된다. 예를 들어 같은
    `(repo_full_name, number)`에 다른 `id`가 오는 경우다.

    이건 재시도로 풀리는 상황이 아니라 데이터가 이상하다는 신호다. 조용히 넘기면
    그 이슈만 영원히 저장되지 않고, 수집은 성공했다고 보고한다.

    Attributes:
        table: 대상 테이블 이름.
        key: 갱신을 시도한 키.
    """

    def __init__(self, table: str, key: dict[str, object]) -> None:
        """예외를 만든다.

        Args:
            table: 대상 테이블 이름.
            key: 갱신을 시도한 키.
        """
        super().__init__(
            f"{table}에 UNIQUE 위반이 났지만 키 {key!r}로 갱신할 행이 없습니다. "
            "PK가 아닌 다른 UNIQUE 제약이 충돌했을 가능성이 높습니다."
        )
        self.table = table
        self.key = key
