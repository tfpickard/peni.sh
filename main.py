#!/usr/bin/env python3
"""
peni.sh - Dynamic SSID/Password Generator and Random Image Server
A FastAPI application that generates memorable SSID/password pairs and serves random images.
"""

import os
import random
import asyncio
from pathlib import Path
from typing import Optional, List
import mimetypes
import logging
import json

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Configuration
class Config:
    IMAGE_DIR = os.getenv("IMAGE_DIR", "/var/www/peni.sh/images")
    ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    MAX_CACHE_SIZE = 1000
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")


config = Config()

# Validate OpenAI configuration
if not config.OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY environment variable is required")
    raise ValueError("OPENAI_API_KEY environment variable is required")

# Initialize OpenAI client
openai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


# Pydantic models
class SSIDPasswordPair(BaseModel):
    ssid: str = Field(..., description="Generated SSID")
    password: str = Field(..., description="Memorable password based on SSID")
    hint: Optional[str] = Field(None, description="Optional hint for the password")


class ImageInfo(BaseModel):
    filename: str
    path: str
    size_bytes: int


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
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
        """Refresh the image cache by scanning the image directory."""
        try:
            image_dir = Path(config.IMAGE_DIR)
            if not image_dir.exists():
                logger.warning(f"Image directory {config.IMAGE_DIR} does not exist")
                self._images = []
                return

            images = []
            for file_path in image_dir.rglob("*"):
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in config.ALLOWED_IMAGE_TYPES
                ):
                    images.append(file_path)

            self._images = images[: self.max_size]
            self._last_scan = asyncio.get_event_loop().time()
            logger.info(f"Refreshed image cache: {len(self._images)} images found")

        except Exception as e:
            logger.error(f"Error refreshing image cache: {e}")
            self._images = []


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
    """List available images."""
    try:
        images = await image_cache.get_images()
        image_info = []

        for image_path in images:
            try:
                stat = image_path.stat()
                image_info.append(
                    ImageInfo(
                        filename=image_path.name,
                        path=str(image_path.relative_to(config.IMAGE_DIR)),
                        size_bytes=stat.st_size,
                    )
                )
            except Exception as e:
                logger.warning(f"Could not get info for {image_path}: {e}")

        return image_info

    except Exception as e:
        logger.error(f"Error listing images: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
