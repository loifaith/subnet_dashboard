#!/bin/bash

# Start Backend API and Frontend servers with PM2

set -e

cd "$(dirname "$0")"

echo "Starting Subnet Dashboard with PM2..."
echo ""

# Ensure pm2 is available
if ! command -v pm2 >/dev/null 2>&1; then
    echo "✗ pm2 is not installed or not on PATH."
    echo "  Install it with: npm install -g pm2"
    exit 1
fi

# Ensure the log directory exists (referenced by ecosystem.config.json)
mkdir -p logs

# Install/update Python dependencies into the virtual environment
if [ -x ".venv/bin/pip" ]; then
    echo "Installing dependencies..."
    .venv/bin/pip install -r requirements.txt > /dev/null 2>&1 || true
fi

echo ""
echo "========================================"
echo "Starting backend_api (port 9900) and dashboard (port 12800)..."
echo "========================================"

# Start (or reload if already running) both apps from the ecosystem file
pm2 startOrReload ecosystem.config.json

# Persist the process list so it survives reboots (requires `pm2 startup` once)
pm2 save > /dev/null 2>&1 || true

echo ""
pm2 list

echo ""
echo "========================================"
echo "Both servers are managed by PM2!"
echo "========================================"
echo ""
echo "Frontend:    http://localhost:12800"
echo "Backend API: http://localhost:9900"
echo "API Docs:    http://localhost:9900/docs"
echo ""
echo "Useful commands:"
echo "  pm2 logs dashboard        # tail frontend logs"
echo "  pm2 logs backend_api      # tail backend logs"
echo "  pm2 restart ecosystem.config.json"
echo "  pm2 stop backend_api dashboard"
echo ""
