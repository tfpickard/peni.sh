# Images Directory

This directory contains all images served by peni.sh.

## Structure

Images are organized by category in subdirectories:

```
images/
├── default/          # Default category
├── memes/           # Meme images
├── art/             # Artistic images
├── nature/          # Nature photos
└── metadata.json    # Image metadata file
```

## Adding Images

### Method 1: Using the CLI Tool (Recommended)

The easiest way to add images is using the `add_image.py` script:

```bash
# Add a single image
python scripts/add_image.py path/to/image.jpg --category memes --tags funny viral

# Add all images from a directory
python scripts/add_image.py path/to/directory/ --category vacation --recursive

# See all options
python scripts/add_image.py --help
```

### Method 2: Manual Addition

1. Copy image files into a category subdirectory (e.g., `images/memes/`)
2. Restart the application to refresh the cache

### Method 3: With Metadata

To add metadata, either:
- Use the CLI tool with `--tags`, `--title`, and `--description` options
- Manually edit `metadata.json` with your image information

## Supported Formats

- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- WebP (.webp)

## Metadata Format

The `metadata.json` file stores additional information about images:

```json
{
  "category/filename.jpg": {
    "filename": "category/filename.jpg",
    "category": "category",
    "tags": ["tag1", "tag2"],
    "title": "Image Title",
    "description": "Image description"
  }
}
```

## Tips

- Organize images by category for better management
- Use descriptive filenames
- Add tags for better searchability (future feature)
- Keep image sizes reasonable (< 10MB recommended)
