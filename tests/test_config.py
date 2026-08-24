"""app/config.py 테스트.

정상 경로보다 **실패 경로**가 많다. 설정은 잘못돼도 프로그램이 그럭저럭 도는
것처럼 보이다가 엉뚱한 숫자를 내놓는 종류의 코드라, 거부해야 할 입력을 실제로
거부하는지가 핵심 계약이다.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings


def _make(required_settings: dict[str, str], **overrides: Any) -> Settings:
    """`.env` 파일을 읽지 않는 Settings를 만든다."""
    return Settings(_env_file=None, **{**required_settings, **overrides})


class TestRequiredValues:
    def test_missing_both_secrets_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)

        missing = {error["loc"][0] for error in exc_info.value.errors()}
        assert missing == {"github_token", "anthropic_api_key"}

    def test_missing_github_token_is_rejected(self, required_settings):
        del required_settings["github_token"]

        with pytest.raises(ValidationError):
            Settings(_env_file=None, **required_settings)

    def test_secrets_are_not_exposed_in_repr(self, required_settings):
        settings = _make(required_settings)

        # SecretStr이 실수로 로그에 찍히는 것을 막는다.
        assert "ghp_test_token" not in repr(settings)
        assert settings.github_token.get_secret_value() == "ghp_test_token"


class TestTargetRepo:
    def test_defaults_to_the_target_repository(self, required_settings):
        settings = _make(required_settings)

        assert settings.target_repo == "PrefectHQ/fastmcp"
        assert settings.repo_owner == "PrefectHQ"
        assert settings.repo_name == "fastmcp"

    def test_surrounding_whitespace_is_stripped(self, required_settings):
        settings = _make(required_settings, target_repo="  owner/name  ")

        assert settings.target_repo == "owner/name"

    @pytest.mark.parametrize(
        "value",
        [
            "fastmcp",  # 슬래시 없음
            "a/b/c",  # 슬래시 둘
            "/fastmcp",  # owner 비어 있음
            "PrefectHQ/",  # name 비어 있음
            "",  # 빈 문자열
            "   ",  # 공백만
        ],
    )
    def test_malformed_repo_is_rejected(self, required_settings, value):
        with pytest.raises(ValidationError):
            _make(required_settings, target_repo=value)


class TestLogLevel:
    def test_lowercase_is_normalized(self, required_settings):
        settings = _make(required_settings, log_level="debug")

        assert settings.log_level == "DEBUG"

    def test_unknown_level_is_rejected(self, required_settings):
        with pytest.raises(ValidationError):
            _make(required_settings, log_level="VERBOSE")


class TestWindowAndStaleDays:
    def test_defaults_match_the_documented_scope(self, required_settings):
        settings = _make(required_settings)

        assert settings.window_days == 180  # 최근 6개월
        assert settings.stale_days == 90  # 90일 무응답

    @pytest.mark.parametrize("field", ["window_days", "stale_days"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_days_are_rejected(self, required_settings, field, value):
        with pytest.raises(ValidationError):
            _make(required_settings, **{field: value})

    def test_stale_longer_than_window_is_rejected(self, required_settings):
        # 이 조합이 통과하면 "방치된 이슈"는 정의상 항상 0건이 되어 지표가
        # 조용히 무의미해진다.
        with pytest.raises(ValidationError, match="STALE_DAYS"):
            _make(required_settings, window_days=90, stale_days=180)

    def test_stale_equal_to_window_is_allowed(self, required_settings):
        settings = _make(required_settings, window_days=90, stale_days=90)

        assert settings.stale_days == settings.window_days


class TestEnvFileEncoding:
    """docs/findings.md 함정 6에 대한 회귀 테스트.

    `.env`에 한글 주석과 이모지가 들어 있어도 읽혀야 한다. `env_file_encoding`을
    빼면 Windows(cp949)에서 UnicodeDecodeError로 터진다.
    """

    def test_utf8_env_file_with_korean_and_emoji_loads(self, tmp_path):
        env_file = tmp_path / ".env"
        with open(env_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "# 한글 주석과 이모지 🚀 가 들어 있는 .env\n"
                "GITHUB_TOKEN=ghp_from_file\n"
                "ANTHROPIC_API_KEY=sk-ant-from-file\n"
                "TARGET_REPO=PrefectHQ/fastmcp\n"
            )

        settings = Settings(_env_file=env_file)

        assert settings.github_token.get_secret_value() == "ghp_from_file"
        assert settings.target_repo == "PrefectHQ/fastmcp"

    def test_explicit_values_win_over_env_file(self, tmp_path, required_settings):
        env_file = tmp_path / ".env"
        with open(env_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("TARGET_REPO=someone/else\n")

        settings = Settings(_env_file=env_file, target_repo="explicit/wins", **required_settings)

        assert settings.target_repo == "explicit/wins"

    def test_env_file_wins_over_defaults(self, tmp_path, required_settings):
        env_file = tmp_path / ".env"
        with open(env_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("TARGET_REPO=someone/else\n")

        settings = Settings(_env_file=env_file, **required_settings)

        assert settings.target_repo == "someone/else"
