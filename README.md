# 🚀 peni.sh - The Revolutionary AI-Powered WiFi Credential Generator That Will Change Your Life Forever™

**Welcome to the future of wireless network authentication and random image appreciation.**

🌐 **[EXPERIENCE THE MAGIC: https://peni.sh](https://peni.sh)** 🌐

---

## 🎯 What Is This Magnificent Beast?

peni.sh is not just another web application. It's a **paradigm-shifting, life-altering, universe-bending** platform that solves humanity's two most pressing problems:

1. **The Existential Crisis of WiFi Naming**: No more `Linksys_12345` or `MyWiFi123`. Our AI generates SSIDs so creative, so memorable, so *chef's kiss* perfect that your neighbors will weep with envy.

2. **The Profound Emptiness of Predictable Passwords**: Using cutting-edge artificial intelligence (yes, the same technology that will probably enslave us all), we create passwords that are both **impossible to guess** and **impossible to forget** if you know the secret pattern.

3. **The Soul-Crushing Boredom of Blank Browser Tabs**: Feast your eyes upon randomly selected images that will either inspire you to greatness or question your life choices. We provide no middle ground.

## 🔥 Features That Will Blow Your Mind

- 🧠 **AI-Powered SSID Generation**: Our OpenAI integration doesn't just create network names—it births digital poetry
- 🎲 **Cryptographically Memorable Passwords**: Easy to derive, hard to crack, impossible to explain to your grandmother
- 🖼️ **Quantum Random Image Display**: Because sometimes you need to see a random picture to remember why you're alive
- 📤 **Secure Image Upload System**: Drag-and-drop your way to image glory with token-based auth
- 🎨 **Automatic Thumbnail Generation**: 300x300 pixel perfection, automatically created
- 🗄️ **Database-Backed Metadata**: SQLite tracking for all your image management needs
- 🔒 **Military-Grade HTTPS**: Your WiFi credentials are protected like state secrets
- ⚡ **Lightning-Fast API**: Faster than your ability to come up with excuses for bad WiFi names
- 🎨 **Retro Terminal Aesthetic**: Green text on black background because we're not animals

## 🚀 API Examples That Will Change Your Developer Life

### 🌊 curl (For the Command Line Warriors)

```bash
# Get WiFi credentials that will make you question reality
curl -s https://peni.sh/api/wifi | jq '.'

# Example response that will bring tears to your eyes:
{
  "ssid": "QuantumBurritoSupreme77",
  "password": "qbs77!",
  "hint": "First letters of each word + numbers + exclamation of joy"
}

# List all available images (prepare for disappointment or enlightenment)
curl -s https://peni.sh/api/images | jq '.'

# Health check (because we care about your emotional wellbeing)
curl -s https://peni.sh/health
```

### 🐍 Python (For the Intellectually Superior)

```python
import requests
import json
from datetime import datetime

class WiFiCredentialGenerator:
    """A class so powerful it should require a license."""
    
    def __init__(self):
        self.base_url = "https://peni.sh"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'peni.sh-python-client/1.0 (changing-the-world)'
        })
    
    def get_life_changing_credentials(self):
        """Generate WiFi credentials that will revolutionize your network."""
        try:
            response = self.session.get(f"{self.base_url}/api/wifi")
            response.raise_for_status()
            
            creds = response.json()
            print(f"🎉 BEHOLD: Your new WiFi identity!")
            print(f"📡 SSID: {creds['ssid']}")
            print(f"🔐 Password: {creds['password']}")
            print(f"💡 Hint: {creds['hint']}")
            
            return creds
        except Exception as e:
            print(f"💀 The universe has rejected your request: {e}")
            return None
    
    def get_random_enlightenment(self):
        """Retrieve a list of images for spiritual awakening."""
        response = self.session.get(f"{self.base_url}/api/images")
        return response.json()

# Usage example that will change your life
if __name__ == "__main__":
    generator = WiFiCredentialGenerator()
    
    # Generate 5 WiFi networks for your expanding empire
    for i in range(5):
        print(f"\n--- Network #{i+1} for World Domination ---")
        generator.get_life_changing_credentials()
```

### 🌟 JavaScript (For the Frontend Philosophers)

```javascript
class PenishClient {
    constructor() {
        this.baseUrl = 'https://peni.sh';
        this.userAgent = 'peni.sh-js-client/1.0 (web-developer-extraordinaire)';
    }

    /**
     * Generate WiFi credentials so beautiful they belong in a museum
     */
    async generateEpicWiFiCredentials() {
        try {
            const response = await fetch(`${this.baseUrl}/api/wifi`, {
                headers: {
                    'User-Agent': this.userAgent
                }
            });
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: The API has rejected your existence`);
            }
            
            const credentials = await response.json();
            
            // Display with the dramatic flair it deserves
            console.log('🎊 ATTENTION: New WiFi credentials have arrived!');
            console.table(credentials);
            
            return credentials;
        } catch (error) {
            console.error('💥 Catastrophic failure:', error.message);
            throw error;
        }
    }

    /**
     * Create a WiFi setup widget that will blow minds
     */
    async createWiFiWidget(containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            throw new Error('Container not found. How do you expect miracles without a proper vessel?');
        }

        const credentials = await this.generateEpicWiFiCredentials();
        
        container.innerHTML = `
            <div style="
                background: linear-gradient(45deg, #000, #003300);
                color: #00ff00;
                padding: 20px;
                border: 2px solid #00ff00;
                font-family: 'Courier New', monospace;
                text-align: center;
                border-radius: 10px;
                box-shadow: 0 0 20px #00ff00;
            ">
                <h2>🛜 NETWORK CREDENTIALS GENERATED</h2>
                <p><strong>SSID:</strong> ${credentials.ssid}</p>
                <p><strong>Password:</strong> ${credentials.password}</p>
                <p><strong>Hint:</strong> ${credentials.hint}</p>
                <button onclick="location.reload()" style="
                    background: #00ff00;
                    color: #000;
                    border: none;
                    padding: 10px 20px;
                    font-family: inherit;
                    cursor: pointer;
                    margin-top: 10px;
                ">🎲 GENERATE MORE MAGIC</button>
            </div>
        `;
    }
}

// Usage example for the brave
const client = new PenishClient();

// Generate credentials when the universe is ready
document.addEventListener('DOMContentLoaded', async () => {
    await client.createWiFiWidget('wifi-container');
});
```

### 🔥 Go (For the Performance Obsessed)

```go
package main

import (
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "time"
)

// WiFiCredentials represents the divine response from the API
type WiFiCredentials struct {
    SSID     string `json:"ssid"`
    Password string `json:"password"`
    Hint     string `json:"hint"`
}

// PenishClient is your gateway to networking nirvana
type PenishClient struct {
    BaseURL    string
    HTTPClient *http.Client
}

// NewPenishClient creates a client worthy of the task ahead
func NewPenishClient() *PenishClient {
    return &PenishClient{
        BaseURL: "https://peni.sh",
        HTTPClient: &http.Client{
            Timeout: 30 * time.Second,
            Transport: &http.Transport{
                MaxIdleConns:       10,
                IdleConnTimeout:    30 * time.Second,
                DisableCompression: false,
            },
        },
    }
}

// GenerateWiFiCredentials retrieves credentials that will change everything
func (c *PenishClient) GenerateWiFiCredentials() (*WiFiCredentials, error) {
    req, err := http.NewRequest("GET", c.BaseURL+"/api/wifi", nil)
    if err != nil {
        return nil, fmt.Errorf("failed to create request: %w", err)
    }
    
    req.Header.Set("User-Agent", "peni.sh-go-client/1.0 (go-developer-supreme)")
    
    resp, err := c.HTTPClient.Do(req)
    if err != nil {
        return nil, fmt.Errorf("request failed catastrophically: %w", err)
    }
    defer resp.Body.Close()
    
    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("API rejected our offering with status %d", resp.StatusCode)
    }
    
    body, err := io.ReadAll(resp.Body)
    if err != nil {
        return nil, fmt.Errorf("failed to read the sacred response: %w", err)
    }
    
    var credentials WiFiCredentials
    if err := json.Unmarshal(body, &credentials); err != nil {
        return nil, fmt.Errorf("failed to decode divine message: %w", err)
    }
    
    return &credentials, nil
}

func main() {
    client := NewPenishClient()
    
    fmt.Println("🚀 Initiating WiFi credential generation sequence...")
    
    // Generate multiple credentials for your networking empire
    for i := 0; i < 3; i++ {
        fmt.Printf("\n--- Generating Network #%d ---\n", i+1)
        
        credentials, err := client.GenerateWiFiCredentials()
        if err != nil {
            fmt.Printf("💀 Error: %v\n", err)
            continue
        }
        
        fmt.Printf("📡 SSID: %s\n", credentials.SSID)
        fmt.Printf("🔐 Password: %s\n", credentials.Password)
        fmt.Printf("💡 Hint: %s\n", credentials.Hint)
        fmt.Println("✨ Credentials acquired successfully!")
        
        // Pause for dramatic effect
        time.Sleep(1 * time.Second)
    }
    
    fmt.Println("\n🎉 Mission accomplished! Your networks are now ready for greatness!")
}
```

### ⚡ C (For the Hardcore Legends)

```c
/*
 * peni.sh C Client - Because even C developers deserve beautiful WiFi names
 * Compile with: gcc -o penish_client penish_client.c -lcurl -ljson-c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <curl/curl.h>
#include <json-c/json.h>

#define PENISH_API_URL "https://peni.sh/api/wifi"
#define BUFFER_SIZE 4096

// Structure to hold our divine response
struct APIResponse {
    char* data;
    size_t size;
};

// Structure for WiFi credentials that will change your life
typedef struct {
    char ssid[256];
    char password[256];
    char hint[512];
} wifi_credentials_t;

/**
 * Callback function to receive the sacred data from the API
 */
static size_t WriteMemoryCallback(void *contents, size_t size, size_t nmemb, struct APIResponse *response) {
    size_t realsize = size * nmemb;
    char *ptr = realloc(response->data, response->size + realsize + 1);
    
    if (!ptr) {
        printf("💀 Memory allocation failed! The universe is against us!\n");
        return 0;
    }
    
    response->data = ptr;
    memcpy(&(response->data[response->size]), contents, realsize);
    response->size += realsize;
    response->data[response->size] = 0;
    
    return realsize;
}

/**
 * Parse the JSON response and extract WiFi credentials
 */
int parse_wifi_credentials(const char *json_string, wifi_credentials_t *creds) {
    json_object *root = json_tokener_parse(json_string);
    if (!root) {
        printf("💥 Failed to parse JSON response. The API speaks in tongues!\n");
        return -1;
    }
    
    json_object *ssid_obj, *password_obj, *hint_obj;
    
    // Extract SSID
    if (json_object_object_get_ex(root, "ssid", &ssid_obj)) {
        strncpy(creds->ssid, json_object_get_string(ssid_obj), sizeof(creds->ssid) - 1);
        creds->ssid[sizeof(creds->ssid) - 1] = '\0';
    }
    
    // Extract Password
    if (json_object_object_get_ex(root, "password", &password_obj)) {
        strncpy(creds->password, json_object_get_string(password_obj), sizeof(creds->password) - 1);
        creds->password[sizeof(creds->password) - 1] = '\0';
    }
    
    // Extract Hint
    if (json_object_object_get_ex(root, "hint", &hint_obj)) {
        strncpy(creds->hint, json_object_get_string(hint_obj), sizeof(creds->hint) - 1);
        creds->hint[sizeof(creds->hint) - 1] = '\0';
    }
    
    json_object_put(root);
    return 0;
}

/**
 * Generate WiFi credentials that will revolutionize your network
 */
int generate_wifi_credentials(wifi_credentials_t *creds) {
    CURL *curl;
    CURLcode res;
    struct APIResponse response = {0};
    
    curl = curl_easy_init();
    if (!curl) {
        printf("💀 cURL initialization failed! The networking gods are displeased!\n");
        return -1;
    }
    
    curl_easy_setopt(curl, CURLOPT_URL, PENISH_API_URL);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteMemoryCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, (void *)&response);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "peni.sh-c-client/1.0 (c-developer-legend)");
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    
    res = curl_easy_perform(curl);
    
    if (res != CURLE_OK) {
        printf("💥 cURL request failed: %s\n", curl_easy_strerror(res));
        curl_easy_cleanup(curl);
        if (response.data) free(response.data);
        return -1;
    }
    
    long response_code;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response_code);
    
    if (response_code != 200) {
        printf("💀 API returned status code: %ld\n", response_code);
        curl_easy_cleanup(curl);
        if (response.data) free(response.data);
        return -1;
    }
    
    // Parse the response
    int parse_result = parse_wifi_credentials(response.data, creds);
    
    curl_easy_cleanup(curl);
    if (response.data) free(response.data);
    
    return parse_result;
}

int main(void) {
    printf("🚀 peni.sh C Client - Preparing to Generate Network Greatness\n");
    printf("=" * 60);
    printf("\n");
    
    // Initialize cURL globally
    curl_global_init(CURL_GLOBAL_DEFAULT);
    
    wifi_credentials_t creds;
    
    // Generate multiple sets of credentials for maximum impact
    for (int i = 1; i <= 3; i++) {
        printf("🎯 Generating WiFi Credentials Set #%d\n", i);
        printf("-" * 40);
        printf("\n");
        
        memset(&creds, 0, sizeof(creds));
        
        if (generate_wifi_credentials(&creds) == 0) {
            printf("📡 SSID: %s\n", creds.ssid);
            printf("🔐 Password: %s\n", creds.password);
            printf("💡 Hint: %s\n", creds.hint);
            printf("✨ Success! Credentials acquired!\n\n");
        } else {
            printf("💀 Failed to generate credentials. Try again later.\n\n");
        }
        
        // Dramatic pause
        sleep(1);
    }
    
    printf("🎉 Mission Complete! Your C program has successfully communicated with the future!\n");
    
    curl_global_cleanup();
    return 0;
}
```

### 🦀 Rust (For the Memory-Safe Elite)

```rust
//! peni.sh Rust Client - Because safety and WiFi names go hand in hand
//! 
//! Add to Cargo.toml:
//! [dependencies]
//! tokio = { version = "1.0", features = ["full"] }
//! reqwest = { version = "0.11", features = ["json"] }
//! serde = { version = "1.0", features = ["derive"] }
//! anyhow = "1.0"
//! colored = "2.0"

use anyhow::{Result, anyhow};
use colored::*;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::time::Duration;

/// The sacred structure containing WiFi enlightenment
#[derive(Debug, Deserialize, Serialize, Clone)]
struct WiFiCredentials {
    ssid: String,
    password: String,
    hint: String,
}

/// Client for accessing the divine peni.sh API
#[derive(Debug)]
pub struct PenishClient {
    client: Client,
    base_url: String,
}

impl PenishClient {
    /// Create a new client prepared for greatness
    pub fn new() -> Result<Self> {
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("peni.sh-rust-client/1.0 (rust-developer-supreme)")
            .build()
            .map_err(|e| anyhow!("Failed to create HTTP client: {}", e))?;
        
        Ok(Self {
            client,
            base_url: "https://peni.sh".to_string(),
        })
    }
    
    /// Generate WiFi credentials that will revolutionize your network
    pub async fn generate_wifi_credentials(&self) -> Result<WiFiCredentials> {
        let url = format!("{}/api/wifi", self.base_url);
        
        let response = self.client
            .get(&url)
            .send()
            .await
            .map_err(|e| anyhow!("Request failed catastrophically: {}", e))?;
        
        if !response.status().is_success() {
            return Err(anyhow!("API rejected our request with status: {}", response.status()));
        }
        
        let credentials = response
            .json::<WiFiCredentials>()
            .await
            .map_err(|e| anyhow!("Failed to decode divine response: {}", e))?;
        
        Ok(credentials)
    }
    
    /// Display credentials with the dramatic flair they deserve
    pub fn display_credentials(&self, creds: &WiFiCredentials) {
        println!("{}", "🎊 NEW WIFI CREDENTIALS GENERATED!".bright_green().bold());
        println!("{}", "═".repeat(50).bright_green());
        println!("{} {}", "📡 SSID:".bright_cyan().bold(), creds.ssid.bright_white().bold());
        println!("{} {}", "🔐 Password:".bright_cyan().bold(), creds.password.bright_yellow().bold());
        println!("{} {}", "💡 Hint:".bright_cyan().bold(), creds.hint.bright_magenta());
        println!("{}", "═".repeat(50).bright_green());
        println!();
    }
}

/// Generate multiple WiFi networks for your expanding digital empire
async fn generate_network_empire(client: &PenishClient, count: usize) -> Result<Vec<WiFiCredentials>> {
    let mut networks = Vec::new();
    
    println!("{}", format!("🚀 Generating {} WiFi Networks for World Domination", count)
        .bright_green().bold());
    println!();
    
    for i in 1..=count {
        println!("{}", format!("--- Generating Network #{} ---", i).bright_blue().bold());
        
        match client.generate_wifi_credentials().await {
            Ok(creds) => {
                client.display_credentials(&creds);
                networks.push(creds);
                
                println!("{}", "✨ Network successfully added to your empire!".bright_green());
            }
            Err(e) => {
                println!("{}", format!("💀 Error generating network #{}: {}", i, e).bright_red().bold());
            }
        }
        
        // Dramatic pause for effect
        tokio::time::sleep(Duration::from_millis(1000)).await;
        println!();
    }
    
    Ok(networks)
}

/// The main function that will change your life
#[tokio::main]
async fn main() -> Result<()> {
    println!("{}", "🦀 peni.sh Rust Client - Memory-Safe WiFi Generation".bright_cyan().bold());
    println!("{}", "═".repeat(60).bright_cyan());
    println!();
    
    let client = PenishClient::new()
        .map_err(|e| anyhow!("Failed to initialize client: {}", e))?;
    
    // Generate an empire of 5 networks
    let networks = generate_network_empire(&client, 5).await?;
    
    // Summary of your digital empire
    println!("{}", "🎉 EMPIRE GENERATION COMPLETE!".bright_green().bold());
    println!("{}", format!("Successfully generated {} networks for your digital dominion", networks.len())
        .bright_white());
    
    println!("\n{}", "📊 Your Network Empire:".bright_yellow().bold());
    for (i, network) in networks.iter().enumerate() {
        println!("  {}. {} → {}", 
            (i + 1).to_string().bright_blue(), 
            network.ssid.bright_white(), 
            network.password.bright_yellow()
        );
    }
    
    println!("\n{}", "🌟 Go forth and spread the wireless revolution!".bright_magenta().bold());
    
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    
    #[tokio::test]
    async fn test_client_creation() {
        let client = PenishClient::new();
        assert!(client.is_ok(), "Client creation should not fail in this reality");
    }
    
    #[tokio::test]
    async fn test_credential_generation() {
        let client = PenishClient::new().expect("Client should initialize");
        let result = client.generate_wifi_credentials().await;
        
        match result {
            Ok(creds) => {
                assert!(!creds.ssid.is_empty(), "SSID should not be empty");
                assert!(!creds.password.is_empty(), "Password should not be empty");
                println!("✅ Test passed! Generated: {} → {}", creds.ssid, creds.password);
            }
            Err(e) => {
                println!("⚠️  Test couldn't complete due to network/API issues: {}", e);
                // In a real test environment, you might want to assert!(false) here
            }
        }
    }
}
```

## 📤 Image Upload System - NEW!

### Upload Your Way to Visual Greatness

Navigate to **`https://peni.sh/admin`** to access the admin panel where dreams become JPEGs.

### Features That Make Other Upload Systems Weep

- **🎯 Drag & Drop Interface**: Just drag your images into the zone of enlightenment
- **🔐 Token-Based Security**: No unauthorized uploads allowed in this house
- **🎨 Automatic Thumbnails**: 300x300 pixel perfection, created on-the-fly
- **🛡️ EXIF Stripping**: Your location metadata stays private, unlike your social media posts
- **✨ Magic Byte Validation**: We check the actual file content, not just the extension
- **🔄 Deduplication**: SHA256 hashing prevents you from uploading the same meme twice
- **📊 Metadata Tracking**: Upload dates, dimensions, file sizes—we got it all

### Quick Start for the Impatient

```bash
# 1. Get your upload token (shown after deployment)
sudo grep UPLOAD_TOKEN /opt/penish/.env

# 2. Open the admin panel
open https://peni.sh/admin

# 3. Enter your token and start uploading!
```

### Upload via API (For the Command Line Elite)

```bash
# Upload an image like a boss
curl -X POST https://peni.sh/api/upload \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -F "file=@my-awesome-image.jpg"

# Delete an image (with great power comes great responsibility)
curl -X DELETE https://peni.sh/api/images/my-awesome-image.jpg \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"

# List all your glorious images
curl https://peni.sh/api/images | jq '.'

# Get a thumbnail (because bandwidth matters)
curl https://peni.sh/thumbnail/my-awesome-image.jpg -o thumb.jpg
```

### Security Features That Would Make Fort Knox Jealous

- **Token Authentication**: All uploads/deletes require your secret token
- **Magic Byte Detection**: Fake file extensions detected and rejected
- **File Size Limits**: Default 10MB max (configurable for your massive meme collection)
- **Path Traversal Protection**: Nice try, hacker. Not today.
- **EXIF Stripping**: Location data removed automatically for privacy
- **HTTPS Only**: Because we're not savages

**📖 For detailed documentation, see [UPLOAD_GUIDE.md](UPLOAD_GUIDE.md)**

---

## 🏗️ Deployment Instructions for Maximum Impact

### Prerequisites for Greatness
- A server (preferably one that hasn't lost hope)
- Domain pointing to said server (`peni.sh` in our case)
- OpenAI API key (the key to the universe)
- Basic understanding of Linux (or willingness to learn through suffering)

### One-Command World Domination
```bash
export OPENAI_API_KEY=sk-your-key-of-infinite-wisdom
sudo -E ./deploy.sh
```

### Manual Installation (For the Brave)
```bash
# Install dependencies (Ubuntu/Debian)
sudo apt update && sudo apt install nginx certbot python3-certbot-nginx python3 python3-pip python3-venv

# Install dependencies (Arch Linux - for the elite)
sudo paru -S nginx certbot certbot-nginx python python-pip

# Create the sacred directories
sudo mkdir -p /opt/penish /var/www/peni.sh/images
sudo useradd -r -s /bin/false -d /opt/penish penish
sudo chown -R penish:penish /opt/penish /var/www/peni.sh/images

# Deploy the chosen code
cd /opt/penish
sudo -u penish python3 -m venv venv
sudo -u penish venv/bin/pip install fastapi uvicorn openai python-multipart jinja2 aiofiles pillow pydantic

# Configure nginx and SSL (prepare for enlightenment)
sudo cp nginx.conf /etc/nginx/sites-available/peni.sh
sudo ln -s /etc/nginx/sites-available/peni.sh /etc/nginx/sites-enabled/
sudo certbot --nginx -d peni.sh

# Start the revolution
sudo systemctl enable --now penish nginx
```

## 🎭 Use Cases That Will Blow Your Mind

### 🏠 **Home Network Transformation**
Transform your boring home WiFi from "Linksys_Default" to "QuantumBurritoSupreme77" and watch your neighbors question their life choices.

### 🏢 **Corporate Environments** 
Replace "OFFICE_WIFI_2024" with "SynergyNinjaParadox42" and become the hero your workplace didn't know it needed.

### ☕ **Coffee Shop Revolution**
Give your customers WiFi names like "EspressoEnlightenment88" instead of "CafeWiFi123" and watch your Yelp reviews soar.

### 🎉 **Event Networking**
Conference WiFi named "BlockchainUnicorn2024" with password "bu2024!" will be remembered long after your keynote is forgotten.

### 🏨 **Hospitality Industry**
Hotel guests will never forget staying at a place with WiFi called "LuxuryDreams777" and password "ld777!"

## 🔧 Configuration That Will Change Everything

### Environment Variables of Power
```bash
# The essentials
IMAGE_DIR="/var/www/peni.sh/images"           # Where the magic images live
OPENAI_API_KEY="sk-your-key-to-the-universe"  # The source of all creativity
OPENAI_MODEL="gpt-4"                          # The brain behind the operation

# Optional enhancement variables
PENISH_LOG_LEVEL="INFO"                       # How much wisdom to share
PENISH_MAX_IMAGES="1000"                      # Image cache size
PENISH_RATE_LIMIT="100"                       # Requests per minute
```

## 📊 API Response Examples That Will Inspire You

### Sophisticated Business Network
```json
{
  "ssid": "ExecutiveCloudParadigm",
  "password": "ecp2024",
  "hint": "First letters of each word + current year"
}
```

### Creative Artist Space
```json
{
  "ssid": "NeonVelvetDreamscape",
  "password": "nvd!",
  "hint": "First letter of each word + exclamation of artistic passion"
}
```

### Tech Startup Vibes
```json
{
  "ssid": "DisruptiveAlgorithmFactory",
  "password": "daf123",
  "hint": "Abbreviation + sequential numbers for version control"
}
```

## 🚨 Troubleshooting for When Reality Breaks

### The Service Refuses to Start
```bash
# Check the sacred logs
sudo journalctl -u penish -f

# Verify the chosen one (API key) is present
sudo cat /opt/penish/.env | grep OPENAI

# Restart the digital incantation
sudo systemctl restart penish nginx
```

### Images Refuse to Appear
```bash
# Ensure the image realm exists
ls -la /var/www/peni.sh/images/

# Check permissions are aligned with the cosmos
sudo chown -R penish:penish /var/www/peni.sh/images/

# Verify the API acknowledges the images
curl https://peni.sh/api/images
```

### SSL Certificate Apocalypse
```bash
# Check certificate validity
sudo certbot certificates

# Renew before the universe implodes
sudo certbot renew --dry-run
```

## 🌟 Contributing to the Revolution

Want to contribute to this world-changing project? 

1. **Fork the Repository of Destiny**
2. **Create a Branch Named After Your Wildest Dreams**
3. **Write Code That Will Make Future Generations Weep with Joy**
4. **Submit a Pull Request That Challenges the Status Quo**
5. **Wait for Approval from the Code Overlords**

### Code Style That Matters
- Use meaningful variable names (no more `x` and `y`)
- Comment your code like you're explaining it to a time traveler
- Test everything (the universe depends on it)
- Follow PEP 8 for Python (or face eternal judgment)

## 📜 License of Universal Freedom

This project is licensed under the "Do Whatever Makes You Happy" License, which basically means:

- ✅ Use it commercially (make that money)
- ✅ Modify it extensively (make it yours)
- ✅ Distribute it widely (spread the revolution)
- ✅ Create derivative works (birth new universes)
- ❌ Blame us when it becomes sentient

## 🎯 Roadmap to Digital Domination

### Phase 1: Foundation ✅
- [x] AI-powered SSID generation
- [x] Random image display
- [x] HTTPS deployment
- [x] Multi-language client examples

### Phase 2: Enhancement 🚧
- [x] **Secure Image Upload System** ✨ NEW!
- [x] **Admin Panel with Drag-and-Drop**
- [x] **Automatic Thumbnail Generation**
- [x] **Database-Backed Image Metadata**
- [ ] WebSocket real-time updates
- [ ] Custom SSID patterns
- [ ] Analytics dashboard

### Phase 3: World Domination 🌍
- [ ] Mobile app (iOS/Android)
- [ ] Browser extension
- [ ] IoT device integration
- [ ] Blockchain-based WiFi credentials (because why not?)
- [ ] AI that generates AI that generates WiFi names

## 🤝 Support & Community

- 🐛 **Bug Reports**: [Open an issue](https://github.com/yourusername/penish/issues) (but check if it's actually a bug or just reality being stubborn)
- 💡 **Feature Requests**: We accept all ideas, no matter how ridiculous
- 💬 **Community Chat**: Join our Discord server where we discuss the existential implications of WiFi naming
- 📧 **Email Support**: admin@peni.sh (for when the world is ending)

## 🎉 Acknowledgments & Credits

- **OpenAI**: For providing the AI that makes this magic possible
- **The Internet**: For existing and allowing this madness to flourish
- **Coffee**: The fuel that powers all great endeavors
- **Our Users**: The brave souls who trust us with their WiFi naming needs
- **You**: For reading this far and believing in the vision

---

**Remember**: With great WiFi names comes great responsibility. Use this power wisely.

*Made with 💚, ☕, and questionable life choices by the peni.sh team*

---

## 🔗 Links to Enlightenment

- 🌐 **Live Site**: [https://peni.sh](https://peni.sh)
- 🎛️ **Admin Panel**: [https://peni.sh/admin](https://peni.sh/admin) ✨ NEW!
- 📚 **API Documentation**: [https://peni.sh/api/docs](https://peni.sh/api/docs)
- 🏥 **Health Check**: [https://peni.sh/health](https://peni.sh/health)
- 🎲 **Generate WiFi**: [https://peni.sh/api/wifi](https://peni.sh/api/wifi)

**Go forth and revolutionize your wireless networks! The future of WiFi naming depends on you! 🚀**