"""Application configuration."""

import os
from pathlib import Path
from typing import Set
import logging

logger = logging.getLogger(__name__)


class Config:
    """Application configuration settings."""

    # Project root
    PROJECT_ROOT = Path(__file__).parent.parent

    # Image configuration
    IMAGE_DIR = os.getenv("IMAGE_DIR", str(PROJECT_ROOT / "images"))
    ALLOWED_IMAGE_TYPES: Set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    MAX_CACHE_SIZE = int(os.getenv("MAX_CACHE_SIZE", "1000"))

    # OpenAI configuration
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")

    # Server configuration
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    RELOAD = os.getenv("RELOAD", "True").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "info")

    # CORS configuration
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://peni.sh").split(",")

    @classmethod
    def validate(cls) -> None:
        """Validate required configuration."""
        if not cls.OPENAI_API_KEY:
            logger.error("OPENAI_API_KEY environment variable is required")
            raise ValueError("OPENAI_API_KEY environment variable is required")

        # Ensure image directory exists
        image_dir = Path(cls.IMAGE_DIR)
        if not image_dir.exists():
            logger.warning(f"Image directory {cls.IMAGE_DIR} does not exist, creating it")
            image_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Configuration loaded successfully")
        logger.info(f"Image directory: {cls.IMAGE_DIR}")
        logger.info(f"OpenAI model: {cls.OPENAI_MODEL}")


# Validate configuration on import
Config.validate()
