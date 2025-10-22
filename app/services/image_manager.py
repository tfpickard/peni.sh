"""Image management service with metadata support."""

import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

from app.config import Config
from app.models import ImageInfo, ImageMetadata

logger = logging.getLogger(__name__)


class ImageManager:
    """Manages images and their metadata."""

    def __init__(self):
        """Initialize the image manager."""
        self.image_dir = Path(Config.IMAGE_DIR)
        self.metadata_file = self.image_dir / "metadata.json"
        self._metadata_cache: Dict[str, ImageMetadata] = {}
        self._load_metadata()

    def _load_metadata(self):
        """Load image metadata from JSON file."""
        try:
            if self.metadata_file.exists():
                with open(self.metadata_file, "r") as f:
                    data = json.load(f)
                    self._metadata_cache = {
                        k: ImageMetadata(**v) for k, v in data.items()
                    }
                logger.info(f"Loaded metadata for {len(self._metadata_cache)} images")
            else:
                logger.info("No metadata file found, starting fresh")
                self._metadata_cache = {}
        except Exception as e:
            logger.error(f"Error loading metadata: {e}")
            self._metadata_cache = {}

    def _save_metadata(self):
        """Save image metadata to JSON file."""
        try:
            data = {k: v.dict() for k, v in self._metadata_cache.items()}
            with open(self.metadata_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved metadata for {len(self._metadata_cache)} images")
        except Exception as e:
            logger.error(f"Error saving metadata: {e}")

    def get_image_info(self, image_path: Path) -> ImageInfo:
        """Get information about an image including metadata.

        Args:
            image_path: Path to the image file

        Returns:
            ImageInfo with file details and metadata
        """
        stat = image_path.stat()
        relative_path = str(image_path.relative_to(self.image_dir))

        # Get metadata if available
        metadata = self._metadata_cache.get(relative_path)

        # Determine category from path or metadata
        category = None
        if metadata and metadata.category:
            category = metadata.category
        elif "/" in relative_path:
            category = relative_path.split("/")[0]
        else:
            category = "default"

        tags = metadata.tags if metadata else []

        return ImageInfo(
            filename=image_path.name,
            path=relative_path,
            size_bytes=stat.st_size,
            category=category,
            tags=tags,
        )

    def add_image_metadata(
        self,
        filename: str,
        category: str = "default",
        tags: Optional[List[str]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Add or update metadata for an image.

        Args:
            filename: Image filename or relative path
            category: Image category
            tags: List of tags
            title: Image title
            description: Image description
        """
        metadata = ImageMetadata(
            filename=filename,
            category=category,
            tags=tags or [],
            title=title,
            description=description,
        )
        self._metadata_cache[filename] = metadata
        self._save_metadata()
        logger.info(f"Added metadata for {filename}")

    def get_metadata(self, filename: str) -> Optional[ImageMetadata]:
        """Get metadata for a specific image.

        Args:
            filename: Image filename or relative path

        Returns:
            ImageMetadata if available, None otherwise
        """
        return self._metadata_cache.get(filename)

    def list_categories(self) -> List[str]:
        """List all available image categories.

        Returns:
            List of category names
        """
        categories = set()
        for metadata in self._metadata_cache.values():
            categories.add(metadata.category)

        # Also scan directories
        for item in self.image_dir.iterdir():
            if item.is_dir() and item.name != "__pycache__":
                categories.add(item.name)

        return sorted(list(categories))

    def get_images_by_category(self, category: str) -> List[Path]:
        """Get all images in a specific category.

        Args:
            category: Category name

        Returns:
            List of image paths in the category
        """
        images = []
        category_dir = self.image_dir / category

        if category_dir.exists() and category_dir.is_dir():
            for file_path in category_dir.iterdir():
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in Config.ALLOWED_IMAGE_TYPES
                ):
                    images.append(file_path)

        return images


# Global image manager instance
image_manager = ImageManager()
