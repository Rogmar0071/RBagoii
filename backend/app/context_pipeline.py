"""
backend.app.context_pipeline
==============================
MQP-CONTRACT: RBOII-PHASE1-SEAL + PHASE3-PIPELINE-SPINE v1.0
MQP-CONTRACT: PHASE3-PIPELINE-EXECUTION-LOCK v1.0

Phase 3: Knowledge Assembly Pipeline.

Single entry point::

    run_context_pipeline(job_id, session, ...) -> ActiveContextSession

Reads ONLY from Phase 1 structural tables (sealed — not modified here):
    RepoFile / CodeSymbol / FileDependency / SymbolCallEdge / EntryPoint

Eight deterministic internal stages, executed in order:

    _stage_normalize        -> NormalizedArtifactSet
    _stage_structural_graph -> StructuralGraph
    _stage_enrich           -> EnrichedGraph
    _stage_link             -> ContextGraph
    _stage_gap_detect       -> ContextGaps
    _stage_user_align       -> AlignedIntentContract  (HARD STOP)
    _stage_finalize         -> FinalContext
    _stage_activate         -> ActiveContextSession

SYSTEM LAW:
    • ALL execution passes through run_context_pipeline.
    • NO stage logic exists outside this file.
    • _stage_activate ONLY runs when AlignedIntentContract.valid is True.
    • If alignment is not confirmed, AlignmentRequiredError is raised and the
      pipeline STOPS.  No bypass is permitted.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _PipelineToken — internal anti-bypass execution token
# ---------------------------------------------------------------------------


class _PipelineToken:
    """Internal pipeline execution token.

    MQP-CONTRACT: PHASE3-ACTIVATION-AUTHORITY-LOCK v1.0

    RULES:
    • Generated ONLY inside run_context_pipeline().
    • Passed internally from run_context_pipeline → _stage_activate.
    • _stage_activate MUST receive a valid _PipelineToken instance.
    • ActiveContextSession MUST be constructed with a valid _PipelineToken.
    • No external code path can produce an ActiveContextSession without
      first obtaining this token, which is only possible through the
      full pipeline execution.

    Any activation attempt without a _PipelineToken raises:
        RuntimeError("ACTIVATION_BLOCKED: pipeline_token required")
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: str = str(uuid.uuid4())

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "_PipelineToken(valid=True)"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AlignmentRequiredError(Exception):
    """Raised by _stage_user_align when the user has not confirmed alignment.

    The pipeline MUST stop.  Callers must present the alignment summary to
    the user and re-invoke run_context_pipeline with alignment_confirmed=True.

    Attributes
    ----------
    summary:
        A dict that MUST be shown to the user before re-invocation.
    """

    def __init__(self, summary: dict) -> None:
        super().__init__("User alignment required before pipeline can continue.")
        self.summary = summary


# ---------------------------------------------------------------------------
# Stage 1 output — NormalizedArtifactSet
# ---------------------------------------------------------------------------


@dataclass
class NormalizedArtifact:
    """Single normalized artifact loaded from the DB."""

    artifact_id: str
    artifact_type: str          # "repo_file"
    path: str
    language: Optional[str]
    content_hash: Optional[str]
    size_bytes: int


@dataclass
class NormalizedArtifactSet:
    """Stage 1 output: all Phase 1 rows loaded and normalised for this job."""

    job_id: str
    artifacts: List[NormalizedArtifact] = field(default_factory=list)
    symbol_count: int = 0
    dependency_count: int = 0
    entry_point_count: int = 0


# ---------------------------------------------------------------------------
# Stage 2 output — StructuralGraph
# ---------------------------------------------------------------------------


@dataclass
class GraphFile:
    file_id: str
    path: str
    language: Optional[str]
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    # IDs of files this file depends on
    depends_on: List[str] = field(default_factory=list)
    is_entry: bool = False
    entry_type: Optional[str] = None


@dataclass
class StructuralGraph:
    """Stage 2 output: structural view of the Phase 1 graph for this job."""

    job_id: str
    files: Dict[str, GraphFile] = field(default_factory=dict)      # file_id -> GraphFile
    symbols: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # symbol_id -> attrs
    call_edges: List[Dict[str, Any]] = field(default_factory=list)
    entry_points: List[Dict[str, Any]] = field(default_factory=list)
    # Drops logged here — NO silent discard
    dropped_imports: List[str] = field(default_factory=list)
    dropped_calls: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 3 output — EnrichedGraph
# ---------------------------------------------------------------------------

# File roles
FILE_ROLE_ENTRY = "entry"
FILE_ROLE_SERVICE = "service"
FILE_ROLE_MODEL = "model"
FILE_ROLE_CONFIG = "config"
FILE_ROLE_UTIL = "util"

# Symbol roles
SYMBOL_ROLE_ORCHESTRATOR = "orchestrator"
SYMBOL_ROLE_TRANSFORMER = "transformer"
SYMBOL_ROLE_LEAF = "leaf"


@dataclass
class EnrichedGraph:
    """Stage 3 output: StructuralGraph with file/symbol role annotations.

    RULE: NO new nodes.  NO inferred structure.  Only existing nodes annotated.
    """

    structural_graph: StructuralGraph
    file_roles: Dict[str, str] = field(default_factory=dict)     # file_id -> role
    symbol_roles: Dict[str, str] = field(default_factory=dict)   # symbol_id -> role


# ---------------------------------------------------------------------------
# Stage 4 output — ContextGraph
# ---------------------------------------------------------------------------


@dataclass
class ContextLink:
    """A traceable link between a user-intent keyword and a graph node."""

    keyword: str
    file_id: Optional[str] = None
    symbol_id: Optional[str] = None
    link_type: str = "file"   # "file" | "symbol"


@dataclass
class ContextGraph:
    """Stage 4 output: EnrichedGraph with intent-to-graph links.

    RULE: ALL links are traceable.  NO floating nodes.
    """

    enriched_graph: EnrichedGraph
    user_intent: str
    links: List[ContextLink] = field(default_factory=list)
    execution_paths: List[List[str]] = field(default_factory=list)  # lists of file_ids


# ---------------------------------------------------------------------------
# Stage 5 output — ContextGaps
# ---------------------------------------------------------------------------

GAP_SEVERITY_CRITICAL = "critical"
GAP_SEVERITY_WARNING = "warning"

GAP_NO_ENTRY_POINTS = "no_entry_points"
GAP_UNRESOLVED_CALLS = "unresolved_calls"
GAP_AMBIGUOUS_INTENT = "ambiguous_intent"
GAP_MISSING_EXECUTION_PATH = "missing_execution_path"


@dataclass
class ContextGap:
    """A single detected gap in the structural or intent context."""

    gap_type: str
    description: str
    severity: str   # "critical" | "warning"


@dataclass
class ContextGaps:
    """Stage 5 output: all detected gaps.

    RULE: EVERY gap logged.  NO silent ignore.
    """

    gaps: List[ContextGap] = field(default_factory=list)

    @property
    def has_critical_gaps(self) -> bool:
        return any(g.severity == GAP_SEVERITY_CRITICAL for g in self.gaps)

    @property
    def critical_gaps(self) -> List[ContextGap]:
        return [g for g in self.gaps if g.severity == GAP_SEVERITY_CRITICAL]

    @property
    def warning_gaps(self) -> List[ContextGap]:
        return [g for g in self.gaps if g.severity == GAP_SEVERITY_WARNING]


# ---------------------------------------------------------------------------
# Stage 6 output — AlignedIntentContract
# ---------------------------------------------------------------------------


@dataclass
class AlignedIntentContract:
    """Stage 6 output: user-confirmed intent alignment contract.

    RULE: valid == True is the only gate to _stage_activate.
    """

    job_id: str
    user_intent: str
    confirmed: bool
    refinement: Optional[str]
    # Summary shown to the user before confirmation (always populated)
    system_summary: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def valid(self) -> bool:
        """True only when the user has explicitly confirmed alignment."""
        return self.confirmed


# ---------------------------------------------------------------------------
# Stage 7 output — FinalContext
# ---------------------------------------------------------------------------


@dataclass
class FinalContext:
    """Stage 7 output: fully resolved context, ready for activation.

    RULE: NO unresolved critical gaps.
    """

    context_graph: ContextGraph
    aligned_intent_contract: AlignedIntentContract
    validated_execution_paths: List[List[str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 8 output — ActiveContextSession
# ---------------------------------------------------------------------------


@dataclass
class ActiveContextSession:
    """Stage 8 output: live, queryable context session.

    Enables:
    - execution simulation
    - path validation
    - structural reasoning

    RULE: Activation is immediate once contract is valid.

    MQP-CONTRACT: PHASE3-ACTIVATION-AUTHORITY-LOCK v1.0
    Creation requires a valid _PipelineToken generated by run_context_pipeline().
    Any direct construction attempt without the token raises ACTIVATION_BLOCKED.
    """

    session_id: str
    job_id: str
    final_context: FinalContext
    activated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Internal pipeline token — must be a _PipelineToken instance.
    # Default is None so that missing-token attempts are caught in __post_init__.
    _token: Optional[Any] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self._token, _PipelineToken):
            raise RuntimeError(
                "ACTIVATION_BLOCKED: ActiveContextSession can only be created "
                "inside run_context_pipeline() with a valid _PipelineToken.  "
                "Direct construction is forbidden."
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _role_for_file(path: str, has_entry: bool, outbound_call_count: int) -> str:
    """Deterministically assign a file role from its path and graph position."""
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    if has_entry:
        return FILE_ROLE_ENTRY
    if any(kw in name for kw in ("service", "server", "api", "app", "router", "routes")):
        return FILE_ROLE_SERVICE
    if any(kw in name for kw in ("model", "schema", "entity", "domain")):
        return FILE_ROLE_MODEL
    if any(kw in name for kw in ("config", "settings", "conf", "env")):
        return FILE_ROLE_CONFIG
    return FILE_ROLE_UTIL


def _role_for_symbol(outbound_calls: int) -> str:
    """Assign symbol role based on how many symbols it calls."""
    if outbound_calls >= 3:
        return SYMBOL_ROLE_ORCHESTRATOR
    if outbound_calls >= 1:
        return SYMBOL_ROLE_TRANSFORMER
    return SYMBOL_ROLE_LEAF


def _extract_intent_keywords(intent: str) -> List[str]:
    """Extract meaningful, lowercase keywords from free-form intent text."""
    _STOPWORDS = frozenset({
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "as", "is", "was", "are",
        "were", "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "could", "should", "may", "i", "want",
        "need", "show", "me", "my", "the", "how", "what", "which",
    })
    words = [w.strip(".,;:!?\"'") for w in intent.lower().split()]
    return [w for w in words if w and w not in _STOPWORDS and len(w) > 1]


# ---------------------------------------------------------------------------
# Stage implementations (ALL PRIVATE — no external calls allowed)
# ---------------------------------------------------------------------------


def _stage_normalize(job_id: str, session: Any) -> NormalizedArtifactSet:
    """
    Stage 1 — Input Capture.

    Loads all Phase 1 rows for *job_id* from the DB and converts them into
    a NormalizedArtifactSet.  This is the SOLE entry point into Phase 1 data.
    """
    from sqlmodel import select

    from backend.app.models import (
        CodeSymbol,
        EntryPoint,
        FileDependency,
        IngestJob,
        RepoFile,
    )

    job_uuid = uuid.UUID(job_id)
    ingest_job = session.get(IngestJob, job_uuid)
    repo_uuid = ingest_job.repo_id if ingest_job and ingest_job.repo_id else job_uuid
    repo_files = list(session.exec(select(RepoFile).where(RepoFile.repo_id == repo_uuid)))

    artifacts = [
        NormalizedArtifact(
            artifact_id=str(rf.id),
            artifact_type="repo_file",
            path=rf.path,
            language=rf.language,
            content_hash=rf.content_hash,
            size_bytes=rf.size_bytes,
        )
        for rf in repo_files
    ]

    file_ids = {rf.id for rf in repo_files}
    symbols = list(session.exec(
        select(CodeSymbol).where(CodeSymbol.file_id.in_(file_ids))
    )) if file_ids else []
    deps = list(session.exec(
        select(FileDependency).where(FileDependency.source_file_id.in_(file_ids))
    )) if file_ids else []
    eps = list(session.exec(
        select(EntryPoint).where(EntryPoint.file_id.in_(file_ids))
    )) if file_ids else []

    result = NormalizedArtifactSet(
        job_id=job_id,
        artifacts=artifacts,
        symbol_count=len(symbols),
        dependency_count=len(deps),
        entry_point_count=len(eps),
    )

    logger.info(
        "STAGE_NORMALIZE: job=%s files=%d symbols=%d deps=%d entries=%d",
        job_id, len(artifacts), len(symbols), len(deps), len(eps),
    )
    return result


def _stage_structural_graph(job_id: str, session: Any) -> StructuralGraph:
    """
    Stage 2 — Structural Graph.

    Builds a StructuralGraph from Phase 1 DB tables for *job_id*.
    ONLY reads from RepoFile / CodeSymbol / FileDependency /
    SymbolCallEdge / EntryPoint.  NO alternate extraction.
    """
    from sqlmodel import select

    from backend.app.models import (
        CodeSymbol,
        EntryPoint,
        FileDependency,
        IngestJob,
        RepoFile,
        SymbolCallEdge,
    )

    job_uuid = uuid.UUID(job_id)
    ingest_job = session.get(IngestJob, job_uuid)
    repo_uuid = ingest_job.repo_id if ingest_job and ingest_job.repo_id else job_uuid
    graph = StructuralGraph(job_id=job_id)

    # ---- RepoFile rows ----
    repo_files = list(session.exec(select(RepoFile).where(RepoFile.repo_id == repo_uuid)))
    for rf in repo_files:
        graph.files[str(rf.id)] = GraphFile(
            file_id=str(rf.id),
            path=rf.path,
            language=rf.language,
        )

    if not graph.files:
        logger.warning("STAGE_STRUCTURAL_GRAPH: no RepoFile rows for job=%s", job_id)
        return graph

    file_ids = {rf.id for rf in repo_files}

    # ---- CodeSymbol rows ----
    symbols = list(session.exec(
        select(CodeSymbol).where(CodeSymbol.file_id.in_(file_ids))
    ))
    for sym in symbols:
        sym_dict = {
            "symbol_id": str(sym.id),
            "file_id": str(sym.file_id),
            "name": sym.name,
            "symbol_type": sym.symbol_type,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
        }
        graph.symbols[str(sym.id)] = sym_dict
        gf = graph.files.get(str(sym.file_id))
        if gf:
            gf.symbols.append(sym_dict)

    # ---- FileDependency rows ----
    deps = list(session.exec(
        select(FileDependency).where(FileDependency.source_file_id.in_(file_ids))
    ))
    for dep in deps:
        gf = graph.files.get(str(dep.source_file_id))
        if gf and str(dep.target_file_id) in graph.files:
            gf.depends_on.append(str(dep.target_file_id))
        else:
            # Should never happen (Phase 1 guarantee) — log the drop
            graph.dropped_imports.append(
                f"dep {dep.id}: source={dep.source_file_id} target={dep.target_file_id}"
            )

    # ---- SymbolCallEdge rows ----
    sym_ids = {sym.id for sym in symbols}
    call_edges = list(session.exec(
        select(SymbolCallEdge).where(SymbolCallEdge.source_symbol_id.in_(sym_ids))
    )) if sym_ids else []
    for edge in call_edges:
        if edge.target_symbol_id is None:
            # Unresolved callee — log the drop per STRICT EDGE POLICY
            graph.dropped_calls.append(
                f"edge {edge.id}: caller={edge.source_symbol_id} callee={edge.callee_name} "
                f"(target unresolved — dropped)"
            )
            continue
        graph.call_edges.append({
            "edge_id": str(edge.id),
            "source_symbol_id": str(edge.source_symbol_id),
            "target_symbol_id": str(edge.target_symbol_id),
            "callee_name": edge.callee_name,
        })

    # ---- EntryPoint rows ----
    eps = list(session.exec(
        select(EntryPoint).where(EntryPoint.file_id.in_(file_ids))
    ))
    for ep in eps:
        graph.entry_points.append({
            "entry_point_id": str(ep.id),
            "file_id": str(ep.file_id),
            "entry_type": ep.entry_type,
            "line": ep.line,
        })
        gf = graph.files.get(str(ep.file_id))
        if gf:
            gf.is_entry = True
            gf.entry_type = ep.entry_type

    logger.info(
        "STAGE_STRUCTURAL_GRAPH: job=%s files=%d symbols=%d edges=%d entries=%d "
        "dropped_imports=%d dropped_calls=%d",
        job_id,
        len(graph.files),
        len(graph.symbols),
        len(graph.call_edges),
        len(graph.entry_points),
        len(graph.dropped_imports),
        len(graph.dropped_calls),
    )
    return graph


def _stage_enrich(structural_graph: StructuralGraph) -> EnrichedGraph:
    """
    Stage 3 — Semantic Enrichment.

    Annotates ONLY existing nodes.  NO new nodes created.  NO inferred structure.

    File roles  : entry | service | model | config | util
    Symbol roles: orchestrator | transformer | leaf
    """
    enriched = EnrichedGraph(structural_graph=structural_graph)

    # Build outbound call index: symbol_id -> count
    outbound: Dict[str, int] = {}
    for edge in structural_graph.call_edges:
        src = edge["source_symbol_id"]
        outbound[src] = outbound.get(src, 0) + 1

    # Annotate file roles
    for file_id, gf in structural_graph.files.items():
        total_outbound = sum(
            outbound.get(sym["symbol_id"], 0) for sym in gf.symbols
        )
        enriched.file_roles[file_id] = _role_for_file(
            gf.path, gf.is_entry, total_outbound
        )

    # Annotate symbol roles
    for sym_id in structural_graph.symbols:
        enriched.symbol_roles[sym_id] = _role_for_symbol(outbound.get(sym_id, 0))

    logger.info(
        "STAGE_ENRICH: job=%s file_roles=%s",
        structural_graph.job_id,
        {r: sum(1 for v in enriched.file_roles.values() if v == r)
         for r in (FILE_ROLE_ENTRY, FILE_ROLE_SERVICE, FILE_ROLE_MODEL,
                   FILE_ROLE_CONFIG, FILE_ROLE_UTIL)},
    )
    return enriched


def _stage_link(enriched_graph: EnrichedGraph, user_intent: str) -> ContextGraph:
    """
    Stage 4 — Context Linking.

    Links user intent to graph nodes.  ALL links are traceable.
    Builds execution path list from entry points → dependencies.

    RULE: NO floating nodes.
    """
    sg = enriched_graph.structural_graph
    ctx = ContextGraph(enriched_graph=enriched_graph, user_intent=user_intent)

    keywords = _extract_intent_keywords(user_intent)

    # Link keywords to matching files and symbols
    for kw in keywords:
        for file_id, gf in sg.files.items():
            if kw in gf.path.lower():
                ctx.links.append(ContextLink(keyword=kw, file_id=file_id, link_type="file"))

        for sym_id, sym in sg.symbols.items():
            if kw in sym["name"].lower():
                ctx.links.append(
                    ContextLink(keyword=kw, symbol_id=sym_id, link_type="symbol")
                )

    # Build execution paths: entry file → all reachable files via dependencies
    for ep in sg.entry_points:
        entry_file_id = ep["file_id"]
        path: List[str] = []
        visited: set = set()
        queue = [entry_file_id]
        while queue:
            fid = queue.pop(0)
            if fid in visited:
                continue
            visited.add(fid)
            path.append(fid)
            gf = sg.files.get(fid)
            if gf:
                queue.extend(dep for dep in gf.depends_on if dep not in visited)
        if path:
            ctx.execution_paths.append(path)

    logger.info(
        "STAGE_LINK: job=%s keywords=%d links=%d execution_paths=%d",
        sg.job_id, len(keywords), len(ctx.links), len(ctx.execution_paths),
    )
    return ctx


def _stage_gap_detect(context_graph: ContextGraph) -> ContextGaps:
    """
    Stage 5 — Gap Detection.

    Detects missing execution paths, unresolved dependencies, and ambiguous
    mappings.  EVERY gap is logged.  NO silent ignore.
    """
    sg = context_graph.enriched_graph.structural_graph
    gaps = ContextGaps()

    # Gap: no entry points found
    if not sg.entry_points:
        gaps.gaps.append(ContextGap(
            gap_type=GAP_NO_ENTRY_POINTS,
            description=(
                "No entry points detected in the ingested repository.  "
                "Execution paths cannot be reconstructed without an entry point."
            ),
            severity=GAP_SEVERITY_CRITICAL,
        ))

    # Gap: unresolved call edges (callee had no DB-backed target)
    if sg.dropped_calls:
        gaps.gaps.append(ContextGap(
            gap_type=GAP_UNRESOLVED_CALLS,
            description=(
                f"{len(sg.dropped_calls)} symbol call edge(s) could not be resolved "
                f"to a known CodeSymbol and were dropped: "
                + "; ".join(sg.dropped_calls[:5])
                + ("..." if len(sg.dropped_calls) > 5 else "")
            ),
            severity=GAP_SEVERITY_WARNING,
        ))

    # Gap: ambiguous/empty intent
    if not context_graph.user_intent.strip():
        gaps.gaps.append(ContextGap(
            gap_type=GAP_AMBIGUOUS_INTENT,
            description="User intent is empty.  Intent alignment cannot be performed.",
            severity=GAP_SEVERITY_CRITICAL,
        ))

    # Gap: intent provided but no graph links were created
    if (
        context_graph.user_intent.strip()
        and not context_graph.links
        and sg.files
    ):
        gaps.gaps.append(ContextGap(
            gap_type=GAP_MISSING_EXECUTION_PATH,
            description=(
                "User intent was provided but no matching files or symbols were found "
                "in the structural graph.  The execution path cannot be linked to intent."
            ),
            severity=GAP_SEVERITY_WARNING,
        ))

    logger.info(
        "STAGE_GAP_DETECT: job=%s gaps=%d critical=%d warnings=%d",
        sg.job_id,
        len(gaps.gaps),
        len(gaps.critical_gaps),
        len(gaps.warning_gaps),
    )
    if gaps.gaps:
        for g in gaps.gaps:
            logger.warning("GAP[%s][%s]: %s", g.severity.upper(), g.gap_type, g.description)

    return gaps


def _stage_user_align(
    context_graph: ContextGraph,
    gaps: ContextGaps,
    user_intent: str,
    alignment_confirmed: bool,
    alignment_refinement: Optional[str],
) -> AlignedIntentContract:
    """
    Stage 6 — User Alignment (HARD STOP).

    Builds the system summary and presents it for user confirmation.

    If alignment_confirmed is False:
        • Raises AlignmentRequiredError with the summary.
        • Pipeline STOPS — no continuation.

    If alignment_confirmed is True:
        • Returns AlignedIntentContract with valid == True.

    NO bypass is permitted.
    """
    sg = context_graph.enriched_graph.structural_graph

    # Build the alignment summary that MUST be shown to the user
    system_summary: Dict[str, Any] = {
        "system_structure": {
            "total_files": len(sg.files),
            "total_symbols": len(sg.symbols),
            "entry_points": [
                {
                    "file": sg.files[ep["file_id"]].path if ep["file_id"] in sg.files else "?",
                    "type": ep["entry_type"],
                }
                for ep in sg.entry_points
            ],
            "file_roles": {
                role: [
                    sg.files[fid].path
                    for fid, r in context_graph.enriched_graph.file_roles.items()
                    if r == role and fid in sg.files
                ]
                for role in (FILE_ROLE_ENTRY, FILE_ROLE_SERVICE, FILE_ROLE_MODEL,
                             FILE_ROLE_CONFIG, FILE_ROLE_UTIL)
            },
        },
        "intent_mapping": {
            "user_intent": user_intent,
            "matched_files": [
                sg.files[lnk.file_id].path
                for lnk in context_graph.links
                if lnk.link_type == "file" and lnk.file_id in sg.files
            ],
            "matched_symbols": [
                sg.symbols[lnk.symbol_id]["name"]
                for lnk in context_graph.links
                if lnk.link_type == "symbol" and lnk.symbol_id in sg.symbols
            ],
        },
        "missing_components": [
            {"type": g.gap_type, "severity": g.severity, "description": g.description}
            for g in gaps.gaps
        ],
        "execution_paths": [
            [sg.files[fid].path for fid in path if fid in sg.files]
            for path in context_graph.execution_paths
        ],
    }

    contract = AlignedIntentContract(
        job_id=sg.job_id,
        user_intent=user_intent,
        confirmed=alignment_confirmed,
        refinement=alignment_refinement,
        system_summary=system_summary,
    )

    if not alignment_confirmed:
        logger.warning(
            "STAGE_USER_ALIGN: HARD STOP — alignment not confirmed for job=%s", sg.job_id
        )
        raise AlignmentRequiredError(summary=system_summary)

    logger.info(
        "STAGE_USER_ALIGN: alignment confirmed for job=%s intent=%r refinement=%r",
        sg.job_id, user_intent, alignment_refinement,
    )
    return contract


def _stage_finalize(
    context_graph: ContextGraph,
    contract: AlignedIntentContract,
    gaps: ContextGaps,
) -> FinalContext:
    """
    Stage 7 — Context Finalization.

    Produces the FinalContext.

    RULE: NO unresolved CRITICAL gaps.  If critical gaps remain after
    alignment, the pipeline raises RuntimeError.
    """
    if gaps.has_critical_gaps:
        critical = "; ".join(g.description for g in gaps.critical_gaps)
        raise RuntimeError(
            f"FINALIZE_BLOCKED: unresolved critical gaps for job={contract.job_id}: "
            + critical
        )

    final = FinalContext(
        context_graph=context_graph,
        aligned_intent_contract=contract,
        validated_execution_paths=list(context_graph.execution_paths),
    )

    logger.info(
        "STAGE_FINALIZE: job=%s validated_paths=%d",
        contract.job_id, len(final.validated_execution_paths),
    )
    return final


def _stage_activate(
    final_context: FinalContext,
    _pipeline_token: Optional[Any] = None,
) -> ActiveContextSession:
    """
    Stage 8 — Activation.

    MQP-CONTRACT: PHASE3-ACTIVATION-AUTHORITY-LOCK v1.0

    RULES (HARD):
    • _pipeline_token MUST be a _PipelineToken instance generated by
      run_context_pipeline().  Missing or wrong type → ACTIVATION_BLOCKED.
    • AlignedIntentContract.valid MUST be True.  FAIL HARD otherwise.
    • NO fallback.  NO silent pass.

    Output: ActiveContextSession — enables execution simulation,
    path validation, and structural reasoning.
    """
    # GUARD 1: pipeline token required — anti-bypass enforcement
    if not isinstance(_pipeline_token, _PipelineToken):
        raise RuntimeError(
            "ACTIVATION_BLOCKED: _stage_activate requires a valid _PipelineToken.  "
            "Activation is only permitted inside run_context_pipeline().  "
            "Direct external invocation is forbidden."
        )

    # GUARD 2: aligned contract must be valid
    contract = final_context.aligned_intent_contract
    if not contract.valid:
        raise RuntimeError(
            f"ACTIVATION_BLOCKED: AlignedIntentContract.valid is False for job="
            f"{contract.job_id}.  Activation requires explicit user confirmation."
        )

    session_id = str(uuid.uuid4())
    active = ActiveContextSession(
        session_id=session_id,
        job_id=contract.job_id,
        final_context=final_context,
        _token=_pipeline_token,
    )

    logger.info(
        "STAGE_ACTIVATE: ACTIVATED session=%s job=%s",
        session_id, contract.job_id,
    )
    return active


# ---------------------------------------------------------------------------
# SINGLE PIPELINE ENTRY POINT
# ---------------------------------------------------------------------------


def run_context_pipeline(
    job_id: str,
    session: Any,
    *,
    user_intent: str = "",
    alignment_confirmed: bool = False,
    alignment_refinement: Optional[str] = None,
) -> ActiveContextSession:
    """
    Run the full Phase 3 Knowledge Assembly Pipeline.

    MQP-CONTRACT: PHASE3-PIPELINE-EXECUTION-LOCK v1.0

    This is the SINGLE entry point for all Phase 3 execution.
    ALL eight stages execute inside this function in order.
    NO stage logic exists outside this file.

    Parameters
    ----------
    job_id:
        UUID string of the completed Phase 1 IngestJob.
    session:
        An active SQLModel / SQLAlchemy DB session (read access to
        Phase 1 tables; Phase 1 tables are NOT modified).
    user_intent:
        Free-form text describing what the user wants to achieve.
    alignment_confirmed:
        Must be True on the SECOND invocation (after the user has reviewed
        the alignment summary).  False on first call → AlignmentRequiredError
        is raised.
    alignment_refinement:
        Optional clarification provided by the user during alignment.

    Returns
    -------
    ActiveContextSession

    Raises
    ------
    AlignmentRequiredError
        When alignment_confirmed is False.  The exception carries a
        ``summary`` dict that MUST be shown to the user before re-invoking
        with alignment_confirmed=True.
    RuntimeError
        On unresolved critical gaps or if activation is attempted without a
        valid contract.

    Flow
    ----
    Input
     → Stage 1  Normalize        (load Phase 1 rows)
     → Stage 2  Structural Graph (build graph from Phase 1 tables)
     → Stage 3  Semantic Enrich  (annotate file/symbol roles)
     → Stage 4  Context Link     (bind intent to graph)
     → Stage 5  Gap Detect       (log all gaps)
     → Stage 6  USER ALIGN       (HARD STOP — raises if not confirmed)
     → Stage 7  Finalize         (validate, no critical gaps)
     → Stage 8  Activate         (produce ActiveContextSession)
    """
    logger.info(
        "PIPELINE_START: job=%s intent=%r alignment_confirmed=%s",
        job_id, user_intent, alignment_confirmed,
    )

    # Generate the internal pipeline token.
    # This token MUST be passed to _stage_activate and is the only valid key
    # for creating an ActiveContextSession.  It is never exposed outside this
    # function — any caller that tries to invoke _stage_activate directly will
    # not have this token and will be blocked.
    pipeline_token = _PipelineToken()

    # Stage 1 — Input Capture
    _artifact_set = _stage_normalize(job_id, session)  # noqa: F841 (validates loading)

    # Stage 2 — Structural Graph
    structural_graph = _stage_structural_graph(job_id, session)

    # Stage 3 — Semantic Enrichment
    enriched_graph = _stage_enrich(structural_graph)

    # Stage 4 — Context Linking
    context_graph = _stage_link(enriched_graph, user_intent)

    # Stage 5 — Gap Detection
    gaps = _stage_gap_detect(context_graph)

    # Stage 6 — User Alignment (HARD STOP)
    # Raises AlignmentRequiredError if alignment_confirmed is False.
    contract = _stage_user_align(
        context_graph,
        gaps,
        user_intent,
        alignment_confirmed,
        alignment_refinement,
    )

    # Stage 7 — Context Finalization
    final_context = _stage_finalize(context_graph, contract, gaps)

    # Stage 8 — Activation (requires pipeline_token — anti-bypass enforcement)
    active_session = _stage_activate(final_context, pipeline_token)

    logger.info(
        "PIPELINE_COMPLETE: job=%s session=%s",
        job_id, active_session.session_id,
    )
    return active_session
