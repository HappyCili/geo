from __future__ import annotations

import logging
import os
from typing import Final


APP_LOGGER_NAME: Final = "app"
CONSOLE_HANDLER_NAME: Final = "app.console"
DEFAULT_LOG_LEVEL: Final = "DEBUG"
_LOG_LEVELS: Final = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def configure_app_logging() -> None:
    """Configure project console logs without changing Uvicorn loggers."""
    logger = logging.getLogger(APP_LOGGER_NAME)
    logger.setLevel(_configured_log_level())
    logger.propagate = False

    if any(handler.get_name() == CONSOLE_HANDLER_NAME for handler in logger.handlers):
        return

    handler = logging.StreamHandler()
    handler.set_name(CONSOLE_HANDLER_NAME)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(handler)


def _configured_log_level() -> int:
    configured = os.getenv("APP_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    try:
        return _LOG_LEVELS[configured]
    except KeyError as error:
        valid_levels = ", ".join(_LOG_LEVELS)
        raise ValueError(f"APP_LOG_LEVEL must be one of: {valid_levels}") from error
