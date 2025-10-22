#!/usr/bin/env python3
"""
peni.sh - Dynamic SSID/Password Generator and Random Image Server
Main application entry point.
"""

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import Config
from app.api.routes import router

# Configure logging
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# FastAPI app initialization
app = FastAPI(
    title="peni.sh",
    description="Dynamic SSID/Password Generator and Random Image Server",
    version=__version__,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router)


@app.on_event("startup")
async def startup_event():
    """Run on application startup."""
    logger.info(f"Starting peni.sh v{__version__}")
    logger.info(f"Image directory: {Config.IMAGE_DIR}")
    logger.info(f"OpenAI model: {Config.OPENAI_MODEL}")


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown."""
    logger.info("Shutting down peni.sh")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=Config.HOST,
        port=Config.PORT,
        reload=Config.RELOAD,
        log_level=Config.LOG_LEVEL,
    )
