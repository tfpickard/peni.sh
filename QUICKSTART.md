# Quick Start Guide

Get peni.sh up and running in 5 minutes!

## Prerequisites

- Python 3.8 or higher
- OpenAI API key ([Get one here](https://platform.openai.com/api-keys))

## Local Development Setup

### 1. Clone and Navigate

```bash
git clone https://github.com/yourusername/peni.sh.git
cd peni.sh
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and add your OpenAI API key:

```bash
OPENAI_API_KEY=sk-your-actual-api-key-here
```

### 5. Add Some Images

```bash
# Method 1: Using the CLI tool (recommended)
python scripts/add_image.py /path/to/your/image.jpg --category memes

# Method 2: Manually copy to images directory
cp /path/to/your/images/* images/default/
```

### 6. Run the Application

```bash
python main.py
```

### 7. Test It Out

Open your browser and visit:

- **Home**: http://localhost:8000
- **API Docs**: http://localhost:8000/api/docs
- **Generate WiFi**: http://localhost:8000/api/wifi
- **Health Check**: http://localhost:8000/health

## Production Deployment

For production deployment on a server:

### Quick Deploy

```bash
export OPENAI_API_KEY=sk-your-key
sudo -E ./deploy.sh
```

This will:
- Install all dependencies
- Set up nginx with SSL
- Configure systemd service
- Start the application

### Manual Deploy

See the full deployment guide in [README.md](README.md#deployment-instructions-for-maximum-impact).

## Adding Images

### Using the CLI Tool

```bash
# Single image with metadata
python scripts/add_image.py path/to/image.jpg \
    --category memes \
    --tags funny viral \
    --title "Funny Meme" \
    --description "This made me laugh"

# Entire directory
python scripts/add_image.py ~/Pictures/ --category vacation --recursive

# List existing categories
python scripts/add_image.py --list-categories
```

### Manual Method

1. Copy images to a category folder:
   ```bash
   cp image.jpg images/my-category/
   ```

2. Restart the application (or wait 5 minutes for auto-refresh)

## Common Commands

### Development

```bash
# Run application
python main.py

# Add an image
python scripts/add_image.py image.jpg --category cats

# Check structure
ls -R app/
```

### Production

```bash
# Check status
sudo systemctl status penish

# View logs
sudo journalctl -u penish -f

# Restart service
sudo systemctl restart penish

# Add images
sudo -u penish python /opt/penish/scripts/add_image.py image.jpg --category cats
```

## Project Structure at a Glance

```
peni.sh/
├── app/                    # Application code
│   ├── api/               # API routes
│   ├── services/          # Business logic
│   ├── core/              # Utilities
│   ├── config.py          # Configuration
│   └── models.py          # Data models
├── images/                # Image storage
│   ├── default/          # Default category
│   └── metadata.json     # Image metadata
├── scripts/              # Utility scripts
│   └── add_image.py      # Image management CLI
└── main.py               # Application entry point
```

## Troubleshooting

### "OPENAI_API_KEY environment variable is required"

Set your API key in `.env`:
```bash
echo "OPENAI_API_KEY=sk-your-key" >> .env
```

### No images showing

1. Check images directory:
   ```bash
   ls -la images/default/
   ```

2. Add some images:
   ```bash
   python scripts/add_image.py path/to/image.jpg
   ```

### Import errors

Make sure you're in the project root and virtual environment is activated:
```bash
cd /path/to/peni.sh
source venv/bin/activate
python main.py
```

### Port already in use

Change the port in `.env`:
```bash
PORT=8001
```

## Next Steps

- Read [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for detailed documentation
- Check [README.md](README.md) for API examples in multiple languages
- Browse [images/README.md](images/README.md) for image management details

## Need Help?

- Check the logs for errors
- Review the documentation
- Open an issue on GitHub
- Read the inline code comments

## Example Workflow

Here's a complete example workflow:

```bash
# 1. Setup
git clone https://github.com/yourusername/peni.sh.git
cd peni.sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
echo "OPENAI_API_KEY=sk-your-key-here" >> .env

# 3. Add images
python scripts/add_image.py ~/Pictures/vacation/*.jpg \
    --category vacation \
    --tags travel beach summer

python scripts/add_image.py ~/Pictures/memes/*.png \
    --category memes \
    --tags funny viral

# 4. Run
python main.py

# 5. Test
curl http://localhost:8000/health
curl http://localhost:8000/api/wifi
curl http://localhost:8000/api/images | jq

# 6. Visit in browser
open http://localhost:8000
```

That's it! You're ready to go! 🚀
