#!/bin/bash

# Start the real-time miner status server with PM2.
# Polls TAOS metrics for your registered miners every 30s and serves the latest
# status (+ 3h history) on port 9800 for other servers to poll.

set -e

cd "$(dirname "$0")"

echo "Starting SN79 Miner Status Server with PM2..."
echo ""

# Ensure pm2 is available
if ! command -v pm2 >/dev/null 2>&1; then
    echo "✗ pm2 is not installed or not on PATH."
    echo "  Install it with: npm install -g pm2"
    exit 1
fi

# Ensure the log directory exists (referenced by ecosystem.config.json)
mkdir -p logs

echo "========================================"
echo "Starting status_server on port ${STATUS_SERVER_PORT:-9800}..."
echo "========================================"

# Start (or reload if already running) only the status_server app
pm2 startOrReload ecosystem.config.json --only status_server

# Persist the process list so it survives reboots (requires `pm2 startup` once)
pm2 save > /dev/null 2>&1 || true

echo ""
pm2 list | grep -E 'name|status_server' || true

echo ""
echo "========================================"
echo "Status server is managed by PM2!"
echo "========================================"
echo ""
echo "Health:  http://localhost:${STATUS_SERVER_PORT:-9800}/health"
echo "Status:  http://localhost:${STATUS_SERVER_PORT:-9800}/status"
echo "Per-UID: http://localhost:${STATUS_SERVER_PORT:-9800}/status/<uid>"
echo "History: http://localhost:${STATUS_SERVER_PORT:-9800}/history?uid=<uid>&minutes=60"
echo ""
echo "Useful commands:"
echo "  pm2 logs status_server     # tail logs"
echo "  pm2 restart status_server"
echo "  pm2 stop status_server"
echo ""
