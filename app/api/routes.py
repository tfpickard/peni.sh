"""API route definitions."""

import random
import mimetypes
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app import __version__
from app.config import Config
from app.models import SSIDPasswordPair, ImageInfo, HealthResponse
from app.core.cache import image_cache
from app.services.wifi_generator import generate_ssid_password
from app.services.image_manager import image_manager

logger = logging.getLogger(__name__)

# Create API router
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def root():
    """Serve random image with basic HTML wrapper."""
    try:
        images = await image_cache.get_images()
        if not images:
            return HTMLResponse(
                """
                <html>
                    <head><title>peni.sh</title></head>
                    <body>
                        <h1>peni.sh</h1>
                        <p>No images available</p>
                        <p>Add images to the images/ directory to get started!</p>
                        <a href="/api/wifi">Generate WiFi Credentials</a>
                    </body>
                </html>
            """
            )

        random_image = random.choice(images)
        image_url = f"/image/{random_image.name}"

        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html>
                <head>
                    <title>peni.sh</title>
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <style>
                        body {{
                            margin: 0;
                            padding: 20px;
                            font-family: 'Courier New', monospace;
                            background: #000;
                            color: #0f0;
                            display: flex;
                            flex-direction: column;
                            align-items: center;
                            min-height: 100vh;
                        }}
                        img {{
                            max-width: 90vw;
                            max-height: 80vh;
                            object-fit: contain;
                            border: 1px solid #0f0;
                        }}
                        .nav {{
                            margin: 20px 0;
                        }}
                        a {{
                            color: #0f0;
                            text-decoration: none;
                            margin: 0 10px;
                            border: 1px solid #0f0;
                            padding: 5px 10px;
                        }}
                        a:hover {{
                            background: #0f0;
                            color: #000;
                        }}
                    </style>
                </head>
                <body>
                    <h1>peni.sh</h1>
                    <div class="nav">
                        <a href="/">Random Image</a>
                        <a href="/api/wifi">WiFi Credentials</a>
                        <a href="/api/docs">API Docs</a>
                    </div>
                    <img src="{image_url}" alt="Random image from peni.sh" />
                    <p>File: {random_image.name}</p>
                </body>
            </html>
        """
        )

    except Exception as e:
        logger.error(f"Error serving root page: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/wifi", response_model=SSIDPasswordPair)
async def get_wifi_credentials():
    """Generate and return WiFi SSID and password."""
    return await generate_ssid_password()


@router.get("/image/{filename}")
async def get_image(filename: str):
    """Serve a specific image file.

    Args:
        filename: Name of the image file

    Returns:
        FileResponse with the image

    Raises:
        HTTPException: If image not found or access denied
    """
    try:
        # Try to find the image in any subdirectory
        image_dir = Path(Config.IMAGE_DIR)
        image_path = None

        # First try direct path
        direct_path = image_dir / filename
        if direct_path.exists() and direct_path.is_file():
            image_path = direct_path
        else:
            # Search in subdirectories
            for file_path in image_dir.rglob(filename):
                if file_path.is_file():
                    image_path = file_path
                    break

        if not image_path:
            raise HTTPException(status_code=404, detail="Image not found")

        # Security check: ensure the file is within the image directory
        if not image_path.resolve().is_relative_to(image_dir.resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        # Check if it's an allowed image type
        if image_path.suffix.lower() not in Config.ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="Invalid image type")

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        return FileResponse(path=image_path, media_type=mime_type, filename=filename)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving image {filename}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/images", response_model=List[ImageInfo])
async def list_images():
    """List available images with metadata.

    Returns:
        List of ImageInfo objects
    """
    try:
        images = await image_cache.get_images()
        image_info = []

        for image_path in images:
            try:
                info = image_manager.get_image_info(image_path)
                image_info.append(info)
            except Exception as e:
                logger.warning(f"Could not get info for {image_path}: {e}")

        return image_info

    except Exception as e:
        logger.error(f"Error listing images: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/categories")
async def list_categories():
    """List available image categories.

    Returns:
        Dict with list of categories
    """
    try:
        categories = image_manager.list_categories()
        return {"categories": categories}
    except Exception as e:
        logger.error(f"Error listing categories: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint.

    Returns:
        HealthResponse with system status
    """
    images = await image_cache.get_images()
    return HealthResponse(
        status="healthy",
        image_count=len(images),
        image_dir=Config.IMAGE_DIR,
        version=__version__,
    )
