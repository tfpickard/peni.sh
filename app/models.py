"""Pydantic models for API requests and responses."""

from typing import Optional, List
from pydantic import BaseModel, Field


class SSIDPasswordPair(BaseModel):
    """WiFi SSID and password pair with hint."""

    ssid: str = Field(..., description="Generated SSID")
    password: str = Field(..., description="Memorable password based on SSID")
    hint: Optional[str] = Field(None, description="Optional hint for the password")


class ImageInfo(BaseModel):
    """Information about an image file."""

    filename: str = Field(..., description="Image filename")
    path: str = Field(..., description="Relative path to image")
    size_bytes: int = Field(..., description="File size in bytes")
    category: Optional[str] = Field(None, description="Image category")
    tags: List[str] = Field(default_factory=list, description="Image tags")


class ImageMetadata(BaseModel):
    """Metadata for an image."""

    filename: str
    category: str = "default"
    tags: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    description: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    image_count: int
    image_dir: str
    version: str
