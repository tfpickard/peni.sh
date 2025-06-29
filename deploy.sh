#!/bin/bash
# deploy.sh - Deployment script for peni.sh
# Run with: sudo ./deploy.sh

set -euo pipefail

# Configuration
DOMAIN="peni.sh"
APP_DIR="/opt/penish"
IMAGES_DIR="/var/www/peni.sh/images"
USER="penish"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="penish"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING: $1${NC}"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $1${NC}"
    exit 1
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root"
    fi
}

detect_package_manager() {
    if command -v apt-get &> /dev/null; then
        PKG_INSTALL="apt-get install -y"
        PKG_UPDATE="apt-get update"
    elif command -v paru &> /dev/null; then
        PKG_INSTALL="paru -S --noconfirm"
        PKG_UPDATE="paru -Sy"
    elif command -v pacman &> /dev/null; then
        PKG_INSTALL="pacman -S --noconfirm"
        PKG_UPDATE="pacman -Sy"
    else
        error "No supported package manager found (apt-get or pacman/paru)"
    fi
}

install_dependencies() {
    log "Installing system dependencies..."
    $PKG_UPDATE
    
    # Common packages
    $PKG_INSTALL nginx certbot python3 python3-pip python3-venv
    
    # Platform-specific packages
    if command -v apt-get &> /dev/null; then
        $PKG_INSTALL python3-certbot-nginx
    elif command -v pacman &> /dev/null; then
        $PKG_INSTALL certbot-nginx
    fi
}

create_user() {
    log "Creating application user..."
    if ! id "$USER" &>/dev/null; then
        useradd -r -s /bin/false -d "$APP_DIR" "$USER"
        log "Created user: $USER"
    else
        log "User $USER already exists"
    fi
}

setup_directories() {
    log "Setting up directories..."
    
    # Application directory
    mkdir -p "$APP_DIR"
    chown "$USER:$USER" "$APP_DIR"
    
    # Images directory
    mkdir -p "$IMAGES_DIR"
    chown "$USER:$USER" "$IMAGES_DIR"
    
    # Log directory
    mkdir -p "/var/log/$SERVICE_NAME"
    chown "$USER:$USER" "/var/log/$SERVICE_NAME"
    
    log "Directories created and permissions set"
}

setup_python_env() {
    log "Setting up Python virtual environment..."
    
    # Create virtual environment as the app user
    sudo -u "$USER" python3 -m venv "$VENV_DIR"
    
    # Install Python dependencies
    sudo -u "$USER" "$VENV_DIR/bin/pip" install --upgrade pip
    
    if [[ -f "requirements.txt" ]]; then
        sudo -u "$USER" "$VENV_DIR/bin/pip" install -r requirements.txt
        log "Installed Python dependencies from requirements.txt"
    else
        warn "requirements.txt not found, installing basic dependencies"
        sudo -u "$USER" "$VENV_DIR/bin/pip" install fastapi uvicorn python-multipart jinja2 aiofiles pillow pydantic
    fi
}

install_application() {
    log "Installing application files..."
    
    # Copy application files
    if [[ -f "main.py" ]]; then
        cp main.py "$APP_DIR/"
        chown "$USER:$USER" "$APP_DIR/main.py"
        log "Copied main.py"
    else
        error "main.py not found in current directory"
    fi
    
    # Create environment file
    cat > "$APP_DIR/.env" << EOF
IMAGE_DIR=$IMAGES_DIR
ENVIRONMENT=production
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_MODEL=${OPENAI_MODEL:-gpt-4}
EOF
    chown "$USER:$USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"  # Protect API key
    
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
        warn "OPENAI_API_KEY not set. You'll need to add it to $APP_DIR/.env"
        warn "Example: echo 'OPENAI_API_KEY=sk-...' | sudo tee -a $APP_DIR/.env"
    fi
}

create_systemd_service() {
    log "Creating systemd service..."
    
    cat > "/etc/systemd/system/$SERVICE_NAME.service" << EOF
[Unit]
Description=peni.sh FastAPI application
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR
Environment=PATH=$VENV_DIR/bin
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 4
Restart=always
RestartSec=10

# Security
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR $IMAGES_DIR /var/log/$SERVICE_NAME

# Logging
StandardOutput=append:/var/log/$SERVICE_NAME/app.log
StandardError=append:/var/log/$SERVICE_NAME/error.log

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    log "Systemd service created and enabled"
}

configure_nginx() {
    log "Configuring nginx..."
    
    # Generate DH parameters if they don't exist
    if [[ ! -f /etc/nginx/dhparam.pem ]]; then
        log "Generating DH parameters (this may take a while)..."
        openssl dhparam -out /etc/nginx/dhparam.pem 2048
    fi
    
    # Add rate limiting to main nginx.conf if not already present
    if ! grep -q "limit_req_zone.*zone=api" /etc/nginx/nginx.conf; then
        log "Adding rate limiting configuration to nginx.conf..."
        sed -i '/http {/a\\n\t# Rate limiting zones for peni.sh\n\tlimit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;\n\tlimit_req_zone $binary_remote_addr zone=images:10m rate=30r/s;' /etc/nginx/nginx.conf
    fi
    
    # Create webroot directory for certbot challenges
    mkdir -p /var/www/letsencrypt
    chown www-data:www-data /var/www/letsencrypt 2>/dev/null || chown nginx:nginx /var/www/letsencrypt 2>/dev/null || true
    
    # Create temporary HTTP-only configuration for domain verification
    log "Creating temporary HTTP-only configuration..."
    cat > "/etc/nginx/sites-available/$DOMAIN" << EOF
server {
    listen 80;
    server_name $DOMAIN;
    
    # Allow certbot domain verification
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        try_files \$uri =404;
    }
    
    # Proxy other requests to the app (for health checks)
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF
    
    # Enable site
    ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/"
    
    # Remove default site if it exists
    rm -f /etc/nginx/sites-enabled/default
    
    # Test nginx configuration
    nginx -t || error "Nginx configuration test failed"
    
    log "Temporary nginx configuration created successfully"
}

setup_ssl() {
    log "Setting up SSL certificate..."
    
    # Start nginx with temporary configuration
    systemctl restart nginx
    
    # Wait a moment for nginx to start
    sleep 2
    
    # Test that the domain is accessible
    log "Testing domain accessibility..."
    if ! curl -s -o /dev/null -w "%{http_code}" "http://$DOMAIN" | grep -q "200\|502\|503"; then
        warn "Domain $DOMAIN may not be properly configured. Continuing anyway..."
    fi
    
    # Get certificate for just the main domain (not www subdomain)
    log "Requesting SSL certificate for $DOMAIN..."
    if certbot certonly --webroot -w /var/www/letsencrypt -d "$DOMAIN" --non-interactive --agree-tos --email "admin@$DOMAIN" --no-eff-email; then
        log "SSL certificate obtained successfully"
    else
        error "Failed to obtain SSL certificate. Check that $DOMAIN points to this server's IP address."
    fi
    
    # Now create the full HTTPS configuration
    log "Creating full HTTPS nginx configuration..."
    if [[ -f "nginx.conf" ]]; then
        # Update the nginx.conf template to only use the main domain
        sed "s/server_name peni.sh www.peni.sh;/server_name $DOMAIN;/g" nginx.conf > "/etc/nginx/sites-available/$DOMAIN"
        
        # Enable rate limiting in the site config
        sed -i 's/# limit_req zone=api/limit_req zone=api/' "/etc/nginx/sites-available/$DOMAIN"
        sed -i 's/# limit_req zone=images/limit_req zone=images/' "/etc/nginx/sites-available/$DOMAIN"
    else
        error "nginx.conf not found"
    fi
    
    # Test the new configuration
    nginx -t || error "HTTPS nginx configuration test failed"
    
    # Setup auto-renewal
    systemctl enable certbot.timer
    systemctl start certbot.timer
    
    log "SSL certificate obtained and nginx configured for HTTPS"
}

start_services() {
    log "Starting services..."
    
    # Start application
    systemctl start "$SERVICE_NAME"
    systemctl status "$SERVICE_NAME" --no-pager
    
    # Start nginx
    systemctl restart nginx
    systemctl status nginx --no-pager
    
    log "Services started successfully"
}

create_sample_images() {
    log "Creating sample images directory structure..."
    
    # Create a sample image if none exist
    if [[ ! "$(ls -A $IMAGES_DIR)" ]]; then
        warn "No images found in $IMAGES_DIR"
        warn "Add some images to $IMAGES_DIR for the site to display"
        
        # Create a placeholder text file
        echo "Add your images to this directory" > "$IMAGES_DIR/README.txt"
        chown "$USER:$USER" "$IMAGES_DIR/README.txt"
    fi
}

show_status() {
    log "Deployment complete!"
    echo
    echo "Services status:"
    systemctl is-active "$SERVICE_NAME" && echo "✓ $SERVICE_NAME is running" || echo "✗ $SERVICE_NAME is not running"
    systemctl is-active nginx && echo "✓ nginx is running" || echo "✗ nginx is not running"
    systemctl is-active certbot.timer && echo "✓ certbot auto-renewal is active" || echo "✗ certbot auto-renewal is not active"
    echo
    echo "URLs:"
    echo "  Main site: https://$DOMAIN"
    echo "  API docs: https://$DOMAIN/api/docs"
    echo "  WiFi endpoint: https://$DOMAIN/api/wifi"
    echo
    echo "Configuration:"
    echo "  Images directory: $IMAGES_DIR"
    echo "  Application directory: $APP_DIR"
    echo "  Environment file: $APP_DIR/.env"
    echo
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
        echo "⚠️  IMPORTANT: Set your OpenAI API key:"
        echo "  sudo nvim $APP_DIR/.env"
        echo "  Add: OPENAI_API_KEY=sk-your-key-here"
        echo "  Then: sudo systemctl restart $SERVICE_NAME"
        echo
    fi
    echo "Logs:"
    echo "  Application: journalctl -u $SERVICE_NAME -f"
    echo "  Nginx: tail -f /var/log/nginx/${DOMAIN}_error.log"
}

main() {
    log "Starting deployment of peni.sh..."
    
    check_root
    detect_package_manager
    install_dependencies
    create_user
    setup_directories
    setup_python_env
    install_application
    create_systemd_service
    configure_nginx
    setup_ssl
    create_sample_images
    start_services
    show_status
}

# Run main function if script is executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi