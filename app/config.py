"""애플리케이션 설정.

환경변수 또는 `.env` 파일에서 값을 읽어 Pydantic으로 검증한다.

필수 값이 비어 있으면 **기동 시점에** `ValidationError`로 실패한다. 수집이 절반쯤
진행된 뒤 401을 받고 죽는 것보다, 시작하기 전에 죽는 쪽이 낫기 때문이다.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    """환경변수에서 읽어 검증한 애플리케이션 설정.

    Attributes:
        github_token: GitHub PAT. 미인증 레이트리밋(60 req/시간)으로는 수집이
            불가능하므로 필수다. public 저장소 읽기 권한이면 충분하다.
        anthropic_api_key: 이슈 본문 분류에 사용할 Anthropic API 키.
        database_url: SQLAlchemy 접속 문자열. 코드에 경로를 하드코딩하지 않고
            이 값 하나로만 결정한다(PostgreSQL 이관 대비).
        target_repo: 분석 대상 저장소. `"owner/name"` 형식.
        window_days: 지표 집계 기간(일). `created_at` 기준으로 자른다.
        stale_days: "방치된 이슈" 판정 기준(일).
        log_level: 로그 레벨 이름.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        # Windows의 기본 인코딩(이 PC는 cp949)으로 읽히면 .env의 한글 주석에서
        # UnicodeDecodeError가 난다. docs/findings.md 함정 6 참고.
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_token: SecretStr
    anthropic_api_key: SecretStr
    database_url: str = "sqlite:///./oss_health.db"
    target_repo: str = "PrefectHQ/fastmcp"
    window_days: int = Field(default=180, gt=0)
    stale_days: int = Field(default=90, gt=0)
    log_level: str = "INFO"

    @field_validator("target_repo")
    @classmethod
    def _validate_target_repo(cls, value: str) -> str:
        """`owner/name` 형식인지 확인한다.

        Args:
            value: 검증할 저장소 식별자.

        Returns:
            앞뒤 공백을 제거한 값.

        Raises:
            ValueError: 슬래시가 정확히 하나가 아니거나 양쪽 중 한쪽이 비어 있는 경우.
        """
        cleaned = value.strip()
        parts = cleaned.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"TARGET_REPO는 'owner/name' 형식이어야 합니다: {value!r}")
        return cleaned

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """알려진 로그 레벨 이름인지 확인한다.

        Args:
            value: 검증할 로그 레벨 이름.

        Returns:
            대문자로 정규화한 값.

        Raises:
            ValueError: 알려진 레벨이 아닌 경우.
        """
        normalized = value.strip().upper()
        if normalized not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"LOG_LEVEL은 {sorted(_VALID_LOG_LEVELS)} 중 하나여야 합니다: {value!r}"
            )
        return normalized

    @model_validator(mode="after")
    def _validate_window_covers_stale(self) -> Settings:
        """집계 기간이 방치 판정 기준보다 짧지 않은지 확인한다.

        `stale_days`가 `window_days`보다 크면 "방치된 이슈"는 정의상 항상 0건이
        된다. 지표가 조용히 무의미해지는 것을 막기 위해 기동 시점에 거부한다.

        Returns:
            검증을 통과한 자기 자신.

        Raises:
            ValueError: `stale_days`가 `window_days`보다 큰 경우.
        """
        if self.stale_days > self.window_days:
            raise ValueError(
                f"STALE_DAYS({self.stale_days})가 WINDOW_DAYS({self.window_days})보다 "
                "크면 방치된 이슈가 항상 0건이 됩니다."
            )
        return self

    @property
    def repo_owner(self) -> str:
        """대상 저장소의 owner 부분."""
        return self.target_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        """대상 저장소의 name 부분."""
        return self.target_repo.split("/", 1)[1]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """설정을 한 번만 읽어 캐시한다.

    import 시점이 아니라 첫 호출 시점에 읽는다. 그래야 테스트가 환경변수를
    준비하기 전에 `ValidationError`로 터지지 않는다.

    Returns:
        검증된 설정 객체.
    """
    return Settings()  # type: ignore[call-arg]  # 값은 환경변수/.env에서 채워진다
