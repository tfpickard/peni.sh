# Image Upload System Guide

## Overview

The peni.sh site now includes a secure, full-featured image upload system with the following capabilities:

- **Web-based Admin Panel** - Drag-and-drop interface for uploading images
- **Token-based Authentication** - Secure uploads using API tokens
- **Automatic Thumbnails** - 300x300 thumbnails generated for all images
- **EXIF Stripping** - Privacy protection by removing metadata
- **File Validation** - Magic byte detection prevents fake file uploads
- **Deduplication** - SHA256 hashing prevents duplicate images
- **Database Tracking** - SQLite database for image metadata
- **Image Management** - View, browse, and delete images through the UI

## Quick Start

### 1. Access the Admin Panel

Navigate to: `https://peni.sh/admin`

### 2. Get Your Upload Token

After deployment, your upload token is displayed and saved in `/opt/penish/.env`

To retrieve it later:
```bash
sudo grep UPLOAD_TOKEN /opt/penish/.env
```

### 3. Upload Images

1. Enter your upload token in the admin panel
2. Click "Save Token" (stores in browser localStorage)
3. Drag and drop images or click the upload zone
4. Images are automatically validated, optimized, and thumbnailed

## Features

### Security Features

- **Authentication**: All upload/delete operations require a bearer token
- **File Validation**: Magic byte detection (not just extension checking)
- **Size Limits**: Configurable max file size (default 10MB)
- **Path Traversal Protection**: Sanitized filenames and path validation
- **EXIF Stripping**: Privacy protection by removing camera metadata
- **HTTPS Only**: All admin operations over SSL

### Image Processing

- **Format Support**: JPEG, PNG, GIF, WebP
- **Automatic Thumbnails**: 300x300 pixels, optimized quality
- **Optimization**: Images saved with quality=95, optimize=True
- **Deduplication**: SHA256 hashing prevents duplicate uploads
- **Dimension Tracking**: Width/height stored in database

### Database Schema

SQLite database at `/var/www/peni.sh/images.db`:

```sql
CREATE TABLE images (
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
```

## API Endpoints

### Upload Image

```bash
POST /api/upload
Authorization: Bearer <your-token>
Content-Type: multipart/form-data

Response:
{
  "filename": "image.jpg",
  "size_bytes": 123456,
  "thumbnail_created": true,
  "message": "Image uploaded successfully: image.jpg"
}
```

### Delete Image

```bash
DELETE /api/images/{filename}
Authorization: Bearer <your-token>

Response:
{
  "filename": "image.jpg",
  "deleted": true,
  "message": "Image deleted successfully: image.jpg"
}
```

### List Images

```bash
GET /api/images

Response:
[
  {
    "filename": "image.jpg",
    "path": "image.jpg",
    "size_bytes": 123456,
    "upload_date": "2025-10-23T12:00:00",
    "width": 1920,
    "height": 1080,
    "has_thumbnail": true
  }
]
```

### Get Thumbnail

```bash
GET /thumbnail/{filename}
```

## Command Line Upload

Using curl:

```bash
# Upload an image
curl -X POST https://peni.sh/api/upload \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -F "file=@/path/to/image.jpg"

# Delete an image
curl -X DELETE https://peni.sh/api/images/image.jpg \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"
```

## Configuration

Environment variables in `/opt/penish/.env`:

```bash
IMAGE_DIR=/var/www/peni.sh/images
THUMBNAIL_DIR=/var/www/peni.sh/images/thumbnails
DB_PATH=/var/www/peni.sh/images.db
MAX_UPLOAD_SIZE_MB=10
UPLOAD_TOKEN=your-secure-token-here
```

## File Organization

```
/var/www/peni.sh/
├── images/              # Full-size images
│   └── thumbnails/      # Auto-generated thumbnails (300x300)
└── images.db            # SQLite metadata database
```

## Troubleshooting

### Upload Token Not Working

1. Check token is set correctly:
   ```bash
   sudo cat /opt/penish/.env | grep UPLOAD_TOKEN
   ```

2. Restart the service:
   ```bash
   sudo systemctl restart penish
   ```

### Thumbnails Not Generating

1. Check directory permissions:
   ```bash
   ls -la /var/www/peni.sh/images/thumbnails
   ```

2. Should be owned by `penish:penish`

3. Check logs:
   ```bash
   journalctl -u penish -f
   ```

### Database Locked

If you get "database is locked" errors:

```bash
sudo chown penish:penish /var/www/peni.sh/images.db
sudo chmod 644 /var/www/peni.sh/images.db
```

## Security Best Practices

1. **Keep your upload token secret** - It's like a password
2. **Use HTTPS only** - Never send the token over HTTP
3. **Rotate tokens periodically** - Update `UPLOAD_TOKEN` in .env
4. **Monitor uploads** - Check logs regularly for suspicious activity
5. **Backup the database** - SQLite file at `/var/www/peni.sh/images.db`

## Backup and Restore

### Backup

```bash
# Backup images and database
sudo tar -czf peni.sh-backup.tar.gz \
  /var/www/peni.sh/images \
  /var/www/peni.sh/images.db
```

### Restore

```bash
# Extract backup
sudo tar -xzf peni.sh-backup.tar.gz -C /

# Fix permissions
sudo chown -R penish:penish /var/www/peni.sh
sudo systemctl restart penish
```

## Migration from Manual Uploads

Existing images in `/var/www/peni.sh/images/` are automatically:
- Detected on first cache refresh (5 minutes or on restart)
- Added to the database with basic metadata
- Available through the admin interface

The system performs a database sync on startup to include manually-placed files.

## Performance Notes

- **Cache**: Image list cached for 5 minutes, auto-refreshes on upload/delete
- **Thumbnails**: Generated once on upload, served from filesystem
- **Database**: SQLite with indexes on filename and upload_date
- **Concurrency**: Supports multiple simultaneous uploads via async processing

## Rate Limiting

Configured in nginx.conf:
- API endpoints: 10 requests/second
- Image serving: 30 requests/second

## Support

For issues or questions:
- Check logs: `journalctl -u penish -f`
- Check nginx logs: `tail -f /var/log/nginx/peni.sh_error.log`
- View API docs: `https://peni.sh/api/docs`
