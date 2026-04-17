# Auto-Sync AI Context System - Complete Solution

## Overview

This document describes the complete solution for keeping `AI_AGENT_CONTEXT.md` automatically synchronized with `README.md`, ensuring AI agents always have current project context before making any code changes.

## Problem Solved

AI agents need to understand:
1. Project structure and architecture
2. Linting standards (100 character line limit, no trailing whitespace)
3. Testing requirements
4. Immutable contracts (Mode Engine, Mutation Governance)
5. Development workflow

Without current context, AI agents may:
- Violate linting rules (E501, W293, W291)
- Break immutable contracts
- Skip required tests
- Use outdated API patterns

## Solution Architecture

### Three-Layer Auto-Sync System

```
┌─────────────────────────────────────────────────────────────────┐
│                     Developer Workflow                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    Edit README.md
                              │
                              ▼
                      git commit
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Layer 1: Pre-commit Hook (Local)                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  .pre-commit-config.yaml                                  │  │
│  │  - Hook: sync-ai-context                                  │  │
│  │  - Trigger: Files matching ^README\.md$                   │  │
│  │  - Action: Run scripts/sync_ai_context.py                 │  │
│  │  - Auto-stage: AI_AGENT_CONTEXT.md if changed             │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                      git push
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│          Layer 2: GitHub Actions (CI - Push Events)            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  .github/workflows/sync-ai-context.yml                    │  │
│  │  - Trigger: push with README.md changes                   │  │
│  │  - Action: Run sync script                                │  │
│  │  - Auto-commit: If AI_AGENT_CONTEXT.md out of sync        │  │
│  │  - Push: Automatic commit back to branch                  │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│       Layer 3: GitHub Actions (CI - PR Validation)             │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  .github/workflows/sync-ai-context.yml                    │  │
│  │  - Trigger: pull_request with README.md changes           │  │
│  │  - Action: Check if AI_AGENT_CONTEXT.md is in sync        │  │
│  │  - Fail CI: If out of sync (blocks merge)                 │  │
│  │  - Message: Instructions to run sync script               │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### 1. Sync Script (`scripts/sync_ai_context.py`)

**Purpose**: Extract key sections from README.md and generate AI_AGENT_CONTEXT.md

**Key Functions**:
- `extract_section()` - Extract markdown sections by title
- `extract_project_structure()` - Get directory tree
- `extract_dev_setup()` - Get development setup instructions
- `extract_testing()` - Get testing requirements
- `extract_env_vars()` - Get environment variables table
- `generate_ai_context()` - Combine all sections into chunked format

**Extracted Sections**:
1. Project description (from title and first paragraph)
2. Project structure (directory tree)
3. Development setup (pre-commit hooks, linting config)
4. Testing requirements (pytest commands, test suites)
5. Environment variables (OpenAI config, API keys)

**Added Context** (not in README):
- Linting error reference (E501, W293, W291)
- Mode Engine contracts (STRICT/VERIFIED/STANDARD)
- Mutation Governance pipeline
- AI agent guidelines and checklists
- Immutable contracts (DO NOT BREAK)
- Development workflow (for features, bugs, refactoring)

**Output Format**:
```markdown
# AI Agent Context - Read First Before Any Mutations

> **REQUIRED READING**: All AI agents must read...
> **AUTO-GENERATED**: This file is automatically updated...

## 🎯 Project Overview
## 📁 Critical Directory Structure
## 🛡️ Code Quality & Linting Standards
## 🔒 Backend Architecture & Contracts
## 🧪 Testing Requirements
## 🔧 Development Workflow
## 📦 Dependencies & Installation
## 🔐 Environment Variables
## 🚨 Critical Constraints
## 🎓 AI Agent Guidelines
## 📞 Getting Help
## ✅ Checklist Before Committing

**Last Updated**: YYYY-MM-DD
**Generated From**: README.md
```

### 2. Pre-commit Hook (`.pre-commit-config.yaml`)

**Configuration**:
```yaml
- repo: local
  hooks:
    - id: sync-ai-context
      name: Sync AI_AGENT_CONTEXT.md from README.md
      entry: python scripts/sync_ai_context.py
      language: system
      files: ^README\.md$
      pass_filenames: false
```

**Behavior**:
- Runs only when README.md is modified
- Executes sync script automatically
- Auto-stages AI_AGENT_CONTEXT.md if changed
- Developer sees updated file in commit

**Developer Experience**:
```bash
$ git add README.md
$ git commit -m "Update documentation"
[INFO] Sync AI_AGENT_CONTEXT.md from README.md...Passed
[copilot/branch abc1234] Update documentation
 2 files changed, 50 insertions(+), 20 deletions(-)
```

### 3. GitHub Actions Workflow (`.github/workflows/sync-ai-context.yml`)

**Triggers**:
- `push` event with README.md in changed files
- `pull_request` event with README.md in changed files

**Push Behavior**:
1. Checkout repository
2. Run sync script
3. Check if AI_AGENT_CONTEXT.md changed
4. If changed: Commit and push automatically
5. Continue workflow

**Pull Request Behavior**:
1. Checkout repository
2. Run sync script
3. Check if AI_AGENT_CONTEXT.md changed
4. If changed: **Fail CI with error message**
5. Block merge until sync is fixed

**Error Message** (on PR failure):
```
❌ AI_AGENT_CONTEXT.md is out of sync with README.md
Please run: python scripts/sync_ai_context.py
Or commit with pre-commit hooks enabled to auto-sync
```

## Usage

### For Developers

#### Initial Setup
```bash
./setup-dev-env.sh  # Installs pre-commit hooks including sync
```

#### Editing README.md
```bash
# Edit README.md
vim README.md

# Commit (sync happens automatically)
git add README.md
git commit -m "Update docs"
# ← AI_AGENT_CONTEXT.md synced and staged here

# Push
git push
```

#### Manual Sync (if needed)
```bash
python scripts/sync_ai_context.py
git add AI_AGENT_CONTEXT.md
git commit -m "Sync AI context"
```

### For AI Agents

Before making any code changes:
1. **Read AI_AGENT_CONTEXT.md** (always current)
2. Check linting rules (100 char line limit)
3. Understand immutable contracts
4. Review testing requirements
5. Follow development workflow guidelines

## Benefits

### 1. Always Current Context

| Before Auto-Sync | After Auto-Sync |
|------------------|-----------------|
| AI reads outdated README | AI reads current AI_AGENT_CONTEXT.md |
| Context may be weeks old | Context synced on every README change |
| AI may violate new rules | AI always has latest rules |
| Manual sync required | Automatic sync enforced |

### 2. Reduced Token Usage

AI_AGENT_CONTEXT.md is **chunked** for AI consumption:
- Removes unnecessary sections (Android app details, upload API)
- Focuses on essential information for code changes
- Adds AI-specific guidelines not in README
- Structured format optimized for AI parsing

### 3. Prevents Linting Failures

AI_AGENT_CONTEXT.md explicitly highlights:
- E501: Line too long (>100 chars)
- W293: Blank line contains whitespace
- W291: Trailing whitespace

AI agents see these rules **before** writing code.

### 4. Enforces Immutable Contracts

Clearly states:
- Mode Engine behavior (STRICT blocks, VERIFIED logs, STANDARD minimal)
- Mutation Governance pipeline (mandatory audit logging)
- Blueprint schema versioning
- API response shapes

AI agents understand what **cannot be changed**.

### 5. CI Enforcement

Three levels of protection:
1. **Local**: Pre-commit hook syncs automatically
2. **Push**: GitHub Actions auto-commits if out of sync
3. **PR**: CI fails if sync not run (blocks merge)

**No way to merge outdated AI context.**

## File Locations

| File | Purpose | Type |
|------|---------|------|
| `README.md` | **Source of truth** - Full project documentation | Manual edit |
| `AI_AGENT_CONTEXT.md` | **AI consumption** - Chunked, essential context | Auto-generated |
| `scripts/sync_ai_context.py` | **Generator** - Extracts and formats content | Tool |
| `.pre-commit-config.yaml` | **Local enforcement** - Runs sync on commit | Config |
| `.github/workflows/sync-ai-context.yml` | **CI enforcement** - Syncs or fails CI | Workflow |

## Maintenance

### Updating the Sync Logic

To change what gets extracted or how it's formatted:

1. Edit `scripts/sync_ai_context.py`
2. Modify extraction functions or template
3. Test: `python scripts/sync_ai_context.py`
4. Verify output: `less AI_AGENT_CONTEXT.md`
5. Commit changes

### Adding New Sections

To extract additional README sections:

1. Add extraction function in `sync_ai_context.py`:
   ```python
   def extract_new_section(readme_content: str) -> str:
       return extract_section(readme_content, "New Section Title")
   ```

2. Call in `generate_ai_context()`:
   ```python
   new_section = extract_new_section(readme_content)
   ```

3. Add to template:
   ```python
   context = f"""
   ...
   ## New Section
   {new_section}
   ...
   """
   ```

### Testing Changes

```bash
# Test sync script
python scripts/sync_ai_context.py

# Test pre-commit hook
pre-commit run sync-ai-context --files README.md

# Test CI workflow locally (requires act)
act push -j sync-ai-context
```

## Monitoring

### Check Sync Status

```bash
# Check if in sync
python scripts/sync_ai_context.py
# Output: "✅ AI_AGENT_CONTEXT.md is already up to date"

# Force regenerate
rm AI_AGENT_CONTEXT.md
python scripts/sync_ai_context.py
# Output: "✅ Successfully updated AI_AGENT_CONTEXT.md"
```

### CI Status

- Check GitHub Actions tab for "Sync AI Context" workflow
- Green ✅ = In sync
- Red ❌ = Out of sync (on PR only)
- Auto-commit on push branches

## Troubleshooting

### Sync Script Fails

**Symptom**: `python scripts/sync_ai_context.py` exits with error

**Solutions**:
1. Check Python version: `python --version` (requires 3.11+)
2. Check README.md exists
3. Check file permissions: `ls -l scripts/sync_ai_context.py`
4. Check for syntax errors in README.md sections

### Pre-commit Hook Not Running

**Symptom**: README.md changes committed but AI_AGENT_CONTEXT.md not updated

**Solutions**:
1. Verify pre-commit installed: `pre-commit --version`
2. Verify hooks installed: `ls -la .git/hooks/pre-commit`
3. Reinstall hooks: `pre-commit install`
4. Test hook: `pre-commit run sync-ai-context --files README.md`

### CI Workflow Not Triggering

**Symptom**: Push with README.md changes but workflow doesn't run

**Solutions**:
1. Check workflow file exists: `cat .github/workflows/sync-ai-context.yml`
2. Check workflow syntax: Use GitHub's workflow validator
3. Check branch protection rules
4. Manually trigger: GitHub Actions tab → "Sync AI Context" → "Run workflow"

### AI_AGENT_CONTEXT.md Out of Sync in PR

**Symptom**: CI fails with "out of sync" message

**Solutions**:
```bash
# Run sync manually
python scripts/sync_ai_context.py

# Stage and commit
git add AI_AGENT_CONTEXT.md
git commit -m "Sync AI context with README"
git push
```

## Success Metrics

### Before Implementation
- ❌ AI agents read potentially outdated README
- ❌ No AI-specific guidelines
- ❌ Linting rules not highlighted
- ❌ Manual sync required

### After Implementation
- ✅ AI agents read current AI_AGENT_CONTEXT.md (auto-synced)
- ✅ AI-specific guidelines and checklists included
- ✅ Linting rules prominently displayed
- ✅ Automatic sync on every README change
- ✅ CI enforcement prevents outdated context

## Related Documentation

- **README.md** - Full project documentation (source)
- **AI_AGENT_CONTEXT.md** - Chunked AI context (generated)
- **LINTING_PREVENTION_SYSTEM.md** - Complete pre-commit system
- **.pre-commit-config.yaml** - Hook configuration
- **scripts/sync_ai_context.py** - Sync script implementation

---

**Version**: 1.0
**Last Updated**: 2026-04-17
**Status**: ✅ Implemented and tested
