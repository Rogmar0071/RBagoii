#!/bin/bash
# Setup development environment with pre-commit hooks

set -e

echo "🔧 Setting up development environment..."

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not found. Please install Python 3.11 or later."
    exit 1
fi

# Install pre-commit if not already installed
if ! command -v pre-commit &> /dev/null; then
    echo "📦 Installing pre-commit..."
    python3 -m pip install pre-commit
else
    echo "✅ pre-commit is already installed"
fi

# Install pre-commit hooks
echo "🪝 Installing pre-commit hooks..."
pre-commit install

# Run pre-commit on all files to ensure everything is configured correctly
echo "🧪 Running pre-commit on all files (this may take a moment)..."
pre-commit run --all-files || {
    echo ""
    echo "⚠️  Some files needed formatting. They have been automatically fixed."
    echo "   Please review the changes and commit them."
    exit 0
}

echo ""
echo "✅ Development environment setup complete!"
echo ""
echo "Pre-commit hooks are now installed and will run automatically before each commit."
echo "This will prevent linting failures in CI by catching issues locally first."
echo ""
echo "To run pre-commit manually on all files: pre-commit run --all-files"
echo "To update hook versions: pre-commit autoupdate"
