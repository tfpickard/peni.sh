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
