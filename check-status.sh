#!/bin/bash
# Check deployment status

echo "🔍 Checking peni.sh Status"
echo "=========================="

# Check if site is responding
if curl -s https://144.126.220.220/health | grep -q "healthy"; then
    echo "✅ Site is healthy"
else
    echo "❌ Site health check failed"
fi

# Check WiFi API
if curl -s https://144.126.220.220/api/wifi | grep -q "ssid"; then
    echo "✅ WiFi API is working"
else
    echo "❌ WiFi API failed"
fi

# Check GitHub Actions status
echo "📊 Latest GitHub Actions: https://github.com/tfpickard/peni.sh/actions"
