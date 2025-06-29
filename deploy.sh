#!/bin/bash
# robust-deploy.sh - More robust deployment with error handling

set -e

echo "🚀 Starting robust deployment..."

# Navigate to project directory
cd /opt/penish-repo || { 
  echo "Creating project directory..."
  sudo mkdir -p /opt/penish-repo
  sudo chown $(whoami):$(whoami) /opt/penish-repo
  cd /opt/penish-repo
}

# Clone or pull latest changes
if [ ! -d ".git" ]; then
  echo "🔄 Initial clone..."
  git clone ${{ github.event.repository.clone_url }} .
else
  echo "🔄 Pulling latest changes..."
  git fetch --all
  git reset --hard origin/main
fi

echo "📁 Current repo contents:"
ls -la

# Set up environment variables
echo "🔧 Setting up environment variables..."
sudo rm -f /opt/penish/.env
echo "IMAGE_DIR=/var/www/peni.sh/images" | sudo tee /opt/penish/.env >/dev/null
echo "ENVIRONMENT=production" | sudo tee -a /opt/penish/.env >/dev/null
echo "OPENAI_API_KEY=${{ secrets.OPENAI_API_KEY }}" | sudo tee -a /opt/penish/.env >/dev/null
echo "OPENAI_MODEL=gpt-4" | sudo tee -a /opt/penish/.env >/dev/null
sudo chmod 600 /opt/penish/.env
sudo chown penish:penish /opt/penish/.env

# Copy files with error checking
echo "📁 Copying application files..."

# Copy main.py
if [ -f "main.py" ]; then
  sudo cp main.py /opt/penish/
  sudo chown penish:penish /opt/penish/main.py
  echo "✅ Copied main.py"
else
  echo "❌ main.py not found in repo!"
  ls -la *.py || echo "No Python files found"
  exit 1
fi

# Copy or create requirements.txt
if [ -f "requirements.txt" ]; then
  sudo cp requirements.txt /opt/penish/
  sudo chown penish:penish /opt/penish/requirements.txt
  echo "✅ Copied requirements.txt from repo"
else
  echo "⚠️ requirements.txt not found in repo, creating default..."
  sudo tee /opt/penish/requirements.txt << 'EOF'
fastapi==0.104.1
uvicorn[standard]==0.24.0
python-multipart==0.0.6
jinja2==3.1.2
python-dotenv==1.0.0
aiofiles==23.2.1
pillow==10.1.0
pydantic==2.5.0
openai==1.3.0
EOF
  sudo chown penish:penish /opt/penish/requirements.txt
  echo "✅ Created default requirements.txt"
fi

# Copy nginx config
if [ -f "nginx.conf" ]; then
  sudo cp nginx.conf /etc/nginx/sites-available/peni.sh
  echo "✅ Copied nginx.conf"
else
  echo "⚠️ nginx.conf not found in repo"
fi

# Verify requirements.txt exists before installing
if [ ! -f "/opt/penish/requirements.txt" ]; then
  echo "❌ requirements.txt still missing!"
  exit 1
fi

echo "🐍 Installing Python dependencies..."
echo "📋 Requirements.txt contents:"
cat /opt/penish/requirements.txt

# Install dependencies with better error handling
sudo -u penish /opt/penish/venv/bin/pip install --upgrade pip
if sudo -u penish /opt/penish/venv/bin/pip install -r /opt/penish/requirements.txt; then
  echo "✅ Dependencies installed successfully"
else
  echo "❌ Failed to install dependencies!"
  echo "📋 Checking pip and python versions:"
  sudo -u penish /opt/penish/venv/bin/python --version
  sudo -u penish /opt/penish/venv/bin/pip --version
  exit 1
fi

# Test nginx configuration (skip if no nginx.conf)
if [ -f "/etc/nginx/sites-available/peni.sh" ]; then
  echo "🧪 Testing nginx configuration..."
  if sudo nginx -t; then
    echo "✅ Nginx config is valid"
  else
    echo "❌ Nginx config has errors!"
    exit 1
  fi
else
  echo "⚠️ Skipping nginx test (no config file)"
fi

echo "🔄 Restarting services..."
sudo systemctl restart penish

# Wait a bit longer for service to start
sleep 10

echo "✅ Verifying services..."
if sudo systemctl is-active --quiet penish; then
  echo "✅ penish service is running"
  
  # Test the application
  if curl -s http://localhost:8000/health | grep -q "healthy"; then
    echo "✅ Application is responding correctly"
  else
    echo "⚠️ Service running but not responding properly"
    echo "App response:"
    curl -s http://localhost:8000/health || echo "No response"
    echo "Recent logs:"
    sudo journalctl -u penish --no-pager -l | tail -5
  fi
else
  echo "❌ penish service failed to start"
  echo "Service status:"
  sudo systemctl status penish --no-pager || true
  echo "Recent logs:"
  sudo journalctl -u penish --no-pager -l | tail -10
  exit 1
fi

# Restart nginx if config was updated
if [ -f "/etc/nginx/sites-available/peni.sh" ]; then
  sudo systemctl reload nginx
  if sudo systemctl is-active --quiet nginx; then
    echo "✅ nginx service is running"
  else
    echo "❌ nginx service failed"
    sudo journalctl -u nginx --no-pager -l | tail-5
  fi
fi

echo "🎉 Deployment completed successfully!"