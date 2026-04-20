# Development Session Workflow

This guide explains how to set up automatic workflow checks for every development session.

## Quick Start

```bash
# Start every development session with:
make init

# Or manually:
bash scripts/init-session.sh
```

---

## Automatic Checks Overview

The repository has **three layers** of automatic checks:

### 1. **Pre-commit Hooks** (Git Level)
- **When**: Runs automatically on `git commit`
- **What**: Linting, formatting, trailing whitespace removal
- **Setup**: `./setup-dev-env.sh` (one-time)

### 2. **Session Initialization** (Environment Level)
- **When**: Run at start of each development session
- **What**: Validates environment, installs hooks, checks health
- **Setup**: `make init` or `bash scripts/init-session.sh`

### 3. **CI Pipeline** (GitHub Level)
- **When**: Runs on every push and PR
- **What**: Full linting + test suite on Python 3.11 & 3.12
- **Simulate locally**: `make ci-local`

---

## Session Initialization

### What `make init` Does

1. ✅ Checks if pre-commit is installed (installs if missing)
2. ✅ Installs git pre-commit hooks
3. ✅ Updates hooks to latest versions
4. ✅ Validates Python dependencies
5. ✅ Runs system health check
6. ✅ Checks for linting issues in uncommitted changes

### When to Run

Run `make init` (or `scripts/init-session.sh`) when:
- Starting a new development session
- Switching to a different machine/environment
- After pulling major changes from `main`
- When CI is failing but you can't reproduce locally

---

## Development Workflow Commands

### Session Start
```bash
make init                # Initialize session + validate environment
```

### Before Committing
```bash
make check               # Run all checks (lint + test)
make lint                # Quick linting check
make test                # Run all tests
```

### Auto-fix Issues
```bash
make format              # Auto-fix formatting issues
ruff check --fix .       # Fix specific ruff issues
```

### Simulate CI Locally
```bash
make ci-local            # Run exactly what CI runs
```

### Manual Pre-commit
```bash
pre-commit run --all-files           # Run all hooks
pre-commit run --files <file>        # Run on specific file
pre-commit run ruff --all-files      # Run specific hook
```

---

## Automatic Workflows by Environment

### Local Development
```bash
# One-time setup
./setup-dev-env.sh

# Each session
make init

# Before commit (automatic)
git commit -m "..."      # Pre-commit hooks run automatically

# Before push (manual)
make check               # Simulate CI
```

### GitHub Codespaces / DevContainers
Automatic setup via `.devcontainer/devcontainer.json`:
- Runs `scripts/init-session.sh` on container creation
- Pre-configures VS Code with linting extensions
- Sets up environment variables

### CI/CD (GitHub Actions)
Automatic on every push/PR:
- Lints: `ruff check ui_blueprint/ tests/ backend/`
- Tests: `pytest tests/` and `pytest backend/tests/`
- Matrix: Python 3.11 & 3.12

---

## Workflow Checks Breakdown

### Linting Checks (Ruff)

| Check | Code | What It Catches |
|-------|------|-----------------|
| Line length | E501 | Lines > 100 characters |
| Trailing whitespace | W291, W293 | Whitespace at end of lines/blank lines |
| Import order | I001 | Unsorted imports |
| Pyflakes | F | Unused imports, undefined names |

**Auto-fix**: `make format` or `ruff check --fix .`

### Test Checks

| Suite | Location | Command |
|-------|----------|---------|
| UI Blueprint | `tests/` | `pytest tests/ -v` |
| Backend | `backend/tests/` | `pytest backend/tests/ -v` |

**Quick run**: `make test`

---

## Common Issues & Solutions

### "Pre-commit hooks not installed"
```bash
./setup-dev-env.sh
# or
pre-commit install
```

### "Linting errors on commit"
```bash
# Auto-fix what's possible
make format

# Check what still needs manual fixing
make lint
```

### "CI failing but works locally"
```bash
# Simulate exact CI environment
make ci-local

# Check specific Python version
python --version  # Should be 3.11 or 3.12
```

### "Dependencies missing"
```bash
make deps
# or manually:
pip install ".[dev]"
pip install -r backend/requirements.txt
```

---

## Integration with AI Agents

When AI agents (like GitHub Copilot) work on this repository:

### Required First Steps
1. Run `make init` to validate environment
2. Check `AI_AGENT_CONTEXT.md` for constraints
3. Verify pre-commit hooks are installed

### Before Making Changes
1. Understand existing patterns: `make lint` on current code
2. Check test coverage: `make test`
3. Review recent changes: `git log --oneline -10`

### After Making Changes
1. Format code: `make format`
2. Run checks: `make check`
3. Simulate CI: `make ci-local`
4. Commit (hooks run automatically)

---

## Makefile Reference

| Command | Description | When to Use |
|---------|-------------|-------------|
| `make init` | Initialize session | Start of session |
| `make check` | Run all checks | Before pushing |
| `make lint` | Lint only | Quick validation |
| `make test` | Run all tests | After code changes |
| `make format` | Auto-fix formatting | Before commit |
| `make ci-local` | Simulate CI | Before creating PR |
| `make clean` | Remove caches | Cleanup |
| `make deps` | Install dependencies | After pulling |

---

## File Locations

| File | Purpose |
|------|---------|
| `scripts/init-session.sh` | Session initialization script |
| `Makefile` | Common development commands |
| `.pre-commit-config.yaml` | Pre-commit hook configuration |
| `.devcontainer/devcontainer.json` | Codespaces/DevContainer setup |
| `.github/workflows/ci.yml` | CI pipeline definition |

---

## Best Practices

1. **Always run `make init` at session start** - Ensures environment is correct
2. **Let pre-commit do its job** - Don't skip hooks with `--no-verify`
3. **Test locally before pushing** - `make ci-local` catches issues early
4. **Fix, don't ignore** - Address linting errors instead of suppressing them
5. **Keep hooks updated** - `pre-commit autoupdate` periodically

---

## Summary

**Inherent workflow checks** are achieved through:

1. ✅ **Pre-commit hooks** - Automatic on `git commit`
2. ✅ **Session init script** - Run once per session with `make init`
3. ✅ **Makefile shortcuts** - `make check` before pushing
4. ✅ **DevContainer config** - Automatic in cloud environments
5. ✅ **CI pipeline** - Final validation on GitHub

**Result**: Code quality checks are baked into every step of the workflow, making it nearly impossible to commit broken code.
