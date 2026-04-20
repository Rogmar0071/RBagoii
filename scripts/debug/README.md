# Debug Scripts

This directory contains debugging utilities for the RBagoii system.

## Available Tools

### health_check.py

Quick system health diagnostic tool.

**Usage:**
```bash
# Basic health check
python scripts/debug/health_check.py

# Verbose output
python scripts/debug/health_check.py --verbose

# JSON output (for automation)
python scripts/debug/health_check.py --json
```

**Checks:**
- Python environment (version 3.11+)
- Backend imports (FastAPI, SQLModel, etc.)
- Database connectivity
- Redis connectivity (if configured)
- Environment variables
- File structure integrity

**Exit codes:**
- `0`: Healthy or degraded (with warnings)
- `1`: Unhealthy (critical issues detected)

---

### analyze_logs.sh

Log file analyzer and pattern detector.

**Usage:**
```bash
# Analyze logs from last 24 hours
./scripts/debug/analyze_logs.sh

# Analyze logs from last hour
./scripts/debug/analyze_logs.sh --since "1 hour ago"

# Show only errors
./scripts/debug/analyze_logs.sh --errors-only

# Verbose output
./scripts/debug/analyze_logs.sh --verbose
```

**Features:**
- Searches standard log locations
- Detects Docker container logs
- Analyzes error patterns
- Counts exception types
- Reports HTTP error codes
- Provides actionable next steps

---

## Integration with Debugging Contract

These tools support the **Debugging Contract** methodology (see `DEBUGGING_CONTRACT.md`):

| Phase | Tool | Purpose |
|-------|------|---------|
| Phase 1: Assess | `health_check.py` | Quick system status check |
| Phase 2: Inventory | `health_check.py --verbose` | Component discovery |
| Phase 3: Logs | `analyze_logs.sh` | Log analysis and correlation |
| Phase 4: Trace | *(manual)* | Follow execution path |
| Phase 5: Contain | *(manual)* | Apply mitigation |
| Phase 6: Forensics | `git log` + `git blame` | Version control analysis |
| Phase 7: Context | *(manual)* | Human consultation |
| Phase 8: Fix | *(manual)* | Apply surgical fix |
| Phase 9: Document | `.github/INCIDENT_TEMPLATE.md` | Create incident report |

---

## Quick Start Debugging Workflow

When a system failure occurs:

1. **Run health check** to get system overview:
   ```bash
   python scripts/debug/health_check.py --verbose
   ```

2. **Analyze recent logs** for error patterns:
   ```bash
   ./scripts/debug/analyze_logs.sh --since "1 hour ago"
   ```

3. **Check recent changes** in version control:
   ```bash
   git log --oneline --since="1 hour ago"
   git log --oneline -- path/to/affected/file.py
   ```

4. **Create incident report** from template:
   ```bash
   cp .github/INCIDENT_TEMPLATE.md incidents/INC-$(date +%Y-%m-%d-%H%M).md
   # Fill in details following the 9-phase structure
   ```

5. **Follow debugging checklist**:
   - See `.github/DEBUGGING_CHECKLIST.md` for step-by-step guide

---

## Adding New Debug Tools

When adding new debugging tools to this directory:

1. **Make it executable**: `chmod +x scripts/debug/your_tool.sh`
2. **Add usage documentation**: Include `--help` flag
3. **Follow naming convention**: Use snake_case, descriptive names
4. **Update this README**: Add entry to "Available Tools" section
5. **Test it**: Verify it works from repository root

---

## Common Debugging Commands

### Backend Issues

```bash
# Check if backend can start
cd backend && python -c "from app.main import app; print('OK')"

# Run backend tests
cd backend && python -m pytest tests/ -v

# Validate mode engine
cd backend && python verify_mode_toggle_real.py

# Validate mutation governance
cd backend && python validate_invariants.py
```

### Database Issues

```bash
# Check database file
ls -lh app.db backend/app.db test_verify.db

# SQLite integrity check
sqlite3 app.db "PRAGMA integrity_check;"

# View table schemas
sqlite3 app.db ".schema"
```

### Queue Issues

```bash
# Check Redis connectivity
redis-cli ping

# Check queue depth
redis-cli llen rq:queue:default

# List failed jobs
redis-cli lrange rq:queue:failed 0 -1
```

### Docker Issues

```bash
# List running containers
docker ps

# View container logs
docker logs <container-name> --tail 100

# Execute command in container
docker exec -it <container-name> bash
```

---

## Emergency Procedures

### System is Down

1. Check health: `python scripts/debug/health_check.py`
2. Check logs: `./scripts/debug/analyze_logs.sh --errors-only`
3. Check recent changes: `git log --oneline -10`
4. Consider rollback: Follow `.github/DEBUGGING_CHECKLIST.md` Phase 5

### High Error Rate

1. Analyze patterns: `./scripts/debug/analyze_logs.sh`
2. Check specific endpoint logs
3. Verify external dependencies (OpenAI, Redis)
4. Check resource usage (disk, memory)

### Queue Stalled

1. Check Redis: `redis-cli ping`
2. Check worker logs: `docker logs <worker-container>`
3. Check queue depth: `redis-cli llen rq:queue:default`
4. Restart worker if needed

---

## See Also

- `DEBUGGING_CONTRACT.md` - Full debugging methodology
- `.github/DEBUGGING_CHECKLIST.md` - Step-by-step checklist
- `.github/INCIDENT_TEMPLATE.md` - Incident report template
- `AI_AGENT_CONTEXT.md` - System architecture overview

---

**Last Updated**: 2026-04-20
