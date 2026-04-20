# 🔍 Quick Debugging Checklist

Use this checklist when investigating system failures. Based on the Debugging Contract phases.

---

## ⚡ Phase 1: Stop & Assess (5 minutes)

- [ ] **Write one-sentence failure description**
  - What component failed?
  - What error/symptom?
  - What scope (who/what affected)?

- [ ] **Establish timeline**
  - [ ] When first detected? `________________`
  - [ ] Last known working time? `________________`
  - [ ] Recent changes? `git log --oneline -10`

- [ ] **Determine scope**
  - [ ] Is it affecting all users or specific subset?
  - [ ] Is it affecting all features or specific endpoint?
  - [ ] What's still working?

**Output**: One sentence like "Backend /api/blueprints/compile returning 500 errors since 14:30 UTC; only affects authenticated requests"

---

## 🗺️ Phase 2: System Inventory (5 minutes)

- [ ] **Core components** (check all that apply):
  - [ ] Backend API (Python/FastAPI)
  - [ ] Database (SQLite/PostgreSQL)
  - [ ] Redis queue
  - [ ] Worker processes
  - [ ] External APIs (OpenAI)
  - [ ] File storage/uploads

- [ ] **Environment details**:
  - [ ] Platform: `________________`
  - [ ] Python version: `python3 --version`
  - [ ] Dependencies: Check `requirements.txt` vs installed

- [ ] **Mark unknowns as UNCONFIRMED**

**Output**: Bullet-point inventory with "UNCONFIRMED" markers

---

## 📋 Phase 3: Logs Before Code (10 minutes)

- [ ] **Check application logs** (in order):
  - [ ] Backend stdout/stderr: `docker logs <container>` or `tail -f logs/app.log`
  - [ ] Worker logs: Check queue processing logs
  - [ ] Web server logs: nginx/uvicorn access logs
  - [ ] Database logs: Query errors, slow queries
  - [ ] Cloud platform logs: Render/AWS/GCP dashboard

- [ ] **Extract evidence**:
  - [ ] Copy relevant error messages with timestamps
  - [ ] Note error codes (500, 404, timeout, etc.)
  - [ ] Find correlation (same timestamp, same user, same pattern)

- [ ] **Rank hypotheses**:
  1. Most likely: `________________`
  2. Possible: `________________`
  3. Check anyway: `________________`

**Output**: Quoted log excerpts + ranked hypotheses

---

## 🔬 Phase 4: Flow Tracing (15 minutes)

- [ ] **Identify entry point**:
  - [ ] API endpoint: `________________`
  - [ ] Cron job: `________________`
  - [ ] Queue task: `________________`
  - [ ] User action: `________________`

- [ ] **Trace execution**:
  ```
  Request → [File/Function] → [File/Function] → [BREAK HERE?] → [File/Function]
  ```

- [ ] **Note suspicious logic**:
  - [ ] Null checks missing?
  - [ ] Unexpected defaults?
  - [ ] Conditional branches unclear?
  - [ ] State mutation unclear?

**Output**: Linear flow with "UNCLEAR" or "SUSPICIOUS" callouts

---

## 🎯 Phase 5: Containment (10 minutes)

- [ ] **Mitigation options** (pick one):
  - [ ] **Rollback**: `git revert <hash>` + redeploy
  - [ ] **Feature flag**: Disable failing feature
  - [ ] **Route around**: Use fallback/mock
  - [ ] **Scale down**: Reduce load on failing component
  - [ ] **Circuit breaker**: Temporarily disable integration

- [ ] **Chosen mitigation**: `________________`
  - [ ] Is it reversible? Yes/No
  - [ ] What's the rollback plan? `________________`
  - [ ] What's the risk? Low/Medium/High

**Output**: Mitigation plan + rollback plan + risk assessment

---

## 🕵️ Phase 6: Git Forensics (5 minutes)

- [ ] **Recent commits** touching affected area:
  ```bash
  git log --oneline -20
  git log --oneline -- path/to/affected/file.py
  git blame path/to/affected/file.py
  ```

- [ ] **Extract context**:
  - [ ] Commit hash: `________________`
  - [ ] Intent (from message): `________________`
  - [ ] Related deploy: `________________`

- [ ] **Historical patterns**:
  - [ ] Similar fixes before? `git log --grep="<keyword>"`
  - [ ] Reverts in history? `git log --grep="revert"`

**Output**: Relevant commit hashes + observed patterns

---

## 👥 Phase 7: Human Context (5 minutes)

- [ ] **Consult** (if available):
  - [ ] Team lead
  - [ ] Original author (from git blame)
  - [ ] QA/testers
  - [ ] Support team

- [ ] **Mine for constraints**:
  - [ ] Known edge cases? `________________`
  - [ ] "We tried this before and...": `________________`
  - [ ] Environment quirks? `________________`

- [ ] **Check documentation**:
  - [ ] Related issues: `________________`
  - [ ] Slack/chat history
  - [ ] Ticket system

**Output**: Notes on recalled behaviors and forgotten constraints

---

## 🔧 Phase 8: Surgical Fix (varies)

- [ ] **Make ONE change**:
  - [ ] File: `________________`
  - [ ] Change: `________________`
  - [ ] Why (not just what): `________________`

- [ ] **Make it observable**:
  - [ ] Added logging? Yes/No
  - [ ] Added metrics? Yes/No
  - [ ] Can monitor the fix? Yes/No

- [ ] **Make it reversible**:
  - [ ] Can revert commit? Yes/No
  - [ ] Can toggle feature flag? Yes/No
  - [ ] Can rollback deploy? Yes/No

- [ ] **Verify fix**:
  - [ ] Unit tests pass: `pytest tests/test_*.py`
  - [ ] Integration tests pass: `pytest backend/tests/`
  - [ ] Manual smoke test: `________________`
  - [ ] Monitoring normal: Check dashboards

**Output**: Patch summary + verification steps + monitoring plan

---

## 📚 Phase 9: Document (10 minutes)

- [ ] **Inline comments**:
  - [ ] Added "why" comments to tricky code? Yes/No
  - [ ] Documented workarounds? Yes/No

- [ ] **Incident report**:
  - [ ] Created from `.github/INCIDENT_TEMPLATE.md`
  - [ ] Filled all 9 phases
  - [ ] Added to repository

- [ ] **Knowledge updates**:
  - [ ] Updated `AI_AGENT_CONTEXT.md`? Yes/No
  - [ ] Updated `README.md` if user-facing? Yes/No
  - [ ] Created regression test? Yes/No

- [ ] **What future debuggers need to know**:
  1. Hidden dependency: `________________`
  2. Tricky assumption: `________________`
  3. Debug faster by: `________________`

**Output**: Incident report + updated docs + breadcrumb comments

---

## ✅ Success Checklist

Before closing the incident:

- [ ] System is stable (not just "seems okay")
- [ ] Fix is reversible (tested rollback procedure)
- [ ] Monitoring shows normal behavior (at least 30 min)
- [ ] Documentation updated (AI_AGENT_CONTEXT.md, inline comments)
- [ ] Future debuggers have roadmap (incident report filed)
- [ ] Risk reduced (not just hidden with workaround)

---

## 🚨 Emergency Commands (RBagoii-Specific)

```bash
# Quick health check
curl http://localhost:8000/health 2>/dev/null | jq .

# Check recent logs (last 50 lines)
docker logs --tail 50 <container-name>

# Check queue depth
redis-cli llen <queue-name>

# Test database connectivity
sqlite3 app.db "SELECT count(*) FROM analysis_jobs;"

# Check running processes
ps aux | grep -E "(python|uvicorn|worker)"

# Check disk space (uploads can fill disk)
df -h /tmp/uploads

# Check recent git changes
git log --oneline --since="24 hours ago"

# Run mode verification
cd backend && python verify_mode_toggle_real.py

# Run boundary validation
cd backend && python validate_boundary_lock.py

# Quick lint check (catches common issues)
ruff check backend/app/<affected-file>.py

# Run specific test
pytest backend/tests/test_<affected-module>.py -v

# Check environment variables
env | grep -E "(API_KEY|OPENAI|REDIS|DATABASE)"
```

---

## 📞 Escalation Criteria

Escalate immediately if:

- [ ] Data loss or corruption detected
- [ ] Security breach suspected
- [ ] Multiple systems failing (cascading failure)
- [ ] Cannot contain blast radius
- [ ] Rollback fails
- [ ] External dependencies permanently down

---

**Pro Tips**:

1. ⏱️ **Time-box each phase** - Don't get stuck analyzing
2. 📝 **Document as you go** - Don't wait until the end
3. 🔄 **Iterate through phases** - It's not always linear
4. 🎯 **Stabilize first** - Elegance comes later
5. 🧪 **Test rollback** - Before you need it
6. 📊 **Add metrics** - For next time
7. 🗣️ **Communicate status** - Keep stakeholders informed

---

**Estimated total time**: 60-90 minutes for typical incident  
**Based on**: DEBUGGING_CONTRACT.md  
**Last updated**: 2026-04-20
