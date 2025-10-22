"""Image cache management."""

import asyncio
import logging
from pathlib import Path
from typing import List

from app.config import Config

logger = logging.getLogger(__name__)


class ImageCache:
    """Cache for image file paths with automatic refresh."""

    def __init__(self, max_size: int = None):
        """Initialize the image cache.

        Args:
            max_size: Maximum number of images to cache (default from config)
        """
        self.max_size = max_size or Config.MAX_CACHE_SIZE
        self._images: List[Path] = []
        self._last_scan = 0
        self._scan_interval = 300  # 5 minutes

    async def get_images(self) -> List[Path]:
        """Get list of available images, refreshing cache if needed.

        Returns:
            List of Path objects for available images
        """
        now = asyncio.get_event_loop().time()
        if now - self._last_scan > self._scan_interval or not self._images:
            await self._refresh_cache()
        return self._images

    async def _refresh_cache(self):
        """Refresh the image cache by scanning the image directory."""
        try:
            image_dir = Path(Config.IMAGE_DIR)
            if not image_dir.exists():
                logger.warning(f"Image directory {Config.IMAGE_DIR} does not exist")
                self._images = []
                return

            images = []
            for file_path in image_dir.rglob("*"):
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in Config.ALLOWED_IMAGE_TYPES
                ):
                    images.append(file_path)

            self._images = images[: self.max_size]
            self._last_scan = asyncio.get_event_loop().time()
            logger.info(f"Refreshed image cache: {len(self._images)} images found")

        except Exception as e:
            logger.error(f"Error refreshing image cache: {e}")
            self._images = []

    async def force_refresh(self):
        """Force an immediate cache refresh."""
        await self._refresh_cache()


# Global cache instance
image_cache = ImageCache()
