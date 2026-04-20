# Makefile for RBagoii development workflows

.PHONY: help init check test lint format ci-local clean

# Default target
help:
	@echo "RBagoii Development Commands"
	@echo "=============================="
	@echo ""
	@echo "Session Management:"
	@echo "  make init          - Initialize development session (hooks, deps, health, quick tests)"
	@echo ""
	@echo "Code Quality:"
	@echo "  make check         - Run all checks (lint + test) ⚠️  MUST PASS BEFORE PUSH"
	@echo "  make lint          - Run linting only"
	@echo "  make format        - Auto-fix formatting issues"
	@echo "  make ci-local      - Simulate CI pipeline locally"
	@echo ""
	@echo "Testing:"
	@echo "  make test          - Run all tests (UI + backend)"
	@echo "  make test-ui       - Run ui_blueprint tests only"
	@echo "  make test-backend  - Run backend tests only"
	@echo "  make test-quick    - Run quick sanity tests"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         - Remove temporary files and caches"
	@echo ""

# Initialize development session
init:
	@echo "🚀 Initializing development session..."
	@bash scripts/init-session.sh

# Run all checks (what CI runs) - REQUIRED before push
check: lint test
	@echo ""
	@echo "✅ All checks passed!"
	@echo "   Safe to push to GitHub"

# Linting
lint:
	@echo "🔍 Running linting checks..."
	@ruff check ui_blueprint/ tests/ backend/
	@echo "✅ Linting passed!"

# Auto-fix formatting
format:
	@echo "🔧 Auto-fixing formatting issues..."
	@ruff check --fix ui_blueprint/ tests/ backend/
	@pre-commit run --all-files || true
	@echo "✅ Formatting complete!"

# Run all tests
test: test-ui test-backend
	@echo "✅ All tests passed!"

# UI blueprint tests
test-ui:
	@echo "🧪 Running ui_blueprint tests..."
	@pytest tests/ -v --tb=short

# Backend tests
test-backend:
	@echo "🧪 Running backend tests..."
	@BACKEND_DISABLE_JOBS=1 pytest backend/tests/ -v --tb=short

# Quick sanity test (subset of critical tests)
test-quick:
	@echo "🧪 Running quick sanity tests..."
	@BACKEND_DISABLE_JOBS=1 pytest backend/tests/test_api.py::TestHealth tests/ -q --tb=line

# Simulate CI pipeline locally (ALWAYS run before creating PR)
ci-local: format check
	@echo "🎯 Local CI simulation complete!"
	@echo ""
	@echo "This is what will run in GitHub Actions:"
	@echo "  ✓ Linting with ruff"
	@echo "  ✓ ui_blueprint tests"
	@echo "  ✓ Backend tests"
	@echo ""
	@echo "✅ Ready to push!"

# Clean temporary files
clean:
	@echo "🧹 Cleaning temporary files..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ Cleanup complete!"

# Install dependencies
deps:
	@echo "📦 Installing dependencies..."
	@pip install -q --upgrade pip
	@pip install -q ".[dev]"
	@pip install -q -r backend/requirements.txt
	@echo "✅ Dependencies installed!"
