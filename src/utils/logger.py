import logging
import colorlog
from logging.handlers import RotatingFileHandler
import yaml
import os


def setup_logger(name: str = "stock_trader") -> logging.Logger:
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["logging"]

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, cfg["level"]))

    # 컬러 콘솔 핸들러
    console = colorlog.StreamHandler()
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        }
    ))

    # 파일 핸들러
    os.makedirs("logs", exist_ok=True)
    file_handler = RotatingFileHandler(
        cfg["file"],
        maxBytes=cfg["max_bytes"],
        backupCount=cfg["backup_count"],
        encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
