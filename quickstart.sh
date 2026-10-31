#!/bin/bash
# Quick Start Script for Melbete Bingo
# Sets up a local Python environment. Persistence is Cloud Firestore, so
# there is no local database or cache to install - you just need a
# Firebase project and service-account key (see README.md).

set -e

echo "🎉 Melbete Bingo - Quick Start Setup"
echo "==================================="
echo ""

# --- .env file ---------------------------------------------------------
if [ ! -f .env ]; then
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo "✅ .env created. Edit it with your configuration before continuing:"
    echo "   - TELEGRAM_BOT_TOKEN      (from @BotFather)"
    echo "   - FIREBASE_PROJECT_ID     (Firebase console -> Project settings)"
    echo "   - FIREBASE_CREDENTIALS_FILE or FIREBASE_CREDENTIALS_BASE64"
    echo "   - PUBLIC_BASE_URL         (your deployed URL, or an ngrok tunnel)"
    echo ""
    echo "See README.md 'Firebase setup' for step-by-step instructions."
    echo ""
fi

# --- Python version check -----------------------------------------------
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
REQUIRED_VERSION="3.10"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "❌ Python 3.10+ is required. Current version: $PYTHON_VERSION"
    exit 1
fi
echo "✅ Python version: $PYTHON_VERSION"

# --- Virtual environment -------------------------------------------------
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
    echo "✅ Virtual environment created"
fi

echo "🔄 Activating virtual environment..."
# shellcheck disable=SC1091
source venv/bin/activate

echo "📥 Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "✅ Dependencies installed"

# --- Firebase credentials sanity check -----------------------------------
if grep -q "^FIREBASE_PROJECT_ID=your-firebase-project-id" .env 2>/dev/null; then
    echo "⚠️  FIREBASE_PROJECT_ID still has its placeholder value in .env"
fi

FIREBASE_CREDENTIALS_FILE=$(grep -E "^[[:space:]]*FIREBASE_CREDENTIALS_FILE=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')
FIREBASE_CREDENTIALS_BASE64=$(grep -E "^[[:space:]]*FIREBASE_CREDENTIALS_BASE64=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')

if [ -n "$FIREBASE_CREDENTIALS_FILE" ]; then
    if [ -f "$FIREBASE_CREDENTIALS_FILE" ]; then
        echo "✅ Found Firebase credentials file: $FIREBASE_CREDENTIALS_FILE"
    else
        echo "⚠️  FIREBASE_CREDENTIALS_FILE is set but file not found: $FIREBASE_CREDENTIALS_FILE"
    fi
elif [ -n "$FIREBASE_CREDENTIALS_BASE64" ]; then
    echo "✅ FIREBASE_CREDENTIALS_BASE64 is set in .env"
else
    echo "⚠️  Firebase credentials are not configured in .env."
    echo "   Set FIREBASE_CREDENTIALS_FILE to a JSON key path, or"
    echo "   set FIREBASE_CREDENTIALS_BASE64 to the base64-encoded key."
fi

echo ""
echo "What would you like to do?"
echo "1. Run tests"
echo "2. Start development server (API)"
echo "3. Start the Telegram bot"
echo "4. Start both (bot + API) with Docker Compose"
echo "5. Exit"
echo ""
read -rp "Enter your choice (1-5): " choice

case $choice in
    1)
        echo ""
        echo "🧪 Running tests..."
        pytest -v
        ;;
    2)
        echo ""
        echo "🚀 Starting development API server..."
        echo "   Press Ctrl+C to stop"
        python3 main.py api
        ;;
    3)
        echo ""
        echo "🤖 Starting the Telegram bot..."
        echo "   Press Ctrl+C to stop"
        python3 main.py bot
        ;;
    4)
        echo ""
        if command -v docker &> /dev/null; then
            echo "🐳 Starting with Docker Compose..."
            docker compose up --build -d
            echo "✅ Services started!"
            echo "   View logs: docker compose logs -f"
            echo "   Stop: docker compose down"
        else
            echo "❌ Docker not found. Please install Docker Desktop / Docker Engine."
        fi
        ;;
    5)
        echo "👋 Goodbye!"
        exit 0
        ;;
    *)
        echo "❌ Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "📖 For more information, see:"
echo "   - README.md          - Setup, local dev, and how the app works"
echo "   - DEPLOYMENT.md       - Deploying to Render/Railway/Fly.io/Cloud Run"
echo "   - API_REFERENCE.md    - REST + WebSocket API reference"
echo ""
