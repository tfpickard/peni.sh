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
    """Serve random image with SEO-optimized HTML wrapper."""
    try:
        images = await image_cache.get_images()
        if not images:
            return HTMLResponse(
                """
                <!DOCTYPE html>
                <html lang="en">
                    <head>
                        <meta charset="utf-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1">
                        <title>peni.sh - AI-Powered WiFi Name Generator | Creative SSID & Password Generator</title>
                        <meta name="description" content="Generate creative WiFi network names and memorable passwords using AI. Free online tool for unique SSID generation with easy-to-remember password patterns.">
                        <meta name="keywords" content="wifi name generator, ssid generator, creative wifi names, password generator, ai wifi names, network name ideas">
                        <link rel="canonical" href="https://peni.sh">
                        <meta property="og:title" content="peni.sh - AI WiFi Name Generator">
                        <meta property="og:description" content="Create amazing WiFi network names with AI-powered SSID generation">
                        <meta property="og:url" content="https://peni.sh">
                        <meta property="og:type" content="website">
                        <meta name="twitter:card" content="summary">
                        <meta name="twitter:title" content="peni.sh - AI WiFi Name Generator">
                        <meta name="twitter:description" content="Generate creative WiFi names and passwords with AI">
                    </head>
                    <body>
                        <h1>peni.sh - AI WiFi Generator</h1>
                        <p>No images available. <a href="/api/wifi">Generate WiFi Credentials</a></p>
                    </body>
                </html>
            """
            )

        random_image = random.choice(images)
        image_url = f"/image/{random_image.name}"

        return HTMLResponse(
            f"""
            <!DOCTYPE html>
            <html lang="en">
                <head>
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <title>peni.sh - AI-Powered WiFi Name Generator | Creative SSID & Password Generator</title>
                    <meta name="description" content="Generate creative WiFi network names and memorable passwords using AI. Free online tool for unique SSID generation with easy-to-remember password patterns. Try our WiFi name generator now!">
                    <meta name="keywords" content="wifi name generator, ssid generator, creative wifi names, password generator, ai wifi names, network name ideas, wifi network names, router names, creative ssid, memorable passwords">
                    <meta name="author" content="peni.sh">
                    <meta name="robots" content="index, follow">
                    <link rel="canonical" href="https://peni.sh">
                    
                    <!-- Open Graph / Facebook -->
                    <meta property="og:type" content="website">
                    <meta property="og:url" content="https://peni.sh">
                    <meta property="og:title" content="peni.sh - AI-Powered WiFi Name Generator">
                    <meta property="og:description" content="Create amazing WiFi network names with AI-powered SSID generation. Generate memorable passwords that are easy to derive from your network name.">
                    <meta property="og:image" content="https://peni.sh{image_url}">
                    <meta property="og:site_name" content="peni.sh">
                    
                    <!-- Twitter -->
                    <meta name="twitter:card" content="summary_large_image">
                    <meta name="twitter:url" content="https://peni.sh">
                    <meta name="twitter:title" content="peni.sh - AI WiFi Name Generator">
                    <meta name="twitter:description" content="Generate creative WiFi names and passwords with AI">
                    <meta name="twitter:image" content="https://peni.sh{image_url}">
                    
                    <!-- Favicon -->
                    <link rel="icon" type="image/x-icon" href="/favicon.ico">
                    
                    <!-- Schema.org structured data -->
                    <script type="application/ld+json">
                    {{
                        "@context": "https://schema.org",
                        "@type": "WebApplication",
                        "name": "peni.sh WiFi Name Generator",
                        "description": "AI-powered tool for generating creative WiFi network names (SSIDs) and memorable passwords",
                        "url": "https://peni.sh",
                        "applicationCategory": "UtilityApplication",
                        "operatingSystem": "Any",
                        "offers": {{
                            "@type": "Offer",
                            "price": "0",
                            "priceCurrency": "USD"
                        }},
                        "author": {{
                            "@type": "Organization",
                            "name": "peni.sh"
                        }},
                        "potentialAction": {{
                            "@type": "UseAction",
                            "target": {{
                                "@type": "EntryPoint",
                                "urlTemplate": "https://peni.sh/api/wifi"
                            }}
                        }}
                    }}
                    </script>
                    
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
                            line-height: 1.6;
                        }}
                        h1 {{
                            font-size: 2.5em;
                            margin-bottom: 10px;
                            text-align: center;
                        }}
                        .tagline {{
                            font-size: 1.2em;
                            text-align: center;
                            margin-bottom: 30px;
                            color: #0aa;
                        }}
                        img {{
                            max-width: 90vw;
                            max-height: 60vh;
                            object-fit: contain;
                            border: 1px solid #0f0;
                            margin: 20px 0;
                        }}
                        .nav {{
                            margin: 20px 0;
                            display: flex;
                            gap: 15px;
                            flex-wrap: wrap;
                            justify-content: center;
                        }}
                        a {{
                            color: #0f0;
                            text-decoration: none;
                            border: 1px solid #0f0;
                            padding: 8px 15px;
                            transition: all 0.3s ease;
                            font-weight: bold;
                        }}
                        a:hover {{
                            background: #0f0;
                            color: #000;
                        }}
                        .content {{
                            max-width: 800px;
                            text-align: center;
                            margin-top: 30px;
                        }}
                        .features {{
                            display: grid;
                            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                            gap: 20px;
                            margin: 30px 0;
                        }}
                        .feature {{
                            border: 1px solid #0f0;
                            padding: 15px;
                            background: rgba(0, 255, 0, 0.05);
                        }}
                        .cta {{
                            margin: 30px 0;
                            padding: 20px;
                            border: 2px solid #0f0;
                            background: rgba(0, 255, 0, 0.1);
                        }}
                        .filename {{
                            font-size: 0.9em;
                            opacity: 0.7;
                            margin-top: 10px;
                        }}
                        @media (max-width: 600px) {{
                            h1 {{ font-size: 2em; }}
                            .nav {{ flex-direction: column; align-items: center; }}
                        }}
                    </style>
                </head>
                <body>
                    <header>
                        <h1>🚀 peni.sh</h1>
                        <p class="tagline">AI-Powered WiFi Name Generator | Create Memorable Network Credentials</p>
                    </header>
                    
                    <nav class="nav">
                        <a href="/" title="Random Image">🎲 Random Image</a>
                        <a href="/api/wifi" title="Generate WiFi Credentials">📡 Generate WiFi</a>
                        <a href="/api/docs" title="API Documentation">📚 API Docs</a>
                        <a href="/sitemap.xml" title="Sitemap">🗺️ Sitemap</a>
                    </nav>
                    
                    <main>
                        <img src="{image_url}" alt="Random inspirational image for creative WiFi naming - {random_image.stem}" loading="lazy" />
                        <p class="filename">Image: {random_image.name}</p>
                        
                        <div class="content">
                            <div class="cta">
                                <h2>🌟 Generate Amazing WiFi Names Now!</h2>
                                <p>Create unique, memorable WiFi network names and passwords using advanced AI. Perfect for homes, offices, cafes, and events.</p>
                                <a href="/api/wifi" style="font-size: 1.2em; padding: 12px 25px;">🎯 Try WiFi Generator</a>
                            </div>
                            
                            <div class="features">
                                <div class="feature">
                                    <h3>🧠 AI-Powered</h3>
                                    <p>Uses advanced AI to create creative, memorable network names</p>
                                </div>
                                <div class="feature">
                                    <h3>🔐 Smart Passwords</h3>
                                    <p>Generates passwords that are easy to derive from the SSID</p>
                                </div>
                                <div class="feature">
                                    <h3>🆓 Completely Free</h3>
                                    <p>No registration, no limits, no cost - just great WiFi names</p>
                                </div>
                                <div class="feature">
                                    <h3>🚀 Instant Results</h3>
                                    <p>Get your new WiFi credentials in seconds via our API</p>
                                </div>
                            </div>
                            
                            <div style="margin-top: 40px;">
                                <h3>🎯 Perfect For:</h3>
                                <p>Home networks • Office WiFi • Coffee shops • Events • Hotels • Co-working spaces</p>
                            </div>
                        </div>
                    </main>
                    
                    <footer style="margin-top: 50px; padding: 20px; border-top: 1px solid #0f0; text-align: center; opacity: 0.8;">
                        <p>&copy; 2025 peni.sh - The future of WiFi naming</p>
                        <p><a href="/api/wifi">WiFi Generator</a> | <a href="/api/docs">API Docs</a> | <a href="mailto:admin@peni.sh">Contact</a></p>
                    </footer>
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


# SEO and Site Enhancement Endpoints


@app.get("/sitemap.xml", response_class=Response)
async def sitemap():
    """Generate XML sitemap for SEO."""
    sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>https://peni.sh</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>daily</changefreq>
        <priority>1.0</priority>
    </url>
    <url>
        <loc>https://peni.sh/api/wifi</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>always</changefreq>
        <priority>0.9</priority>
    </url>
    <url>
        <loc>https://peni.sh/api/docs</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>weekly</changefreq>
        <priority>0.8</priority>
    </url>
    <url>
        <loc>https://peni.sh/wifi-name-generator</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>weekly</changefreq>
        <priority>0.9</priority>
    </url>
    <url>
        <loc>https://peni.sh/creative-wifi-names</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>weekly</changefreq>
        <priority>0.8</priority>
    </url>
    <url>
        <loc>https://peni.sh/api/images</loc>
        <lastmod>2025-06-29</lastmod>
        <changefreq>daily</changefreq>
        <priority>0.6</priority>
    </url>
</urlset>"""

    return Response(content=sitemap_xml, media_type="application/xml")


@app.get("/robots.txt", response_class=Response)
async def robots():
    """Robots.txt for SEO."""
    robots_txt = """User-agent: *
Allow: /
Disallow: /api/docs/
Disallow: /api/redoc/

Sitemap: https://peni.sh/sitemap.xml

# Crawl-delay for being polite
Crawl-delay: 1"""

    return Response(content=robots_txt, media_type="text/plain")


@app.get("/favicon.ico")
async def favicon():
    """Simple favicon endpoint."""
    # Return a simple 1x1 pixel green PNG as favicon
    green_pixel = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x00\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    return Response(content=green_pixel, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_level="info")
