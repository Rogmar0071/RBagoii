#!/usr/bin/env python3
"""
Sync key sections from README.md to AI_AGENT_CONTEXT.md

This script extracts critical information from README.md and updates
the AI_AGENT_CONTEXT.md file to ensure AI agents always have current context.

Run automatically via pre-commit hook on README.md changes.
"""

import re
import sys
from datetime import datetime
from pathlib import Path


def extract_section(content: str, section_title: str, level: int = 2) -> str:
    """Extract a section from markdown content."""
    pattern = rf'^{"#" * level}\s+{re.escape(section_title)}.*?(?=^{"#" * level}\s|\Z)'
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return match.group(0).strip() if match else ""


def extract_project_structure(readme_content: str) -> str:
    """Extract the project structure section."""
    section = extract_section(readme_content, "Project structure")
    if not section:
        return ""

    # Extract just the code block
    code_block = re.search(r'```.*?```', section, re.DOTALL)
    return code_block.group(0) if code_block else ""


def extract_quick_start(readme_content: str) -> str:
    """Extract installation and setup commands."""
    section = extract_section(readme_content, "Quick start")
    return section if section else ""


def extract_env_vars(readme_content: str) -> str:
    """Extract environment variables section."""
    section = extract_section(readme_content, "OpenAI configuration")
    return section if section else ""


def extract_testing(readme_content: str) -> str:
    """Extract testing information."""
    section = extract_section(readme_content, "Running tests")
    return section if section else ""


def extract_dev_setup(readme_content: str) -> str:
    """Extract development setup section."""
    section = extract_section(readme_content, "Development setup")
    return section if section else ""


def generate_ai_context(readme_path: Path) -> str:
    """Generate AI_AGENT_CONTEXT.md content from README.md."""

    readme_content = readme_path.read_text(encoding='utf-8')

    # Get the project description (first few lines after title)
    description_match = re.search(
        r'^#\s+.*?\n\n>\s+(.*?)\n\n---',
        readme_content,
        re.MULTILINE | re.DOTALL
    )
    description = (
        description_match.group(1)
        if description_match
        else "Convert 10-second Android screen-recording clips into structured blueprints."
    )

    # Extract key sections
    project_structure = extract_project_structure(readme_content)
    dev_setup = extract_dev_setup(readme_content)
    testing = extract_testing(readme_content)
    env_vars = extract_env_vars(readme_content)

    # Get current date
    current_date = datetime.now().strftime("%Y-%m-%d")

    # Default fallback content for missing sections
    default_dev_setup = '''
Before making ANY code changes:

1. **Setup Pre-commit Hooks**:
   ```bash
   ./setup-dev-env.sh  # One-time setup
   ```

2. **Ruff Configuration** (`pyproject.toml`):
   - Line length: **100 characters maximum** (E501)
   - Target Python: 3.11+
   - Enabled rules: E (errors), F (pyflakes), W (warnings), I (import order)
'''

    default_testing = '''
### Test Suites

| Suite | Location | Command | Required |
|-------|----------|---------|----------|
| UI Blueprint | `tests/` | `pytest tests/ -v` | ✅ Always |
| Backend | `backend/tests/` | `pytest backend/tests/ -v` | ✅ Always |
| Mode Engine | `backend/tests/test_mode_engine.py` | (subset) | ✅ Critical |
| Mutation Governance | `backend/tests/test_mutation_governance.py` | (subset) | ✅ Critical |
'''

    default_env_vars = '''
### Backend Configuration

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `API_KEY` | No | (unset) | Service bearer token for protected endpoints |
| `OPENAI_API_KEY` | No | (unset) | Enables AI features (chat, domain derivation) |
| `REDIS_URL` | No | (unset) | Enables RQ background job queue |
| `DATABASE_URL` | No | (in-memory) | PostgreSQL connection string |
| `MAX_UPLOAD_BYTES` | No | `52428800` | Max file upload size (50 MB) |
'''

    # Use extracted content or defaults
    dev_setup_content = dev_setup if dev_setup else default_dev_setup
    testing_content = testing if testing else default_testing
    env_vars_content = env_vars if env_vars else default_env_vars
    project_structure_content = (
        project_structure if project_structure else "See README.md for full project structure"
    )

    # Build context using format() to avoid f-string quote issues in Python 3.11
    context_template = '''# AI Agent Context - Read First Before Any Mutations

> **REQUIRED READING**: All AI agents must read and understand this file
> before planning or executing any code changes.
> **AUTO-GENERATED**: This file is automatically updated when README.md changes.
> Do not edit manually.

---

## 🎯 Project Overview

**RecoB (ui-blueprint)** - {description}

### Core Purpose
- Extract UI interaction patterns from 10-second Android screen recordings
- Generate machine-readable JSON blueprints with frame-by-frame element tracking
- Enable near-human-indistinguishable replay in custom renderers
- Support automation script compilation (UIAutomator/Accessibility)

---

## 📁 Critical Directory Structure

{project_structure}

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

{dev_setup}

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

{testing}

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

{env_vars}

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
   - Errors use `{{"error": {{"code": "...", "message": "..."}}}}`

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

**Last Updated**: {current_date}
**Generated From**: README.md
**Generator**: scripts/sync_ai_context.py
**Purpose**: Provide AI agents with essential context before making changes

> ⚠️ **Do not edit this file directly.** Changes will be overwritten.
> Edit README.md instead and run `python scripts/sync_ai_context.py` to regenerate.
'''

    context = context_template.format(
        description=description,
        project_structure=project_structure_content,
        dev_setup=dev_setup_content,
        testing=testing_content,
        env_vars=env_vars_content,
        current_date=current_date
    )

    return context


def main():
    """Main entry point."""
    repo_root = Path(__file__).parent.parent
    readme_path = repo_root / "README.md"
    ai_context_path = repo_root / "AI_AGENT_CONTEXT.md"

    if not readme_path.exists():
        print(f"❌ README.md not found at {readme_path}", file=sys.stderr)
        return 1

    print("📝 Generating AI_AGENT_CONTEXT.md from README.md...")

    try:
        new_content = generate_ai_context(readme_path)

        # Check if content changed
        if ai_context_path.exists():
            old_content = ai_context_path.read_text(encoding='utf-8')
            if old_content == new_content:
                print("✅ AI_AGENT_CONTEXT.md is already up to date")
                return 0

        # Write new content
        ai_context_path.write_text(new_content, encoding='utf-8')
        print(f"✅ Successfully updated {ai_context_path}")

        # If running in pre-commit, stage the file
        if len(sys.argv) > 1:
            import subprocess
            subprocess.run(["git", "add", str(ai_context_path)], check=False)
            print("📌 Staged AI_AGENT_CONTEXT.md for commit")

        return 0

    except Exception as e:
        print(f"❌ Error generating AI_AGENT_CONTEXT.md: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
