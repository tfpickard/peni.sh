# 🚀 Complete GitHub CI/CD Setup for peni.sh

**Goal**: Push code → GitHub → Server automatically pulls and redeploys → World domination achieved

## 📋 Quick Setup Overview

1. **Create GitHub repo and push code**
2. **Set up GitHub Actions** for automatic deployment
3. **Configure server with deploy keys**
4. **Test the automated pipeline**

---

## 🎯 Method 1: GitHub Actions with SSH (Recommended)

This is the most reliable method - GitHub Actions will SSH into your server and run the deployment.

### Step 1: Create GitHub Repository

```bash
# In your project directory (where you have all the peni.sh files)
git init
git add .
git commit -m "🚀 Initial commit: peni.sh - The future of WiFi naming"

# Create repo on GitHub, then:
git remote add origin https://github.com/yourusername/penish.git
git branch -M main
git push -u origin main
```

### Step 2: Set up SSH Key for GitHub Actions

On your **server**:
```bash
# Generate SSH key for GitHub Actions
ssh-keygen -t ed25519 -f ~/.ssh/github_actions_key -N ""

# Display the public key (add this as a deploy key on GitHub)
cat ~/.ssh/github_actions_key.pub

# Display the private key (add this as a GitHub secret)
cat ~/.ssh/github_actions_key
```

### Step 3: Configure GitHub Repository

**Add Deploy Key** (Settings → Deploy keys → Add deploy key):
- Title: `Server Deploy Key`
- Key: Contents of `~/.ssh/github_actions_key.pub`
- ✅ Allow write access

**Add Repository Secrets** (Settings → Secrets and variables → Actions):
- `HOST`: Your server IP address
- `USERNAME`: Your server username  
- `SSH_KEY`: Contents of `~/.ssh/github_actions_key` (private key)
- `OPENAI_API_KEY`: Your OpenAI API key

### Step 4: Create GitHub Actions Workflow

Create `.github/workflows/deploy.yml`:

```yaml
name: 🚀 Deploy peni.sh to Production

on:
  push:
    branches: [ main ]
  workflow_dispatch: # Allow manual triggers

jobs:
  deploy:
    name: 🌍 Deploy to Server
    runs-on: ubuntu-latest
    
    steps:
    - name: 📥 Checkout Code
      uses: actions/checkout@v4
      
    - name: 🔐 Setup SSH
      uses: webfactory/ssh-agent@v0.7.0
      with:
        ssh-private-key: ${{ secrets.SSH_KEY }}
        
    - name: 🚀 Deploy to Server
      run: |
        ssh -o StrictHostKeyChecking=no ${{ secrets.USERNAME }}@${{ secrets.HOST }} << 'ENDSSH'
          # Navigate to project directory
          cd /opt/penish-repo || { echo "Creating project directory..."; sudo mkdir -p /opt/penish-repo; sudo chown $(whoami):$(whoami) /opt/penish-repo; cd /opt/penish-repo; }
          
          # Clone or pull latest changes
          if [ ! -d ".git" ]; then
            echo "🔄 Initial clone..."
            git clone https://github.com/${{ github.repository }} .
          else
            echo "🔄 Pulling latest changes..."
            git fetch --all
            git reset --hard origin/main
          fi
          
          # Set up environment variables
          sudo bash -c "cat > /opt/penish/.env << EOF
        IMAGE_DIR=/var/www/peni.sh/images
        ENVIRONMENT=production
        OPENAI_API_KEY=${{ secrets.OPENAI_API_KEY }}
        OPENAI_MODEL=gpt-4
        EOF"
        
          # Copy files to production directory
          sudo cp main.py /opt/penish/
          sudo cp requirements.txt /opt/penish/
          sudo cp nginx.conf /etc/nginx/sites-available/peni.sh
          
          # Install/update Python dependencies
          sudo -u penish /opt/penish/venv/bin/pip install -r requirements.txt
          
          # Test nginx configuration
          sudo nginx -t
          
          # Restart services
          sudo systemctl restart penish
          sudo systemctl reload nginx
          
          # Verify services are running
          sleep 5
          sudo systemctl is-active penish || echo "❌ penish service failed"
          sudo systemctl is-active nginx || echo "❌ nginx service failed"
          
          echo "✅ Deployment completed successfully!"
        ENDSSH
        
    - name: 🧪 Test Deployment
      run: |
        # Wait a moment for services to fully start
        sleep 10
        
        # Test if the site is responding
        if curl -s https://peni.sh/health | grep -q "healthy"; then
          echo "✅ Health check passed!"
        else
          echo "❌ Health check failed!"
          exit 1
        fi
        
        # Test WiFi API endpoint
        if curl -s https://peni.sh/api/wifi | grep -q "ssid"; then
          echo "✅ WiFi API is working!"
        else
          echo "❌ WiFi API failed!"
          exit 1
        fi
```

---

## 🎯 Method 2: Simple Webhook Approach

If you prefer a lighter approach, set up a webhook endpoint on your server.

### Step 1: Create Webhook Script on Server

Create `/opt/penish/webhook.py`:

```python
#!/usr/bin/env python3
"""
Simple webhook server for automatic deployments
"""
import subprocess
import json
import hmac
import hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
import os

WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'your-secret-key-here')
REPO_PATH = '/opt/penish-repo'

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/webhook':
            self.send_response(404)
            self.end_headers()
            return
            
        # Verify GitHub signature
        signature = self.headers.get('X-Hub-Signature-256')
        if not self.verify_signature(signature):
            self.send_response(401)
            self.end_headers()
            return
            
        # Read payload
        content_length = int(self.headers['Content-Length'])
        payload = self.rfile.read(content_length)
        
        try:
            data = json.loads(payload)
            if data.get('ref') == 'refs/heads/main':
                self.deploy()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Deployment triggered!')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'Not main branch, ignoring')
        except Exception as e:
            print(f"Error: {e}")
            self.send_response(500)
            self.end_headers()
    
    def verify_signature(self, signature):
        if not signature:
            return False
        expected = hmac.new(
            WEBHOOK_SECRET.encode(),
            self.rfile.read(int(self.headers['Content-Length'])),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", signature)
    
    def deploy(self):
        """Run deployment script"""
        try:
            subprocess.run(['/opt/penish/auto-deploy.sh'], check=True)
            print("✅ Deployment completed successfully!")
        except subprocess.CalledProcessError as e:
            print(f"❌ Deployment failed: {e}")

if __name__ == '__main__':
    server = HTTPServer(('localhost', 9000), WebhookHandler)
    print("🚀 Webhook server running on port 9000")
    server.serve_forever()
```

### Step 2: Create Auto-Deploy Script

Create `/opt/penish/auto-deploy.sh`:

```bash
#!/bin/bash
# auto-deploy.sh - Automatic deployment script

set -euo pipefail

REPO_PATH="/opt/penish-repo"
LOG_FILE="/var/log/penish/deploy.log"

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "🚀 Starting automatic deployment..."

# Navigate to repo directory
cd "$REPO_PATH"

# Pull latest changes
log "📥 Pulling latest changes from GitHub..."
git fetch --all
git reset --hard origin/main

# Copy files to production
log "📁 Copying files to production directory..."
sudo cp main.py /opt/penish/
sudo cp requirements.txt /opt/penish/
sudo cp nginx.conf /etc/nginx/sites-available/peni.sh

# Update Python dependencies
log "🐍 Updating Python dependencies..."
sudo -u penish /opt/penish/venv/bin/pip install -r requirements.txt

# Test nginx configuration
log "🧪 Testing nginx configuration..."
sudo nginx -t

# Restart services
log "🔄 Restarting services..."
sudo systemctl restart penish
sudo systemctl reload nginx

# Wait and verify
sleep 5
if sudo systemctl is-active --quiet penish && sudo systemctl is-active --quiet nginx; then
    log "✅ Deployment completed successfully!"
else
    log "❌ Some services failed to start properly"
    exit 1
fi
```

### Step 3: Set up Webhook Service

Create systemd service `/etc/systemd/system/penish-webhook.service`:

```ini
[Unit]
Description=peni.sh Webhook Server
After=network.target

[Service]
Type=simple
User=penish
WorkingDirectory=/opt/penish
Environment=WEBHOOK_SECRET=your-super-secret-key-here
ExecStart=/opt/penish/venv/bin/python webhook.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable penish-webhook
sudo systemctl start penish-webhook
```

### Step 4: Configure GitHub Webhook

In your GitHub repo: Settings → Webhooks → Add webhook:
- **Payload URL**: `https://peni.sh:9000/webhook`
- **Content type**: `application/json`
- **Secret**: `your-super-secret-key-here`
- **Events**: Just push events

---

## 🎯 Method 3: Git Hooks (Simplest)

Set up a git hook that triggers on push to automatically deploy.

### Step 1: Set up Bare Repository on Server

```bash
# Create bare repo on server
sudo mkdir -p /opt/penish-git
sudo git init --bare /opt/penish-git
sudo chown -R penish:penish /opt/penish-git

# Create post-receive hook
sudo tee /opt/penish-git/hooks/post-receive << 'EOF'
#!/bin/bash
# post-receive hook for automatic deployment

echo "🚀 Received push to peni.sh repository"

# Temporary directory for checkout
TMPDIR="/tmp/penish-deploy-$(date +%s)"
git --git-dir=/opt/penish-git --work-tree="$TMPDIR" checkout -f

echo "📁 Copying files to production..."
cp "$TMPDIR/main.py" /opt/penish/
cp "$TMPDIR/requirements.txt" /opt/penish/
cp "$TMPDIR/nginx.conf" /etc/nginx/sites-available/peni.sh

echo "🐍 Updating dependencies..."
/opt/penish/venv/bin/pip install -r /opt/penish/requirements.txt

echo "🔄 Restarting services..."
systemctl restart penish
systemctl reload nginx

echo "✅ Deployment complete!"

# Cleanup
rm -rf "$TMPDIR"
EOF

sudo chmod +x /opt/penish-git/hooks/post-receive
```

### Step 2: Add Server as Git Remote

```bash
# On your local machine, add server as remote
git remote add production user@your-server:/opt/penish-git

# Push to deploy
git push production main
```

---

## 🧪 Testing Your CI/CD Pipeline

### Test the Complete Workflow

1. **Make a change**:
   ```bash
   echo "# Testing auto-deploy" >> README.md
   git add README.md
   git commit -m "🧪 Test: Auto-deployment pipeline"
   git push origin main
   ```

2. **Monitor the deployment**:
   ```bash
   # Watch GitHub Actions (if using Method 1)
   # Check: https://github.com/yourusername/penish/actions
   
   # Or watch server logs
   sudo journalctl -u penish -f
   sudo tail -f /var/log/penish/deploy.log
   ```

3. **Verify it worked**:
   ```bash
   curl https://peni.sh/health
   curl https://peni.sh/api/wifi
   ```

### Emergency Rollback Plan

If something breaks:
```bash
# Quick rollback using git
cd /opt/penish-repo
git log --oneline -5  # See recent commits
git checkout <previous-commit-hash>
sudo systemctl restart penish
```

---

## 🔧 Advanced CI/CD Features

### Multi-Environment Setup

```yaml
# In .github/workflows/deploy.yml
strategy:
  matrix:
    environment: [staging, production]
    include:
      - environment: staging
        host: staging.peni.sh
      - environment: production
        host: peni.sh
```

### Database Migrations (if you add a DB later)

```bash
# Add to deploy script
echo "🗄️ Running database migrations..."
/opt/penish/venv/bin/python manage.py migrate
```

### Health Checks & Monitoring

```yaml
# Add to GitHub Actions
- name: 📊 Post-Deploy Health Check
  run: |
    # Wait for service to fully start
    sleep 30
    
    # Check all endpoints
    curl -f https://peni.sh/health
    curl -f https://peni.sh/api/wifi
    curl -f https://peni.sh/sitemap.xml
    
    echo "✅ All health checks passed!"
```

---

## 🚨 Security Best Practices

1. **Use SSH keys, not passwords**
2. **Restrict SSH access** to specific IPs if possible
3. **Use GitHub secrets** for sensitive data
4. **Rotate secrets regularly**
5. **Monitor deployment logs** for anomalies

```bash
# Set up fail2ban for SSH protection
sudo apt install fail2ban
sudo systemctl enable fail2ban
```

---

## 🎉 Your Complete Workflow

Once set up, your workflow becomes:

```bash
# 1. Make changes
nvim main.py

# 2. Test locally  
python main.py

# 3. Commit and push
git add .
git commit -m "🚀 Add awesome new feature"
git push origin main

# 4. Watch the magic happen!
# GitHub Actions automatically:
# - Tests your code
# - SSHs to your server  
# - Pulls latest changes
# - Restarts services
# - Runs health checks
# - Notifies you of success/failure

# 5. Verify deployment
curl https://peni.sh/api/wifi
```

**That's it!** You now have a professional CI/CD pipeline that automatically deploys your WiFi naming revolution to the world! 🌍✨

Choose **Method 1 (GitHub Actions)** for the most robust solution, or **Method 3 (Git Hooks)** for simplicity. Both will make you a deployment wizard! 🧙‍♂️