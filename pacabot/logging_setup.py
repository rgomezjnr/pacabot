import logging
import sys
from pathlib import Path
from pacabot.config import LoggingConfig

_LEVEL_MAP = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: LoggingConfig, level_override: str | None = None) -> logging.Logger:
    level_str = (level_override or cfg.log_level).lower()
    level = _LEVEL_MAP[level_str]

    log_path = Path(cfg.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger = logging.getLogger("pacabot")
    logger.setLevel(level)
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("pacabot")
