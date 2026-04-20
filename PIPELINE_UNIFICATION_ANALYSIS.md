# Pipeline Unification Analysis - Post PR#59 (8009090)

**Date**: 2026-04-20  
**Requirement**: Confirm every process follows the same pipeline after 8009090  
**Status**: ✅ **MIGRATION COMPLETE** - All paths now unified

**Update**: Migration completed on 2026-04-20. See `MIGRATION_COMPLETE.md` for full details.

---

## Summary

**BEFORE Migration** (Initial Analysis): NOT all ingestion processes followed the unified pipeline. There were TWO parallel repo ingestion paths.

**AFTER Migration** (Current Status): ✅ ALL ingestion processes now use the unified pipeline (`process_ingest_job`).

---

## Migration Results

### All Endpoints Now Unified ✅

**Endpoints using unified pipeline:**
1. ✅ `POST /v1/ingest/file` → File upload
2. ✅ `POST /v1/ingest/url` → URL ingestion  
3. ✅ `POST /v1/ingest/repo` → Repo ingestion
4. ✅ `POST /api/repos/add` → Legacy repo ingestion (MIGRATED)
5. ✅ `POST /api/repos/{repo_id}/retry` → Retry ingestion (MIGRATED)

**Architecture (Current)**:
```
ALL endpoints
  ↓
Create IngestJob (kind="file"|"url"|"repo")
  ↓
_enqueue(job_id)
  ↓
process_ingest_job(job_id)
  ↓
Switch on job.kind:
  ├─ "file" → _ingest_file()
  ├─ "url"  → _ingest_url()
  └─ "repo" → _ingest_repo()
```

**Model**: All use `IngestJob` table  
**Status tracking**: All use unified state machine (CREATED→STAGED→READY→QUEUED→RUNNING→PROCESSING→FINALIZING→SUCCESS/FAILED)  
**Location**: `backend/app/ingest_pipeline.py`

---

## Legacy Path Status

### Deprecated (but still functional) ⚠️

**Functions marked as deprecated:**
1. ⚠️ `worker.run_repo_ingestion()` - Deprecated, will be removed
2. ⚠️ `github_routes._enqueue_repo_ingestion()` - Deprecated, will be removed

**Status**: No longer called by any endpoints, but kept for backward compatibility. Will be removed in future release.

---

## Migration Implementation

### Changes Made

**1. `add_repo()` endpoint** (github_routes.py):
- Now creates `IngestJob` record
- Stores `repo_id` in `IngestJob.source_path` for legacy coordination
- Uses `_transition()` and `_enqueue()` from unified pipeline
- Maintains `Repo` table for backward compatibility

**2. `retry_repo_ingestion()` endpoint** (github_routes.py):
- Now creates new `IngestJob` for each retry
- Uses unified pipeline instead of legacy worker
- Deletes old chunks, updates Repo table

**3. `_ingest_repo()` function** (ingest_pipeline.py):
- Detects legacy `repo_id` in `source_path`
- Sets BOTH `repo_id` and `ingest_job_id` FKs on `RepoChunk`
- Updates `Repo` table on success with final counts

**4. `process_ingest_job()` function** (ingest_pipeline.py):
- Updates legacy `Repo` table on failure
- Sets `ingestion_status="failed"` for legacy endpoints

---

## Original Analysis (Pre-Migration)

## Key Differences

| Aspect | Unified Pipeline | Legacy Pipeline |
|--------|------------------|-----------------|
| Endpoint | `/v1/ingest/repo` | `/api/repos/add` |
| Model | `IngestJob` | `Repo` |
| Function | `process_ingest_job()` | `run_repo_ingestion()` |
| States | 8 states (full state machine) | 4 states (simple) |
| File staging | Uses `.ready` flag protocol | N/A (no file staging) |
| Atomic handoff | Yes (MQP-CONTRACT) | No |
| Location | `ingest_pipeline.py` | `worker.py` + `github_routes.py` |
| Job registry | Registered in `job_registry.py` | Also registered (dual!) |

---

## Evidence of Dual Registration

From `backend/app/job_registry.py`:
```python
JOB_REGISTRY: dict[str, Any] = {
    "process_ingest_job": process_ingest_job,      # NEW unified
    "run_repo_ingestion": run_repo_ingestion,      # OLD legacy
    # ...
}
```

Both functions are registered and can be enqueued to workers!

---

## Files Involved in Legacy Path

1. `backend/app/github_routes.py`:
   - Line 509: `_enqueue_repo_ingestion()` 
   - Line 755: `add_repo()` endpoint
   - Line 680: `retry_repo_ingestion()` endpoint

2. `backend/app/worker.py`:
   - Line 2805: `run_repo_ingestion()` implementation

3. `backend/app/job_registry.py`:
   - Line 57: Registration of `run_repo_ingestion`

---

## Impact Assessment

### What Works Correctly ✅
- File uploads via `/v1/ingest/file` → Unified pipeline
- URL ingestion via `/v1/ingest/url` → Unified pipeline
- Repo ingestion via `/v1/ingest/repo` → Unified pipeline

### What Uses Legacy Path ❌
- Repo ingestion via `/api/repos/add` → Legacy pipeline
- Retry ingestion via `/api/chat/.../repos/.../retry` → Legacy pipeline

### Risks of Dual Paths
1. **Inconsistent behavior** - Same operation behaves differently depending on endpoint
2. **Duplicate code maintenance** - Bug fixes must be applied to both paths
3. **Data model fragmentation** - `Repo` vs `IngestJob` tables serve similar purposes
4. **State machine inconsistency** - Different status values and transitions
5. **Contract violations** - Legacy path doesn't use FILE_STAGING_FINAL_INVARIANT_V2

---

## Recommendation

**CRITICAL**: The legacy repo ingestion path should be removed or migrated to use the unified pipeline.

### Option 1: Remove Legacy Endpoints (RECOMMENDED)
- Remove `POST /api/repos/add` 
- Remove `POST /api/chat/.../repos/.../retry`
- Update frontend to use `/v1/ingest/repo` instead
- Deprecate `run_repo_ingestion()` function
- Keep `Repo` table for historical data but use `IngestJob` for new ingestions

### Option 2: Redirect Legacy to Unified
- Make `add_repo()` create an `IngestJob` instead of calling `run_repo_ingestion()`
- Preserve backward compatibility while using unified pipeline internally

### Option 3: Document Dual Path (NOT RECOMMENDED)
- Accept that two paths exist
- Document when to use each
- Maintain both codebases in parallel

---

## Next Steps

1. **Identify frontend dependencies** on `/api/repos/add`
2. **Create migration plan** to unified pipeline
3. **Add deprecation warnings** to legacy endpoints
4. **Update integration tests** to use unified endpoints
5. **Remove legacy code** after migration complete

---

## Conclusion

**Answer to requirement**: ✅ YES, every process now follows the same pipeline after migration on 2026-04-20.

The unified `process_ingest_job()` pipeline is used by ALL endpoints. Legacy functions are deprecated but kept for one release cycle for safety.

**See `MIGRATION_COMPLETE.md` for complete migration documentation.**
