#!/bin/bash

# Start Backend API and Frontend servers

echo "Starting SN71 Session Manager with Backend API..."
echo ""

# Check if virtual environment exists
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Install/update dependencies
echo "Installing dependencies..."
pip install -r requirements.txt > /dev/null 2>&1

echo ""
echo "========================================"
echo "Starting Backend API on port 9500..."
echo "========================================"
python backend_api.py &
BACKEND_PID=$!

# Wait a moment for backend to start
sleep 2

# Check if backend started successfully
if kill -0 $BACKEND_PID 2>/dev/null; then
    echo "✓ Backend API started (PID: $BACKEND_PID)"
else
    echo "✗ Failed to start Backend API"
    exit 1
fi

echo ""
echo "========================================"
echo "Starting Frontend on port 8000..."
echo "========================================"
python app.py &
FRONTEND_PID=$!

# Wait a moment for frontend to start
sleep 2

# Check if frontend started successfully
if kill -0 $FRONTEND_PID 2>/dev/null; then
    echo "✓ Frontend started (PID: $FRONTEND_PID)"
else
    echo "✗ Failed to start Frontend"
    kill $BACKEND_PID
    exit 1
fi

echo ""
echo "========================================"
echo "Both servers are running!"
echo "========================================"
echo ""
echo "Frontend:    http://localhost:12800"
echo "Backend API: http://localhost:9900"
echo "API Docs:    http://localhost:9900/docs"
echo ""
echo "Press Ctrl+C to stop both servers"
echo ""

# Function to handle Ctrl+C
cleanup() {
    echo ""
    echo "Stopping servers..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
    echo "Servers stopped."
    exit 0
}

# Trap Ctrl+C
trap cleanup INT

# Wait for processes
wait
