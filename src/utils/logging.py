import logging
import os
import re
import sys
from typing import Optional

class SecretMaskingFormatter(logging.Formatter):
    """Formatter that masks potential API keys and secrets from log records."""

    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.secret_patterns = [
            re.compile(r"(x-api-key['\":\s=]+)([a-zA-Z0-9_\-]+)", re.IGNORECASE),
            re.compile(r"(api_key['\":\s=]+)([a-zA-Z0-9_\-]+)", re.IGNORECASE),
            re.compile(r"(authorization['\":\s=]+bearer\s+)([a-zA-Z0-9_\.\-]+)", re.IGNORECASE),
        ]

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        masked = original
        for pattern in self.secret_patterns:
            masked = pattern.sub(r"\1***MASKED***", masked)
        return masked

def setup_logger(name: str = "nft_bot", level: str = "INFO") -> logging.Logger:
    """Configures and returns a logger instance with secret masking."""
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(numeric_level)
        formatter = SecretMaskingFormatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
