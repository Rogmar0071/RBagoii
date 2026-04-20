#!/bin/bash
# scripts/init-session.sh
# Run this at the start of each development session to validate environment

set -e

echo "🚀 Initializing development session..."

# 1. Check if pre-commit hooks are installed
if ! pre-commit --version &> /dev/null; then
    echo "❌ pre-commit not found. Installing..."
    pip install -q pre-commit
fi

if [ ! -f .git/hooks/pre-commit ]; then
    echo "⚙️  Installing pre-commit hooks..."
    pre-commit install
    echo "✅ Pre-commit hooks installed"
else
    echo "✅ Pre-commit hooks already installed"
fi

# 2. Update pre-commit hooks to latest versions
echo "🔄 Updating pre-commit hooks..."
pre-commit autoupdate --quiet || echo "⚠️  Could not auto-update hooks"

# 3. Validate dependencies are installed
echo "📦 Checking Python dependencies..."
if ! python -c "import ruff" &> /dev/null; then
    echo "⚙️  Installing development dependencies..."
    pip install -q ".[dev]"
fi

# 4. Run quick health check
echo "🏥 Running health check..."
if [ -f "scripts/debug/health_check.py" ]; then
    python scripts/debug/health_check.py --quick || echo "⚠️  Health check failed"
fi

# 5. Check for uncommitted changes that would fail linting
echo "🔍 Checking for linting issues in current changes..."
if git diff --quiet; then
    echo "✅ No uncommitted changes"
else
    echo "⚙️  Checking uncommitted changes..."
    # Run pre-commit on changed files only
    pre-commit run --files $(git diff --name-only --diff-filter=ACMR) || {
        echo "⚠️  Some files have linting issues. Run 'pre-commit run --all-files' to see details."
    }
fi

echo ""
echo "✨ Session initialized successfully!"
echo ""
echo "Quick commands:"
echo "  pre-commit run --all-files  # Run all checks"
echo "  pytest tests/ -v            # Run ui_blueprint tests"
echo "  pytest backend/tests/ -v    # Run backend tests"
echo "  ruff check backend/         # Check backend linting"
echo ""
