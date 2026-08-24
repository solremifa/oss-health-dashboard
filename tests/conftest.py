"""pytest 공통 픽스처."""

from __future__ import annotations

import pytest

# Settings가 읽는 환경변수 전부. 실제 셸이나 CI에 값이 설정돼 있으면 테스트가
# 그 값을 주워 읽어 결과가 환경에 따라 달라진다. 매 테스트마다 비운다.
_SETTINGS_ENV_VARS = (
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "TARGET_REPO",
    "WINDOW_DAYS",
    "STALE_DAYS",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch):
    """설정 관련 환경변수를 비운 상태에서 테스트를 시작한다."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def required_settings() -> dict[str, str]:
    """필수 값만 채운 최소 설정 딕셔너리."""
    return {
        "github_token": "ghp_test_token",
        "anthropic_api_key": "sk-ant-test-key",
    }
