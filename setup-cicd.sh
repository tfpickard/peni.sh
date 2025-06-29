#!/bin/bash
# setup-cicd.sh - One-command CI/CD setup for peni.sh
# Run with: ./setup-cicd.sh

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[$(date +'%H:%M:%S')] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +'%H:%M:%S')] WARNING: $1${NC}"
}

error() {
    echo -e "${RED}[$(date +'%H:%M:%S')] ERROR: $1${NC}"
    exit 1
}

info() {
    echo -e "${BLUE}[$(date +'%H:%M:%S')] INFO: $1${NC}"
}

# Configuration
GITHUB_REPO=""
SERVER_HOST=""
SERVER_USER=""
OPENAI_API_KEY=""

print_banner() {
    echo -e "${GREEN}"
    cat << 'EOF'
    ____             _       __    
   / __ \___  ____  (_)_____/ /_   
  / /_/ / _ \/ __ \/ / ___/ __ \  
 / ____/  __/ / / / (__  ) / / /  
/_/    \___/_/ /_/_/____/_/ /_/   
                                 
🚀 CI/CD Setup for World Domination
EOF
    echo -e "${NC}"
}

collect_config() {
    echo -e "${BLUE}📋 Let's set up your CI/CD pipeline!${NC}"
    echo
    
    read -p "GitHub username: " GITHUB_USER
    read -p "Repository name (e.g., penish): " REPO_NAME
    GITHUB_REPO="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
    
    read -p "Server IP address: " SERVER_HOST
    read -p "Server username: " SERVER_USER
    read -s -p "OpenAI API Key: " OPENAI_API_KEY
    echo
    
    echo
    info "Configuration collected:"
    info "GitHub Repo: ${GITHUB_REPO}"
    info "Server: ${SERVER_USER}@${SERVER_HOST}"
    info "OpenAI Key: ${OPENAI_API_KEY:0:10}..."
    echo
    
    read -p "Continue with setup? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        error "Setup cancelled by user"
    fi
}

setup_git_repo() {
    log "Setting up Git repository..."
    
    # Initialize git if not already done
    if [ ! -d ".git" ]; then
        git init
        log "Initialized Git repository"
    fi
    
    # Add all files
    git add .
    
    # Create initial commit if needed
    if ! git rev-parse --verify HEAD &>/dev/null; then
        git commit -m "🚀 Initial commit: peni.sh - The future of WiFi naming"
        log "Created initial commit"
    fi
    
    # Add remote if not exists
    if ! git remote get-url origin &>/dev/null; then
        git remote add origin "$GITHUB_REPO"
        log "Added GitHub remote"
    fi
    
    # Create main branch and push
    git branch -M main
    log "Set main branch"
}

create_github_actions() {
    log "Creating GitHub Actions workflow..."
    
    mkdir -p .github/workflows
    
    cat > .github/workflows/deploy.yml << EOF
name: 🚀 Deploy peni.sh to Production

on:
  push:
    branches: [ main ]
  workflow_dispatch:

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
        ssh-private-key: \${{ secrets.SSH_KEY }}
        
    - name: 🚀 Deploy to Server
      run: |
        ssh -o StrictHostKeyChecking=no \${{ secrets.USERNAME }}@\${{ secrets.HOST }} << 'ENDSSH'
          # Navigate to project directory
          cd /opt/penish-repo || { 
            echo "Creating project directory..."
            sudo mkdir -p /opt/penish-repo
            sudo chown \$(whoami):\$(whoami) /opt/penish-repo
            cd /opt/penish-repo
          }
          
          # Clone or pull latest changes
          if [ ! -d ".git" ]; then
            echo "🔄 Initial clone..."
            git clone \${{ github.event.repository.clone_url }} .
          else
            echo "🔄 Pulling latest changes..."
            git fetch --all
            git reset --hard origin/main
          fi
          
          # Set up environment variables
          sudo bash -c "cat > /opt/penish/.env << EOL
IMAGE_DIR=/var/www/peni.sh/images
ENVIRONMENT=production
OPENAI_API_KEY=\${{ secrets.OPENAI_API_KEY }}
OPENAI_MODEL=gpt-4
EOL"
        
          # Copy files to production directory
          sudo cp main.py /opt/penish/ 2>/dev/null || echo "main.py not found, skipping"
          sudo cp requirements.txt /opt/penish/ 2>/dev/null || echo "requirements.txt not found, skipping"
          sudo cp nginx.conf /etc/nginx/sites-available/peni.sh 2>/dev/null || echo "nginx.conf not found, skipping"
          
          # Install/update Python dependencies
          sudo -u penish /opt/penish/venv/bin/pip install -r /opt/penish/requirements.txt
          
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
        sleep 10
        
        # Test health endpoint
        if curl -s https://${SERVER_HOST}/health | grep -q "healthy"; then
          echo "✅ Health check passed!"
        else
          echo "❌ Health check failed!"
          exit 1
        fi
        
        # Test WiFi API endpoint
        if curl -s https://${SERVER_HOST}/api/wifi | grep -q "ssid"; then
          echo "✅ WiFi API is working!"
        else
          echo "❌ WiFi API failed!"
          exit 1
        fi
EOF
    
    log "Created GitHub Actions workflow file"
}

setup_server_keys() {
    log "Setting up SSH keys for deployment..."
    
    # Generate SSH key for GitHub Actions
    SSH_KEY_PATH="/tmp/github_actions_key"
    ssh-keygen -t ed25519 -f "$SSH_KEY_PATH" -N "" -q
    
    info "SSH keys generated"
    echo
    echo -e "${YELLOW}🔑 IMPORTANT: Add these to your GitHub repository:${NC}"
    echo
    echo -e "${BLUE}1. Go to: https://github.com/${GITHUB_USER}/${REPO_NAME}/settings/keys${NC}"
    echo -e "${BLUE}2. Add this as a Deploy Key:${NC}"
    echo "=========================="
    cat "${SSH_KEY_PATH}.pub"
    echo "=========================="
    echo
    
    echo -e "${BLUE}3. Go to: https://github.com/${GITHUB_USER}/${REPO_NAME}/settings/secrets/actions${NC}"
    echo -e "${BLUE}4. Add these Repository Secrets:${NC}"
    echo
    echo "SECRET NAME: HOST"
    echo "VALUE: ${SERVER_HOST}"
    echo
    echo "SECRET NAME: USERNAME" 
    echo "VALUE: ${SERVER_USER}"
    echo
    echo "SECRET NAME: OPENAI_API_KEY"
    echo "VALUE: ${OPENAI_API_KEY}"
    echo
    echo "SECRET NAME: SSH_KEY"
    echo "VALUE:"
    echo "=========================="
    cat "${SSH_KEY_PATH}"
    echo "=========================="
    echo
    
    read -p "Press ENTER when you've added all the secrets to GitHub..."
    
    # Clean up temporary keys
    rm -f "${SSH_KEY_PATH}" "${SSH_KEY_PATH}.pub"
}

setup_server_repo() {
    log "Setting up repository directory on server..."
    
    ssh "${SERVER_USER}@${SERVER_HOST}" << EOF
        # Create repo directory if it doesn't exist
        if [ ! -d "/opt/penish-repo" ]; then
            sudo mkdir -p /opt/penish-repo
            sudo chown \$(whoami):\$(whoami) /opt/penish-repo
            echo "✅ Created /opt/penish-repo directory"
        else
            echo "✅ /opt/penish-repo already exists"
        fi
        
        # Ensure penish user owns the penish directory
        sudo chown -R penish:penish /opt/penish /var/www/peni.sh
        echo "✅ Set correct ownership for penish directories"
EOF
    
    log "Server setup completed"
}

push_and_test() {
    log "Pushing code and testing deployment..."
    
    # Add and commit the workflow file
    git add .github/workflows/deploy.yml
    git commit -m "🔧 Add GitHub Actions CI/CD workflow"
    
    # Push to GitHub
    git push -u origin main
    
    log "Code pushed to GitHub!"
    
    echo
    info "🎉 CI/CD Pipeline Setup Complete!"
    echo
    echo -e "${GREEN}Next steps:${NC}"
    echo "1. Check GitHub Actions: https://github.com/${GITHUB_USER}/${REPO_NAME}/actions"
    echo "2. Monitor deployment progress"
    echo "3. Test your site: https://${SERVER_HOST}"
    echo
    echo -e "${BLUE}Future deployments are now automatic:${NC}"
    echo "  git commit -m 'Your changes'"
    echo "  git push origin main"
    echo "  # 🚀 Automatic deployment triggered!"
    echo
}

create_helper_scripts() {
    log "Creating helper scripts..."
    
    # Create quick deploy script
    cat > quick-deploy.sh << 'EOF'
#!/bin/bash
# Quick deployment script

echo "🚀 Quick Deploy to peni.sh"
echo "=========================="

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    echo "📝 You have uncommitted changes:"
    git status --porcelain
    echo
    read -p "Commit and deploy? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        read -p "Commit message: " commit_msg
        git add .
        git commit -m "$commit_msg"
    else
        echo "❌ Deployment cancelled"
        exit 1
    fi
fi

echo "🚀 Pushing to GitHub..."
git push origin main

echo "✅ Deployment triggered! Check GitHub Actions for progress."
echo "🌐 Your site: https://peni.sh"
EOF
    
    chmod +x quick-deploy.sh
    
    # Create status check script
    cat > check-status.sh << EOF
#!/bin/bash
# Check deployment status

echo "🔍 Checking peni.sh Status"
echo "=========================="

# Check if site is responding
if curl -s https://${SERVER_HOST}/health | grep -q "healthy"; then
    echo "✅ Site is healthy"
else
    echo "❌ Site health check failed"
fi

# Check WiFi API
if curl -s https://${SERVER_HOST}/api/wifi | grep -q "ssid"; then
    echo "✅ WiFi API is working"
else
    echo "❌ WiFi API failed"
fi

# Check GitHub Actions status
echo "📊 Latest GitHub Actions: https://github.com/${GITHUB_USER}/${REPO_NAME}/actions"
EOF
    
    chmod +x check-status.sh
    
    log "Created helper scripts: quick-deploy.sh and check-status.sh"
}

main() {
    print_banner
    collect_config
    setup_git_repo
    create_github_actions
    setup_server_keys
    setup_server_repo
    create_helper_scripts
    push_and_test
}

# Run main function
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi