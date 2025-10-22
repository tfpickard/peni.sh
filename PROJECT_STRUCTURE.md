# Project Structure Documentation

This document explains the refactored project structure of peni.sh and how to work with it.

## Overview

The project has been refactored from a single monolithic `main.py` file into a modular, maintainable structure that follows best practices for FastAPI applications.

## Directory Structure

```
peni.sh/
├── app/                      # Main application package
│   ├── __init__.py          # Package initialization, version info
│   ├── config.py            # Configuration management
│   ├── models.py            # Pydantic models for data validation
│   ├── api/                 # API routes
│   │   ├── __init__.py
│   │   └── routes.py        # All API endpoint definitions
│   ├── services/            # Business logic layer
│   │   ├── __init__.py
│   │   ├── wifi_generator.py    # WiFi credential generation
│   │   └── image_manager.py     # Image management & metadata
│   └── core/                # Core utilities
│       ├── __init__.py
│       └── cache.py         # Image caching system
├── images/                  # Image storage
│   ├── default/            # Default category
│   ├── metadata.json       # Image metadata
│   └── README.md           # Images directory documentation
├── scripts/                # Utility scripts
│   └── add_image.py       # CLI tool for adding images
├── main.py                 # Application entry point
├── requirements.txt        # Python dependencies
├── .env.example           # Example environment configuration
├── deploy.sh              # Deployment script
└── README.md              # Main project README
```

## Key Components

### 1. Configuration (`app/config.py`)

Centralized configuration management using environment variables:

```python
from app.config import Config

# Access configuration
image_dir = Config.IMAGE_DIR
api_key = Config.OPENAI_API_KEY
```

**Environment Variables:**
- `IMAGE_DIR` - Where images are stored
- `OPENAI_API_KEY` - OpenAI API key (required)
- `OPENAI_MODEL` - OpenAI model to use
- `HOST` / `PORT` - Server configuration
- `LOG_LEVEL` - Logging verbosity
- `ALLOWED_ORIGINS` - CORS origins

### 2. Models (`app/models.py`)

Pydantic models for type safety and validation:

- `SSIDPasswordPair` - WiFi credentials
- `ImageInfo` - Image file information
- `ImageMetadata` - Image metadata
- `HealthResponse` - Health check response

### 3. API Routes (`app/api/routes.py`)

All HTTP endpoints in one organized module:

- `GET /` - Home page with random image
- `GET /api/wifi` - Generate WiFi credentials
- `GET /image/{filename}` - Serve specific image
- `GET /api/images` - List all images
- `GET /api/categories` - List image categories
- `GET /health` - Health check

### 4. Services (`app/services/`)

Business logic separated from API layer:

#### WiFi Generator (`wifi_generator.py`)
```python
from app.services.wifi_generator import generate_ssid_password

# Generate credentials
credentials = await generate_ssid_password()
```

#### Image Manager (`image_manager.py`)
```python
from app.services.image_manager import image_manager

# Add metadata
image_manager.add_image_metadata(
    filename="cat.jpg",
    category="pets",
    tags=["cute", "cat"]
)

# Get categories
categories = image_manager.list_categories()
```

### 5. Core Utilities (`app/core/`)

#### Cache System (`cache.py`)
```python
from app.core.cache import image_cache

# Get cached images
images = await image_cache.get_images()

# Force refresh
await image_cache.force_refresh()
```

## Adding New Features

### Adding a New API Endpoint

1. Add the route in `app/api/routes.py`:

```python
@router.get("/api/new-endpoint")
async def new_endpoint():
    """Your new endpoint."""
    return {"message": "Hello!"}
```

2. If needed, add a model in `app/models.py`:

```python
class NewModel(BaseModel):
    field: str
```

3. If complex logic is needed, add a service in `app/services/`:

```python
# app/services/new_service.py
async def do_something():
    """Business logic here."""
    pass
```

### Adding New Configuration

1. Add to `app/config.py`:

```python
class Config:
    NEW_SETTING = os.getenv("NEW_SETTING", "default_value")
```

2. Add to `.env.example`:

```bash
NEW_SETTING=value
```

3. Update deployment script if needed

## Working with Images

### Adding Images via CLI (Recommended)

```bash
# Single image
python scripts/add_image.py path/to/image.jpg --category memes --tags funny viral

# Entire directory
python scripts/add_image.py path/to/dir/ --category vacation --recursive

# With full metadata
python scripts/add_image.py image.jpg \
    --category art \
    --tags abstract colorful \
    --title "My Artwork" \
    --description "A beautiful piece"
```

### Adding Images Manually

1. Copy image to category directory:
```bash
cp image.jpg images/category-name/
```

2. Restart application or wait for cache refresh (5 minutes)

### Image Metadata Format

Edit `images/metadata.json`:

```json
{
  "category/filename.jpg": {
    "filename": "category/filename.jpg",
    "category": "category",
    "tags": ["tag1", "tag2"],
    "title": "Image Title",
    "description": "Description"
  }
}
```

## Development Workflow

### Local Development

1. Clone repository:
```bash
git clone https://github.com/yourusername/peni.sh.git
cd peni.sh
```

2. Create virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create `.env` file:
```bash
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

5. Run the application:
```bash
python main.py
```

6. Visit http://localhost:8000

### Testing

```bash
# Run application in debug mode
python main.py

# Check logs
tail -f logs/app.log

# Test API endpoints
curl http://localhost:8000/health
curl http://localhost:8000/api/wifi
curl http://localhost:8000/api/images
```

## Deployment

### Production Deployment

1. Ensure environment is configured:
```bash
export OPENAI_API_KEY=sk-your-key
```

2. Run deployment script:
```bash
sudo -E ./deploy.sh
```

3. Monitor logs:
```bash
journalctl -u penish -f
```

### Updating Deployed Application

1. Pull latest changes:
```bash
cd /opt/penish
git pull
```

2. Update dependencies:
```bash
source venv/bin/activate
pip install -r requirements.txt
```

3. Restart service:
```bash
sudo systemctl restart penish
```

## Benefits of New Structure

### Maintainability
- **Separation of concerns** - Each module has a single responsibility
- **Easy to navigate** - Logical directory structure
- **Testable** - Components can be tested independently

### Scalability
- **Modular design** - Easy to add new features
- **Service layer** - Business logic separated from API
- **Configuration management** - Environment-based config

### Developer Experience
- **Clear organization** - Know where to find things
- **Type safety** - Pydantic models prevent errors
- **Documentation** - Self-documenting code structure

### Ease of Adding Images
- **CLI tool** - Simple command-line interface
- **Metadata support** - Rich image information
- **Categories** - Organized image storage
- **Auto-scanning** - Automatic detection of new images

## Migration from Old Structure

The old monolithic `main.py` has been backed up as `main.py.old`.

### Key Changes:
1. **Configuration** - Moved from inline to `app/config.py`
2. **Models** - Extracted to `app/models.py`
3. **Routes** - Organized in `app/api/routes.py`
4. **Services** - Business logic in `app/services/`
5. **Caching** - Isolated in `app/core/cache.py`

### What Stayed the Same:
- All API endpoints work identically
- Image serving functionality unchanged
- WiFi generation produces same results
- Compatible with existing deployment

## Troubleshooting

### Import Errors

If you get import errors, ensure you're running from the project root:
```bash
cd /path/to/peni.sh
python main.py
```

### Configuration Not Found

Ensure `.env` file exists or set environment variables:
```bash
export OPENAI_API_KEY=sk-your-key
```

### Images Not Appearing

1. Check image directory exists:
```bash
ls images/
```

2. Force cache refresh by restarting:
```bash
# Development
# Just restart the application

# Production
sudo systemctl restart penish
```

3. Check logs:
```bash
journalctl -u penish -f
```

## Future Enhancements

Potential improvements to consider:

- [ ] Unit tests for each module
- [ ] Database for metadata (instead of JSON)
- [ ] Image upload via API
- [ ] Image search by tags
- [ ] User authentication
- [ ] Rate limiting per user
- [ ] Image analytics
- [ ] CDN integration
- [ ] Docker containerization
- [ ] CI/CD pipeline

## Contributing

When contributing to this project:

1. Follow the existing structure
2. Put API routes in `app/api/`
3. Put business logic in `app/services/`
4. Put utilities in `app/core/`
5. Add models to `app/models.py`
6. Update this documentation
7. Test your changes

## Questions?

- Check the README.md for general information
- Review inline code comments
- Look at existing code for examples
- Consult FastAPI documentation: https://fastapi.tiangolo.com
