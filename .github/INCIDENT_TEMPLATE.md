# Incident Report: [Brief Description]

**Incident ID**: INC-YYYY-MM-DD-NNN  
**Severity**: [Critical | High | Medium | Low]  
**Status**: [Investigating | Mitigated | Resolved | Closed]  
**Reported**: YYYY-MM-DD HH:MM UTC  
**Resolved**: YYYY-MM-DD HH:MM UTC *(if applicable)*

---

## Phase 1: Stop & Assess

### Failure Statement
<!-- One sentence describing what failed -->


### Timeline
- **First detected**: YYYY-MM-DD HH:MM UTC
- **Last known good**: YYYY-MM-DD HH:MM UTC
- **Recent changes**: 
  - Commit: `<hash>` - <description>
  - Deploy: <timestamp> to <environment>

### Scope
- **Affected components**: 
- **Affected users**: 
- **Still functioning**: 

---

## Phase 2: System Inventory

### Components Involved
- [ ] Backend API (Python/FastAPI)
- [ ] Database (SQLite/PostgreSQL)
- [ ] Queue system (Redis RQ)
- [ ] Frontend/UI
- [ ] External APIs (OpenAI, etc.)
- [ ] File storage
- [ ] CI/CD pipeline
- [ ] Other: ___________

### Environment
- **Platform**: [Local | Render | Docker | Other]
- **Python version**: 
- **Dependencies**: *(any relevant versions)*

### Unknowns
<!-- Mark items that need investigation with UNCONFIRMED -->
- UNCONFIRMED: 

---

## Phase 3: Logs & Evidence

### Log Excerpts
```
[Paste relevant log lines here with timestamps]
```

### Error Patterns
<!-- What errors appear repeatedly? -->


### Correlations
<!-- What happened at the same time? -->


### Hypotheses (ranked)
1. **Most likely**: 
2. **Possible**: 
3. **Unlikely but check**: 

---

## Phase 4: Execution Flow Trace

### Entry Point
<!-- How did the failure trigger? (API call, cron job, user action, etc.) -->


### Data Flow
```
Request → [Component A] → [Component B] → [FAILURE HERE] → [Component C]
```

### Unclear Logic
<!-- Note any confusing or suspect code paths -->


---

## Phase 5: Blast Radius Containment

### Mitigation Options
- [ ] **Option 1**: [Description]
  - Reversible: Yes/No
  - Risk: Low/Medium/High
  - Implementation: [Steps]

- [ ] **Option 2**: [Description]
  - Reversible: Yes/No
  - Risk: Low/Medium/High
  - Implementation: [Steps]

### Chosen Mitigation
<!-- Which option was selected and why -->


### Rollback Plan
<!-- How to undo the mitigation if it fails -->


---

## Phase 6: Version Control Forensics

### Relevant Commits
| Commit | Date | Author | Description | Suspicion |
|--------|------|--------|-------------|-----------|
| `<hash>` | YYYY-MM-DD | @user | Description | High/Medium/Low |

### Historical Context
<!-- Have we seen this before? Related fixes? -->


---

## Phase 7: Context from Humans

### Team Consultation
- **Consulted**: @user1, @user2
- **Notes**: 
  - Known edge cases: 
  - Recalled behaviors: 
  - Environment quirks: 

### Issue Tracker References
- Related issues: #123, #456
- Related PRs: #789

---

## Phase 8: Fix Implementation

### Changes Made
<!-- List specific changes -->
- File: `path/to/file.py`
  - Change: [Description]
  - Why: [Explanation]
  - Reversible: Yes/No

### Verification Steps
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Manual smoke test: [Description]
- [ ] Monitoring shows normal behavior

### Post-Fix Monitoring
<!-- What to watch for in the next 24-48 hours -->
- [ ] Error rates
- [ ] Response times
- [ ] Queue depth
- [ ] Other: ___________

---

## Phase 9: Documentation & Lessons

### What We Learned
<!-- What did the system actually do that we didn't know? -->


### Hidden Dependencies
<!-- What dependencies or constraints weren't obvious? -->


### How to Debug This Faster Next Time
1. Check _____ first
2. Look for _____ in logs
3. Validate _____ assumption

### Documentation Updates
- [ ] Updated AI_AGENT_CONTEXT.md with new knowledge
- [ ] Added inline comments to tricky code
- [ ] Updated runbook/playbook
- [ ] Created regression test

### Follow-Up Actions
- [ ] Action item 1 - Owner: @user - Due: YYYY-MM-DD
- [ ] Action item 2 - Owner: @user - Due: YYYY-MM-DD

---

## Success Criteria Checklist

- [ ] System is stable
- [ ] Fix can be rolled back cleanly
- [ ] Future debuggers have fewer unknowns
- [ ] Risk is reduced, not hidden
- [ ] Monitoring/observability improved
- [ ] Documentation updated

---

## Appendix

### Additional Context
<!-- Any other relevant information -->


### Related Incidents
<!-- Links to similar past incidents -->


---

**Report prepared by**: @username  
**Reviewed by**: @username  
**Sign-off by**: @username
