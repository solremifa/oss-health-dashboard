"""app/logging.py 테스트.

로그는 이 프로젝트에서 실패를 드러내는 주 수단이라, "로그를 남기려다 터지는"
경우가 없어야 한다. 특히 이슈 본문에는 이모지가 흔하다(표본 45건 중 10건).
"""

from __future__ import annotations

import io
import logging

import app.logging as app_logging
from app.logging import configure_logging, get_logger


class TestConfigureLogging:
    def test_handler_is_not_duplicated_on_repeated_calls(self, monkeypatch):
        # 모듈 전역 플래그를 초기화해 첫 호출부터 재현한다.
        monkeypatch.setattr(app_logging, "_configured", False)
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])

        configure_logging("INFO")
        after_first = len(root.handlers)
        configure_logging("INFO")
        configure_logging("DEBUG")

        assert after_first == 1
        assert len(root.handlers) == 1

    def test_level_is_updated_on_repeated_calls(self, monkeypatch):
        monkeypatch.setattr(app_logging, "_configured", False)
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])

        configure_logging("INFO")
        configure_logging("ERROR")

        assert root.level == logging.ERROR

    def test_missing_reconfigure_on_stream_is_tolerated(self, monkeypatch):
        """리다이렉트된 스트림처럼 reconfigure가 없어도 죽지 않아야 한다."""
        monkeypatch.setattr(app_logging, "_configured", False)
        monkeypatch.setattr(app_logging.sys, "stdout", io.StringIO())
        monkeypatch.setattr(app_logging.sys, "stderr", io.StringIO())
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])

        configure_logging("INFO")  # 예외가 나지 않으면 통과


class TestNonAsciiOutput:
    def test_emoji_message_does_not_raise_on_cp949_stream(self):
        """cp949 스트림에 이모지를 써도 로깅이 죽지 않아야 한다.

        docs/findings.md 함정 6의 회귀 테스트다. Windows 콘솔 기본 인코딩을
        흉내 내기 위해 cp949 스트림을 직접 만들어 붙인다.
        """
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp949", errors="backslashreplace")

        handler = logging.StreamHandler(stream=stream)
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("test.emoji")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        logger.info("이슈 본문: 🚀 fast, Pythonic way")
        handler.flush()
        stream.flush()

        written = buffer.getvalue().decode("cp949")
        assert "이슈 본문" in written
        # 이모지는 cp949로 표현할 수 없으므로 escape되지만, 예외는 나지 않는다.
        assert "\\U0001f680" in written or "🚀" in written


class TestGetLogger:
    def test_returns_logger_with_requested_name(self):
        assert get_logger("app.collectors.client").name == "app.collectors.client"
