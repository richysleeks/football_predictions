#!/usr/bin/env bash
# First-time setup for the football predictions automation.
# Run this once after cloning the repo.

set -e

echo ""
echo "=== Football Predictions Setup ==="
echo ""

# 1. Install dependencies
echo "[1/4] Installing Python dependencies..."
pip install -r requirements.txt

# 2. Create .env from template
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[2/4] Created .env — please fill in your API keys before running predictions."
else
    echo "[2/4] .env already exists — skipping."
fi

# 3. Create database directory
mkdir -p data/raw data/processed data/final
echo "[3/4] Created data directories."

# 4. Run migrations
echo "[4/4] Running database migrations..."
python manage.py migrate

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your API keys"
echo "  2. Run: python manage.py run_predictions"
echo "  3. (Next day) Run: python manage.py check_results"
echo ""
echo "To automate daily (Linux cron):"
echo "  0 9 * * * cd /path/to/football_predictions && python manage.py run_predictions"
echo "  0 8 * * * cd /path/to/football_predictions && python manage.py check_results"
echo ""
