#!/usr/bin/env python3
"""
peni.sh - Dynamic SSID/Password Generator and Random Image Server
A FastAPI application that generates memorable SSID/password pairs and serves random images.
"""

import os
import random
import asyncio
from pathlib import Path
from typing import Optional, List, Tuple
import mimetypes
import logging
import json
import hashlib
import secrets
import aiosqlite
from datetime import datetime
from io import BytesIO
import imghdr

from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Depends, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from PIL import Image, ExifTags

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Configuration
class Config:
    IMAGE_DIR = os.getenv("IMAGE_DIR", "/var/www/peni.sh/images")
    THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "/var/www/peni.sh/images/thumbnails")
    DB_PATH = os.getenv("DB_PATH", "/var/www/peni.sh/images.db")
    ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    MAX_CACHE_SIZE = 1000
    MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024  # Default 10MB
    THUMBNAIL_SIZE = (300, 300)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")
    # Security: Upload authentication token
    UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", secrets.token_urlsafe(32))  # Generate random token if not set


config = Config()

# Validate OpenAI configuration
if not config.OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY environment variable is required")
    raise ValueError("OPENAI_API_KEY environment variable is required")

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

# Log upload token for initial setup
if not os.getenv("UPLOAD_TOKEN"):
    logger.warning("=" * 80)
    logger.warning("UPLOAD_TOKEN not set! Generated random token:")
    logger.warning(f"Token: {config.UPLOAD_TOKEN}")
    logger.warning("Add this to your .env file to persist it:")
    logger.warning(f"UPLOAD_TOKEN={config.UPLOAD_TOKEN}")
    logger.warning("=" * 80)


# Pydantic models
class SSIDPasswordPair(BaseModel):
    ssid: str = Field(..., description="Generated SSID")
    password: str = Field(..., description="Memorable password based on SSID")
    hint: Optional[str] = Field(None, description="Optional hint for the password")


class ImageInfo(BaseModel):
    filename: str
    path: str
    size_bytes: int
    upload_date: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_thumbnail: bool = False


class ImageUploadResponse(BaseModel):
    filename: str
    size_bytes: int
    thumbnail_created: bool
    message: str


class ImageDeleteResponse(BaseModel):
    filename: str
    deleted: bool
    message: str


# FastAPI app initialization
app = FastAPI(
    title="peni.sh",
    description="Dynamic SSID/Password Generator and Random Image Server",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://peni.sh"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# Database initialization and management
async def init_db():
    """Initialize the SQLite database for image metadata."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                original_filename TEXT NOT NULL,
                upload_date TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                has_thumbnail INTEGER DEFAULT 0,
                file_hash TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_filename ON images(filename)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_upload_date ON images(upload_date)
        """)
        await db.commit()
    logger.info(f"Database initialized at {config.DB_PATH}")


@app.on_event("startup")
async def startup_event():
    """Run on application startup."""
    # Ensure directories exist
    Path(config.IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.THUMBNAIL_DIR).mkdir(parents=True, exist_ok=True)

    # Initialize database
    await init_db()

    logger.info("Application startup complete")


# Authentication dependency
async def verify_upload_token(authorization: Optional[str] = Header(None)):
    """Verify the upload token for authentication."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Support both "Bearer <token>" and just "<token>"
    token = authorization.replace("Bearer ", "").strip()

    if token != config.UPLOAD_TOKEN:
        logger.warning(f"Invalid upload token attempt")
        raise HTTPException(status_code=403, detail="Invalid upload token")

    return True


# File validation functions
def validate_image_file(file_content: bytes, filename: str) -> Tuple[str, str]:
    """
    Validate image file using magic bytes (not just extension).
    Returns (mime_type, extension) or raises HTTPException.
    """
    # Detect actual file type using imghdr
    image_type = imghdr.what(None, h=file_content)

    if not image_type:
        raise HTTPException(status_code=400, detail="File is not a valid image")

    # Map imghdr types to extensions and MIME types
    type_map = {
        'jpeg': ('.jpg', 'image/jpeg'),
        'png': ('.png', 'image/png'),
        'gif': ('.gif', 'image/gif'),
        'webp': ('.webp', 'image/webp'),
    }

    if image_type not in type_map:
        raise HTTPException(
            status_code=400,
            detail=f"Image type '{image_type}' not supported. Allowed: jpeg, png, gif, webp"
        )

    extension, mime_type = type_map[image_type]

    return mime_type, extension


def strip_exif(image: Image.Image) -> Image.Image:
    """Remove EXIF data from image for privacy."""
    try:
        # Create a new image without EXIF data
        data = list(image.getdata())
        image_without_exif = Image.new(image.mode, image.size)
        image_without_exif.putdata(data)
        return image_without_exif
    except Exception as e:
        logger.warning(f"Failed to strip EXIF data: {e}")
        return image


async def create_thumbnail(image_path: Path, thumbnail_path: Path) -> bool:
    """Create a thumbnail for the image."""
    try:
        with Image.open(image_path) as img:
            # Strip EXIF from thumbnail too
            img = strip_exif(img)

            # Create thumbnail maintaining aspect ratio
            img.thumbnail(config.THUMBNAIL_SIZE, Image.Resampling.LANCZOS)

            # Save thumbnail
            img.save(thumbnail_path, optimize=True, quality=85)

        logger.info(f"Thumbnail created: {thumbnail_path.name}")
        return True
    except Exception as e:
        logger.error(f"Failed to create thumbnail: {e}")
        return False


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and other issues."""
    # Get just the filename without any path components
    filename = Path(filename).name

    # Remove or replace dangerous characters
    dangerous_chars = ['..', '/', '\\', '\0', '\n', '\r']
    for char in dangerous_chars:
        filename = filename.replace(char, '_')

    # Limit length
    name, ext = os.path.splitext(filename)
    if len(name) > 200:
        name = name[:200]

    return name + ext


async def get_image_dimensions(file_content: bytes) -> Tuple[int, int]:
    """Get image dimensions from file content."""
    try:
        with Image.open(BytesIO(file_content)) as img:
            return img.size
    except Exception:
        return (0, 0)


# Image cache for performance
class ImageCache:
    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._images: List[Path] = []
        self._last_scan = 0
        self._scan_interval = 300  # 5 minutes

    async def get_images(self) -> List[Path]:
        """Get list of available images, refreshing cache if needed."""
        now = asyncio.get_event_loop().time()
        if now - self._last_scan > self._scan_interval or not self._images:
            await self._refresh_cache()
        return self._images

    async def _refresh_cache(self):
        """Refresh the image cache by scanning the image directory and database."""
        try:
            image_dir = Path(config.IMAGE_DIR)
            if not image_dir.exists():
                logger.warning(f"Image directory {config.IMAGE_DIR} does not exist")
                self._images = []
                return

            # Get images from filesystem
            filesystem_images = set()
            for file_path in image_dir.rglob("*"):
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in config.ALLOWED_IMAGE_TYPES
                    and "thumbnails" not in str(file_path)  # Exclude thumbnail directory
                ):
                    filesystem_images.add(file_path.name)

            # Sync with database: add missing files to DB
            async with aiosqlite.connect(config.DB_PATH) as db:
                # Get images already in database
                async with db.execute("SELECT filename FROM images") as cursor:
                    db_images = {row[0] async for row in cursor}

                # Add new images to database
                for filename in filesystem_images - db_images:
                    file_path = image_dir / filename
                    try:
                        stat = file_path.stat()
                        mime_type, _ = mimetypes.guess_type(str(file_path))
                        await db.execute(
                            """INSERT INTO images
                               (filename, original_filename, upload_date, size_bytes, mime_type)
                               VALUES (?, ?, ?, ?, ?)""",
                            (filename, filename, datetime.now().isoformat(),
                             stat.st_size, mime_type or 'application/octet-stream')
                        )
                        logger.info(f"Added existing image to database: {filename}")
                    except Exception as e:
                        logger.warning(f"Could not add {filename} to database: {e}")

                await db.commit()

            # Build cache from filesystem
            images = [image_dir / filename for filename in filesystem_images]
            self._images = images[: self.max_size]
            self._last_scan = asyncio.get_event_loop().time()
            logger.info(f"Refreshed image cache: {len(self._images)} images found")

        except Exception as e:
            logger.error(f"Error refreshing image cache: {e}")
            self._images = []

    async def invalidate(self):
        """Force cache refresh on next request."""
        self._last_scan = 0


image_cache = ImageCache(config.MAX_CACHE_SIZE)


# AI-powered SSID/Password generation
async def generate_ssid_password() -> SSIDPasswordPair:
    """Generate a memorable SSID and password pair using OpenAI."""
    try:
        prompt = """Generate a WiFi network SSID and password pair where:

1. The SSID should be creative, memorable, and somewhat quirky (like 'QuantumCoffeehouse' or 'NeonDreams42')
2. The password should be easy to guess/remember if you know the SSID, using a simple, consistent rule
3. The password should be reasonably secure (8+ characters)
4. Provide a hint that explains how to derive the password from the SSID

Examples of good patterns:
- SSID: "CosmicPizza88" → Password: "CP88!" (first letters + numbers + symbol)
- SSID: "NightOwlCafe" → Password: "nocafe" (first letters of each word + last word)
- SSID: "RetroWave2024" → Password: "retro2024" (first word + numbers)

Respond with ONLY a JSON object in this exact format:
{
  "ssid": "your_creative_ssid",
  "password": "derived_password",
  "hint": "explanation of how password relates to ssid"
}

Generate a new, unique combination now."""

        response = await openai_client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a creative WiFi network name generator. Generate memorable SSID/password pairs with simple derivation rules.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.9,  # High creativity
        )

        # Parse the JSON response
        content = response.choices[0].message.content.strip()

        # Clean up any markdown formatting
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
            return SSIDPasswordPair(
                ssid=data["ssid"],
                password=data["password"],
                hint=data.get("hint", "Derive password from SSID using the pattern"),
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenAI JSON response: {content}")
            raise HTTPException(
                status_code=502, detail="Invalid response from AI service"
            )

    except Exception as e:
        logger.error(f"Error generating SSID/password with OpenAI: {e}")

        # Fallback to a simple deterministic method if OpenAI fails
        fallback_number = random.randint(1000, 9999)
        fallback_ssid = f"NetworkDown{fallback_number}"
        fallback_password = f"nd{fallback_number}"

        return SSIDPasswordPair(
            ssid=fallback_ssid,
            password=fallback_password,
            hint="Fallback mode: 'nd' + the number from SSID",
        )


# API Routes

@app.post("/api/upload", response_model=ImageUploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    authenticated: bool = Depends(verify_upload_token)
):
    """Upload a new image with authentication, validation, and thumbnail generation."""
    try:
        # Read file content
        file_content = await file.read()

        # Validate file size
        if len(file_content) > config.MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size: {config.MAX_UPLOAD_SIZE // 1024 // 1024}MB"
            )

        # Validate it's actually an image using magic bytes
        mime_type, correct_ext = validate_image_file(file_content, file.filename)

        # Sanitize and generate unique filename
        original_filename = sanitize_filename(file.filename)
        name, _ = os.path.splitext(original_filename)

        # Check if file already exists, add counter if needed
        filename = original_filename
        counter = 1
        while (Path(config.IMAGE_DIR) / filename).exists():
            filename = f"{name}_{counter}{correct_ext}"
            counter += 1

        # Calculate file hash for deduplication
        file_hash = hashlib.sha256(file_content).hexdigest()

        # Check if this exact file already exists
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT filename FROM images WHERE file_hash = ?", (file_hash,)
            ) as cursor:
                existing = await cursor.fetchone()
                if existing:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Identical image already exists: {existing[0]}"
                    )

        # Get image dimensions
        width, height = await get_image_dimensions(file_content)

        # Strip EXIF data and save
        image_path = Path(config.IMAGE_DIR) / filename
        try:
            with Image.open(BytesIO(file_content)) as img:
                # Strip EXIF
                img_clean = strip_exif(img)

                # Save with optimization
                img_clean.save(image_path, optimize=True, quality=95)

            logger.info(f"Image saved: {filename} ({len(file_content)} bytes)")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to process image: {str(e)}")

        # Create thumbnail
        thumbnail_path = Path(config.THUMBNAIL_DIR) / filename
        thumbnail_created = await create_thumbnail(image_path, thumbnail_path)

        # Save metadata to database
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                """INSERT INTO images
                   (filename, original_filename, upload_date, size_bytes, mime_type,
                    width, height, has_thumbnail, file_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filename,
                    original_filename,
                    datetime.now().isoformat(),
                    image_path.stat().st_size,
                    mime_type,
                    width,
                    height,
                    1 if thumbnail_created else 0,
                    file_hash
                )
            )
            await db.commit()

        # Invalidate cache to include new image
        await image_cache.invalidate()

        return ImageUploadResponse(
            filename=filename,
            size_bytes=image_path.stat().st_size,
            thumbnail_created=thumbnail_created,
            message=f"Image uploaded successfully: {filename}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading image: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@app.delete("/api/images/{filename}", response_model=ImageDeleteResponse)
async def delete_image(
    filename: str,
    authenticated: bool = Depends(verify_upload_token)
):
    """Delete an image with authentication."""
    try:
        # Sanitize filename for security
        filename = sanitize_filename(filename)
        image_path = Path(config.IMAGE_DIR) / filename
        thumbnail_path = Path(config.THUMBNAIL_DIR) / filename

        # Security check
        if not image_path.resolve().is_relative_to(Path(config.IMAGE_DIR).resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        if not image_path.exists():
            raise HTTPException(status_code=404, detail="Image not found")

        # Delete from filesystem
        image_path.unlink()
        logger.info(f"Deleted image: {filename}")

        # Delete thumbnail if exists
        if thumbnail_path.exists():
            thumbnail_path.unlink()
            logger.info(f"Deleted thumbnail: {filename}")

        # Delete from database
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("DELETE FROM images WHERE filename = ?", (filename,))
            await db.commit()

        # Invalidate cache
        await image_cache.invalidate()

        return ImageDeleteResponse(
            filename=filename,
            deleted=True,
            message=f"Image deleted successfully: {filename}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting image {filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")


@app.get("/", response_class=HTMLResponse)
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
                        <a href="/admin">Admin</a>
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


@app.get("/api/wifi", response_model=SSIDPasswordPair)
async def get_wifi_credentials():
    """Generate and return WiFi SSID and password."""
    return await generate_ssid_password()


@app.get("/image/{filename}")
async def get_image(filename: str):
    """Serve a specific image file."""
    try:
        image_path = Path(config.IMAGE_DIR) / filename

        # Security check: ensure the file is within the image directory
        if not image_path.resolve().is_relative_to(Path(config.IMAGE_DIR).resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        if not image_path.exists() or not image_path.is_file():
            raise HTTPException(status_code=404, detail="Image not found")

        # Check if it's an allowed image type
        if image_path.suffix.lower() not in config.ALLOWED_IMAGE_TYPES:
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


@app.get("/api/images", response_model=List[ImageInfo])
async def list_images():
    """List available images with metadata from database."""
    try:
        # Ensure cache is fresh
        await image_cache.get_images()

        image_info = []

        # Get metadata from database
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                """SELECT filename, size_bytes, upload_date, width, height, has_thumbnail
                   FROM images
                   ORDER BY upload_date DESC"""
            ) as cursor:
                async for row in cursor:
                    filename, size_bytes, upload_date, width, height, has_thumbnail = row
                    image_info.append(
                        ImageInfo(
                            filename=filename,
                            path=filename,
                            size_bytes=size_bytes,
                            upload_date=upload_date,
                            width=width,
                            height=height,
                            has_thumbnail=bool(has_thumbnail)
                        )
                    )

        return image_info

    except Exception as e:
        logger.error(f"Error listing images: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/thumbnail/{filename}")
async def get_thumbnail(filename: str):
    """Serve a thumbnail image."""
    try:
        filename = sanitize_filename(filename)
        thumbnail_path = Path(config.THUMBNAIL_DIR) / filename

        # Security check
        if not thumbnail_path.resolve().is_relative_to(Path(config.THUMBNAIL_DIR).resolve()):
            raise HTTPException(status_code=403, detail="Access denied")

        if not thumbnail_path.exists():
            # If thumbnail doesn't exist, try to create it
            image_path = Path(config.IMAGE_DIR) / filename
            if image_path.exists():
                await create_thumbnail(image_path, thumbnail_path)
                if not thumbnail_path.exists():
                    raise HTTPException(status_code=404, detail="Thumbnail not found and could not be created")
            else:
                raise HTTPException(status_code=404, detail="Thumbnail not found")

        mime_type, _ = mimetypes.guess_type(str(thumbnail_path))
        if not mime_type:
            mime_type = "application/octet-stream"

        return FileResponse(path=thumbnail_path, media_type=mime_type, filename=f"thumb_{filename}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving thumbnail {filename}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Admin interface for uploading and managing images."""
    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
    <title>peni.sh - Admin</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 20px;
            font-family: 'Courier New', monospace;
            background: #000;
            color: #0f0;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        h1 {
            text-align: center;
            border-bottom: 2px solid #0f0;
            padding-bottom: 10px;
        }
        .section {
            margin: 30px 0;
            padding: 20px;
            border: 1px solid #0f0;
        }
        .upload-zone {
            border: 2px dashed #0f0;
            padding: 40px;
            text-align: center;
            cursor: pointer;
            transition: background 0.3s;
        }
        .upload-zone:hover, .upload-zone.drag-over {
            background: #0f0;
            color: #000;
        }
        input[type="text"], input[type="file"] {
            width: 100%;
            padding: 10px;
            margin: 10px 0;
            background: #000;
            color: #0f0;
            border: 1px solid #0f0;
            font-family: 'Courier New', monospace;
        }
        button {
            background: #000;
            color: #0f0;
            border: 1px solid #0f0;
            padding: 10px 20px;
            cursor: pointer;
            font-family: 'Courier New', monospace;
            margin: 5px;
        }
        button:hover {
            background: #0f0;
            color: #000;
        }
        .image-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .image-card {
            border: 1px solid #0f0;
            padding: 10px;
        }
        .image-card img {
            width: 100%;
            height: 200px;
            object-fit: cover;
            border: 1px solid #0f0;
        }
        .image-info {
            font-size: 12px;
            margin: 5px 0;
        }
        .message {
            padding: 10px;
            margin: 10px 0;
            border: 1px solid #0f0;
            display: none;
        }
        .message.error {
            border-color: #f00;
            color: #f00;
        }
        .message.success {
            border-color: #0f0;
            color: #0f0;
        }
        .progress {
            width: 100%;
            height: 20px;
            background: #000;
            border: 1px solid #0f0;
            margin: 10px 0;
            display: none;
        }
        .progress-bar {
            height: 100%;
            background: #0f0;
            width: 0%;
            transition: width 0.3s;
        }
        a {
            color: #0f0;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
        }
        .stat {
            border: 1px solid #0f0;
            padding: 15px;
            text-align: center;
        }
        .stat-value {
            font-size: 24px;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>peni.sh - Admin Panel</h1>

        <div class="section">
            <h2>Authentication</h2>
            <input type="text" id="token" placeholder="Enter upload token (required for all operations)">
            <button onclick="saveToken()">Save Token</button>
            <div id="token-status" class="message"></div>
        </div>

        <div class="section">
            <h2>Statistics</h2>
            <div class="stats">
                <div class="stat">
                    <div class="stat-value" id="total-images">-</div>
                    <div>Total Images</div>
                </div>
                <div class="stat">
                    <div class="stat-value" id="total-size">-</div>
                    <div>Total Size</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2>Upload Images</h2>
            <div class="upload-zone" id="upload-zone">
                <p>Drag & drop images here or click to select</p>
                <p>Supported: JPEG, PNG, GIF, WebP | Max size: 10MB</p>
            </div>
            <input type="file" id="file-input" accept="image/*" multiple style="display: none;">
            <div class="progress" id="progress">
                <div class="progress-bar" id="progress-bar"></div>
            </div>
            <div id="upload-message" class="message"></div>
        </div>

        <div class="section">
            <h2>Manage Images</h2>
            <button onclick="loadImages()">Refresh List</button>
            <div class="image-grid" id="image-grid"></div>
        </div>

        <div class="section">
            <p><a href="/">← Back to main site</a> | <a href="/api/docs">API Documentation</a></p>
        </div>
    </div>

    <script>
        let authToken = localStorage.getItem('uploadToken') || '';
        if (authToken) {
            document.getElementById('token').value = authToken;
            document.getElementById('token-status').textContent = 'Token loaded from storage';
            document.getElementById('token-status').className = 'message success';
            document.getElementById('token-status').style.display = 'block';
        }

        function saveToken() {
            authToken = document.getElementById('token').value.trim();
            if (authToken) {
                localStorage.setItem('uploadToken', authToken);
                showMessage('token-status', 'Token saved successfully', 'success');
                loadImages();
            } else {
                showMessage('token-status', 'Please enter a valid token', 'error');
            }
        }

        function showMessage(elementId, text, type) {
            const el = document.getElementById(elementId);
            el.textContent = text;
            el.className = `message ${type}`;
            el.style.display = 'block';
            setTimeout(() => { el.style.display = 'none'; }, 5000);
        }

        function formatBytes(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(2) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
        }

        async function loadImages() {
            try {
                const response = await fetch('/api/images');
                if (!response.ok) throw new Error('Failed to load images');

                const images = await response.json();
                const grid = document.getElementById('image-grid');

                if (images.length === 0) {
                    grid.innerHTML = '<p>No images uploaded yet</p>';
                } else {
                    grid.innerHTML = images.map(img => `
                        <div class="image-card">
                            <img src="${img.has_thumbnail ? '/thumbnail/' + img.filename : '/image/' + img.filename}"
                                 alt="${img.filename}"
                                 onclick="window.open('/image/${img.filename}', '_blank')">
                            <div class="image-info">
                                <strong>${img.filename}</strong><br>
                                Size: ${formatBytes(img.size_bytes)}<br>
                                ${img.width && img.height ? `Dimensions: ${img.width}x${img.height}<br>` : ''}
                                ${img.upload_date ? `Uploaded: ${new Date(img.upload_date).toLocaleDateString()}<br>` : ''}
                                <button onclick="deleteImage('${img.filename}')">Delete</button>
                            </div>
                        </div>
                    `).join('');
                }

                // Update stats
                document.getElementById('total-images').textContent = images.length;
                const totalSize = images.reduce((sum, img) => sum + img.size_bytes, 0);
                document.getElementById('total-size').textContent = formatBytes(totalSize);

            } catch (error) {
                console.error('Error loading images:', error);
                document.getElementById('image-grid').innerHTML = '<p style="color: #f00;">Error loading images</p>';
            }
        }

        async function uploadFile(file) {
            if (!authToken) {
                showMessage('upload-message', 'Please enter and save your upload token first', 'error');
                return;
            }

            const formData = new FormData();
            formData.append('file', file);

            try {
                document.getElementById('progress').style.display = 'block';
                document.getElementById('progress-bar').style.width = '50%';

                const response = await fetch('/api/upload', {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${authToken}`
                    },
                    body: formData
                });

                document.getElementById('progress-bar').style.width = '100%';

                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Upload failed');
                }

                const result = await response.json();
                showMessage('upload-message', result.message, 'success');
                loadImages();

            } catch (error) {
                showMessage('upload-message', 'Upload failed: ' + error.message, 'error');
            } finally {
                setTimeout(() => {
                    document.getElementById('progress').style.display = 'none';
                    document.getElementById('progress-bar').style.width = '0%';
                }, 1000);
            }
        }

        async function deleteImage(filename) {
            if (!authToken) {
                alert('Please enter and save your upload token first');
                return;
            }

            if (!confirm(`Delete ${filename}?`)) return;

            try {
                const response = await fetch(`/api/images/${filename}`, {
                    method: 'DELETE',
                    headers: {
                        'Authorization': `Bearer ${authToken}`
                    }
                });

                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Delete failed');
                }

                const result = await response.json();
                showMessage('upload-message', result.message, 'success');
                loadImages();

            } catch (error) {
                showMessage('upload-message', 'Delete failed: ' + error.message, 'error');
            }
        }

        // Drag and drop functionality
        const uploadZone = document.getElementById('upload-zone');
        const fileInput = document.getElementById('file-input');

        uploadZone.addEventListener('click', () => fileInput.click());

        uploadZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadZone.classList.add('drag-over');
        });

        uploadZone.addEventListener('dragleave', () => {
            uploadZone.classList.remove('drag-over');
        });

        uploadZone.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadZone.classList.remove('drag-over');

            const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
            files.forEach(file => uploadFile(file));
        });

        fileInput.addEventListener('change', (e) => {
            Array.from(e.target.files).forEach(file => uploadFile(file));
            e.target.value = '';
        });

        // Load images on page load
        loadImages();
    </script>
</body>
</html>
    """)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    images = await image_cache.get_images()
    return {
        "status": "healthy",
        "image_count": len(images),
        "image_dir": config.IMAGE_DIR,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_level="info")
