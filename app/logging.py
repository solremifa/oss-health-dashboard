"""로깅 설정.

이 프로젝트의 원칙 중 하나가 "조용히 버리지 않는다"이므로, 로그는 부가 기능이
아니라 실패를 드러내는 주 수단이다. 설정은 진입점(`scripts/`, FastAPI 기동)에서
한 번만 호출한다.
"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def configure_logging(level: str = "INFO") -> None:
    """루트 로거를 설정한다. 여러 번 호출해도 핸들러가 중복되지 않는다.

    표준 스트림을 UTF-8로 재설정한다. Windows의 기본 콘솔 인코딩(이 PC는 cp949)
    에서는 이슈 본문에 섞인 이모지를 로그로 출력하는 순간 `UnicodeEncodeError`가
    나는데, 하필 "깨진 데이터를 로그로 남기는" 실패 경로에서 터진다.
    docs/findings.md 함정 6 참고.

    Args:
        level: 로그 레벨 이름. `app.config.Settings.log_level`이 이미 검증한 값을
            넘기는 것을 전제로 한다.
    """
    global _configured

    for stream in (sys.stdout, sys.stderr):
        # 파이프로 리다이렉트된 경우 등 reconfigure가 없는 스트림도 있다.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")

    if _configured:
        logging.getLogger().setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """모듈용 로거를 돌려준다.

    Args:
        name: 보통 호출하는 모듈의 `__name__`.

    Returns:
        해당 이름의 로거.
    """
    return logging.getLogger(name)
