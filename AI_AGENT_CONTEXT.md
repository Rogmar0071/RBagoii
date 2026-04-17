# AI Agent Context - Read First Before Any Mutations

> **REQUIRED READING**: All AI agents must read and understand this file
> before planning or executing any code changes.
> **AUTO-GENERATED**: This file is automatically updated when README.md changes.
> Do not edit manually.

---

## 🎯 Project Overview

**RecoB (ui-blueprint)** - Convert 10-second Android screen-recording clips into a structured "blueprint" suitable for near-human-indistinguishable replay in a custom renderer — and optionally for compiling into automation events.

### Core Purpose
- Extract UI interaction patterns from 10-second Android screen recordings
- Generate machine-readable JSON blueprints with frame-by-frame element tracking
- Enable near-human-indistinguishable replay in custom renderers
- Support automation script compilation (UIAutomator/Accessibility)

---

## 📁 Critical Directory Structure

```
ui-blueprint/
├── schema/
│   └── blueprint.schema.json   # JSON Schema v1
├── ui_blueprint/
│   ├── __init__.py
│   ├── __main__.py             # CLI entry point
│   ├── extractor.py            # Video → Blueprint pipeline
│   └── preview.py              # Blueprint → PNG preview frames
├── tests/
│   └── test_extractor.py       # Unit + CLI integration tests
├── .github/workflows/ci.yml    # GitHub Actions CI
└── pyproject.toml
```

**Key Directories**:
- `backend/` - FastAPI backend (Python 3.11+)
  - `app/` - Application code
  - `tests/` - Backend test suite (pytest)
- `ui_blueprint/` - Core blueprint extraction library
- `tests/` - ui_blueprint tests
- `android/` - Android screen recording app
- `schema/` - JSON Schema definitions
- `.github/workflows/` - CI/CD pipelines

---

## 🛡️ Code Quality & Linting Standards

### **CRITICAL**: Pre-commit Hooks Are Mandatory

## Development setup

### First-time setup

Run the setup script to configure your local development environment with pre-commit hooks:

```bash
./setup-dev-env.sh
```

This installs and configures [pre-commit](https://pre-commit.com) hooks that will:
- ✅ Auto-fix code formatting issues (ruff)
- ✅ Check for trailing whitespace
- ✅ Ensure files end with newlines
- ✅ Prevent large files from being committed
- ✅ Validate YAML and JSON syntax
- ✅ Check for merge conflicts and debug statements

Pre-commit hooks **prevent CI linting failures** by catching issues locally before you commit.

### Manual pre-commit usage

```bash
# Run on all files
pre-commit run --all-files

# Run on staged files only (happens automatically on git commit)
pre-commit run

# Update hook versions
pre-commit autoupdate
```

---

### Common Linting Errors to Avoid

| Error | Description | Prevention |
|-------|-------------|------------|
| **E501** | Line too long (>100 chars) | Break long lines, use parentheses for line continuation |
| **W293** | Blank line contains whitespace | Use empty lines with no spaces/tabs |
| **W291** | Trailing whitespace | Pre-commit auto-fixes this |

### Before Committing Code

```bash
# Run locally before committing
pre-commit run --all-files

# Or let it run automatically on commit
git commit -m "Your message"  # Pre-commit runs here
```

---

## 🔒 Backend Architecture & Contracts

### Mode Engine System

**Location**: `backend/app/mode_engine.py`

Three execution modes (immutable contract):

| Mode | Symbol | Behavior |
|------|--------|----------|
| **STRICT** | `MODE_STRICT` | Validates all constraints, blocks on failure |
| **VERIFIED** | `MODE_VERIFIED` | Validates + logs violations, allows execution |
| **STANDARD** | `MODE_STANDARD` | Minimal validation, fast execution |

**Key Principles**:
- Mode resolution is deterministic
- STRICT mode NEVER allows invalid operations
- All mode decisions are audit-logged
- Mode conflicts resolved with priority: STRICT > VERIFIED > STANDARD

### Mutation Governance

**Location**: `backend/app/mutation_governance/engine.py`

All state-changing operations go through governance pipeline:

1. **Input validation** - Schema conformance
2. **Mode resolution** - Determine execution mode
3. **Constraint validation** - Check business rules
4. **Audit logging** - Record all decisions (mandatory)
5. **Execution** - Perform mutation (only if validated)

**Retry behavior**:
- Max 3 retries on validation failure
- Deterministic backoff
- All retries audit-logged

---

## 🧪 Testing Requirements

## Running tests

```bash
pytest tests/ -v
```

CI runs automatically on every push and pull request via GitHub Actions (`.github/workflows/ci.yml`).

---

### Before Submitting Changes

```bash
# 1. Run linter
ruff check backend/ ui_blueprint/ tests/

# 2. Run all tests
pytest tests/ -v
pytest backend/tests/ -v

# 3. Verify CI would pass
pre-commit run --all-files
```

### CI Pipeline (`.github/workflows/ci.yml`)

- Runs on: Every push, every PR
- Jobs:
  - `lint-and-test`: Python linting + ui_blueprint tests (3.11, 3.12)
  - `backend-test`: Backend linting + backend tests (3.11, 3.12)
  - `android-assemble`: Android APK build (optional/informational)

**All linting and test jobs must pass before merge.**

---

## 🔧 Development Workflow

### For New Features

1. **Read this file first** ✋
2. **Set up pre-commit hooks**: `./setup-dev-env.sh`
3. **Understand existing code**: Read relevant files in `backend/app/` or `ui_blueprint/`
4. **Write tests first**: Add tests before implementation
5. **Implement with small commits**: Commit frequently
6. **Lint locally**: `ruff check .` before committing
7. **Run tests locally**: `pytest` before pushing
8. **Let pre-commit validate**: It runs on `git commit`

### For Bug Fixes

1. **Reproduce the bug**: Add a failing test case first
2. **Fix the issue**: Make minimal changes
3. **Verify the fix**: Ensure new test passes, old tests still pass
4. **Check for regressions**: Run full test suite
5. **Update docs**: If behavior changes affect users

### For Refactoring

1. **Run tests first**: Establish baseline (all passing)
2. **Make changes incrementally**: Small, testable steps
3. **Run tests after each step**: Ensure no breakage
4. **Commit frequently**: One logical change per commit
5. **Verify linting**: `ruff check` and `pre-commit run --all-files`

---

## 📦 Dependencies & Installation

### Python Dependencies

**Core** (`pyproject.toml`):
- Python 3.11+ (required)
- Pillow (image processing)
- imageio[ffmpeg] (optional, for video decoding)

**Dev** (`[project.optional-dependencies].dev`):
- ruff (linting & formatting)
- pytest (testing)
- pre-commit (git hooks)
- jsonschema (validation)

**Backend** (`backend/requirements.txt`):
- FastAPI (web framework)
- SQLModel (database ORM)
- pydantic (data validation)
- httpx (HTTP client)
- OpenAI SDK (optional, for AI features)
- Redis/RQ (optional, for background jobs)

### Installation Commands

```bash
# UI Blueprint (core)
pip install ".[dev]"

# Backend
pip install -r backend/requirements.txt

# Pre-commit hooks
pip install pre-commit
pre-commit install
```

---

## 🔐 Environment Variables

## OpenAI configuration

Setting `OPENAI_API_KEY` on the server enables two AI features:

1. **AI domain derivation** — `POST /api/domains/derive` uses GPT instead of the keyword stub.
2. **AI chat** — `POST /api/chat` responds via GPT instead of returning a stub message.

### Two separate secrets — do not confuse them

| Variable | Purpose | Sent to clients? |
|---|---|---|
| `API_KEY` | Service bearer token — protects all mutating endpoints | **No** — stays on server |
| `OPENAI_API_KEY` | Server-side OpenAI credential — used for AI calls | **Never** — stays on server |

Clients only ever need `API_KEY` (passed as `Authorization: Bearer <API_KEY>`).
`OPENAI_API_KEY` is read on the server and never appears in any response or log.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(unset — stub mode)* | OpenAI API key |
| `OPENAI_MODEL_DOMAIN` | `gpt-4.1-mini` | Model used by `/api/domains/derive` |
| `OPENAI_MODEL_CHAT` | `gpt-4.1-mini` | Model used by `/api/chat` |
| `OPENAI_BASE_URL` | `https://api.openai.com` | Base URL (supports custom proxies) |
| `OPENAI_TIMEOUT_SECONDS` | `30` | Per-request timeout |

### Render deployment

In the Render **Environment** tab for your web service add:

```
API_KEY          = <generate with: openssl rand -hex 32>
OPENAI_API_KEY   = sk-...
```

Leave `OPENAI_MODEL_DOMAIN`, `OPENAI_MODEL_CHAT`, and `OPENAI_BASE_URL` unset to
use the defaults.

### /api/chat usage

```bash
# Stub reply (OPENAI_API_KEY not configured)
curl -s -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "How do I derive a domain profile?"}' \
  | python3 -m json.tool

# Response shape:
# {
#   "schema_version": "v1.1.0",
#   "reply": "[Stub] You said: ...",
#   "tools_available": ["domains.derive", "domains.confirm", ...]
# }
```

`tools_available` lists the pipeline actions the assistant can describe (no automatic
tool execution yet — information only).

---

### Never Commit Secrets

- ❌ Don't commit API keys to git
- ❌ Don't hardcode credentials in code
- ✅ Use environment variables
- ✅ Add secrets to `.gitignore` (already configured)

---

## 🚨 Critical Constraints

### Immutable Contracts (DO NOT BREAK)

1. **Mode Engine Contract**:
   - MODE_STRICT always blocks invalid operations
   - Mode resolution is deterministic
   - Audit logging is mandatory

2. **Mutation Governance Contract**:
   - All mutations go through validation pipeline
   - Retry count is always logged
   - Validation results are immutable once persisted

3. **Blueprint Schema**:
   - Must conform to `schema/blueprint.schema.json`
   - Breaking changes require schema version bump

4. **API Response Shapes**:
   - Don't change existing response structures without version bump
   - Errors use `{"error": {"code": "...", "message": "..."}}`

### Safe to Modify

- ✅ Add new features (with tests)
- ✅ Improve performance (with benchmarks)
- ✅ Fix bugs (with regression tests)
- ✅ Refactor internal implementation (tests must pass)
- ✅ Add new API endpoints (with docs)

### Requires Careful Review

- ⚠️ Changing database schema
- ⚠️ Modifying mode engine behavior
- ⚠️ Changing mutation governance rules
- ⚠️ Breaking API changes
- ⚠️ Dependency version upgrades (test thoroughly)

---

## 🎓 AI Agent Guidelines

### Before Making Any Changes

1. ✅ Read this entire file
2. ✅ Check if pre-commit hooks are configured
3. ✅ Understand the relevant module's purpose
4. ✅ Review existing tests for the module
5. ✅ Verify linting rules in `pyproject.toml`

### When Writing Code

1. ✅ Follow existing code patterns and conventions
2. ✅ Keep lines under 100 characters (E501)
3. ✅ Remove trailing whitespace (W291, W293)
4. ✅ Add type hints (Python 3.11+ syntax)
5. ✅ Write docstrings for public functions
6. ✅ Import order: stdlib → third-party → local

### When Writing Tests

1. ✅ Test file naming: `test_*.py` or `*_test.py`
2. ✅ Use pytest fixtures for common setup
3. ✅ Test both success and failure cases
4. ✅ Use descriptive test names: `test_<what>_<condition>_<expected>`
5. ✅ Mock external dependencies (OpenAI, Redis, etc.)

### When Making PRs

1. ✅ All linting passes: `ruff check .`
2. ✅ All tests pass: `pytest`
3. ✅ Pre-commit hooks pass: `pre-commit run --all-files`
4. ✅ Commit messages are descriptive
5. ✅ Large changes are broken into smaller commits

---

## 📞 Getting Help

### Documentation

- **README.md** - Full project documentation (source of truth)
- **AI_AGENT_CONTEXT.md** - This file (auto-generated from README.md)
- **LINTING_PREVENTION_SYSTEM.md** - Pre-commit hooks explained
- **schema/blueprint.schema.json** - Blueprint data structure
- **backend/app/mode_engine.py** - Mode system implementation
- **backend/app/mutation_governance/** - Governance pipeline

### Common Issues

| Issue | Solution |
|-------|----------|
| Linting errors | Run `pre-commit run --all-files` |
| Tests failing | Check `pytest -v` output for details |
| Import errors | Verify dependencies: `pip install ".[dev]"` |
| CI failures | Check GitHub Actions logs for specifics |

---

## ✅ Checklist Before Committing

```
□ Pre-commit hooks installed (./setup-dev-env.sh)
□ Code follows 100-character line limit
□ No trailing whitespace on blank lines
□ All imports are sorted correctly
□ Type hints added for new functions
□ Tests added for new functionality
□ All tests pass locally (pytest)
□ Linting passes locally (ruff check .)
□ Pre-commit passes (pre-commit run --all-files)
□ Documentation updated if needed
□ No secrets or credentials in code
```

---

**Last Updated**: 2026-04-17
**Generated From**: README.md
**Generator**: scripts/sync_ai_context.py
**Purpose**: Provide AI agents with essential context before making changes

> ⚠️ **Do not edit this file directly.** Changes will be overwritten.
> Edit README.md instead and run `python scripts/sync_ai_context.py` to regenerate.
