from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from backend.app.query_classifier import QueryType, route_query


class QueryState(str, Enum):
    RECEIVED = "RECEIVED"
    CLASSIFIED_STRUCTURAL = "CLASSIFIED_STRUCTURAL"
    CLASSIFIED_SEMANTIC = "CLASSIFIED_SEMANTIC"
    CLASSIFIED_HYBRID = "CLASSIFIED_HYBRID"
    EXECUTED_STRUCTURAL = "EXECUTED_STRUCTURAL"
    EXECUTED_SEMANTIC = "EXECUTED_SEMANTIC"
    FAILED = "FAILED"


@dataclass
class QueryRuntime:
    classification: QueryType
    state: QueryState = QueryState.RECEIVED
    state_history: list[QueryState] = field(default_factory=lambda: [QueryState.RECEIVED])
    execution_path: list[str] = field(default_factory=lambda: ["execute_query"])
    structural_called: bool = False
    retrieval_called: bool = False
    llm_called: bool = False


@dataclass(frozen=True)
class ExecutionTrace:
    classification: str
    execution_path: list[str]
    structural_called: bool
    retrieval_called: bool
    llm_called: bool


class RuntimeViolationError(RuntimeError):
    """Raised when runtime invariants are violated."""


_ALLOWED_TRANSITIONS: dict[QueryState, set[QueryState]] = {
    QueryState.RECEIVED: {
        QueryState.CLASSIFIED_STRUCTURAL,
        QueryState.CLASSIFIED_SEMANTIC,
        QueryState.CLASSIFIED_HYBRID,
        QueryState.FAILED,
    },
    QueryState.CLASSIFIED_STRUCTURAL: {QueryState.EXECUTED_STRUCTURAL, QueryState.FAILED},
    QueryState.CLASSIFIED_SEMANTIC: {QueryState.EXECUTED_SEMANTIC, QueryState.FAILED},
    QueryState.CLASSIFIED_HYBRID: {QueryState.EXECUTED_STRUCTURAL, QueryState.FAILED},
    QueryState.EXECUTED_STRUCTURAL: {QueryState.EXECUTED_SEMANTIC, QueryState.FAILED},
    QueryState.EXECUTED_SEMANTIC: {QueryState.FAILED},
    QueryState.FAILED: set(),
}


def _transition(runtime: QueryRuntime, next_state: QueryState) -> None:
    if next_state not in _ALLOWED_TRANSITIONS[runtime.state]:
        raise RuntimeError("INVALID_QUERY_STATE_TRANSITION")
    runtime.state = next_state
    runtime.state_history.append(next_state)


def _validate_structural_result(result: dict[str, Any]) -> bool:
    files = result.get("files")
    file_count = result.get("file_count")
    source = result.get("source")
    if not isinstance(files, list):
        return False
    if not isinstance(file_count, int):
        return False
    if file_count != len(files):
        return False
    return source == "index_registry"


def call_llm(
    query: str,
    semantic_payload: dict[str, Any],
    llm_handler: Callable[[str, dict[str, Any]], str] | None,
) -> str | None:
    """Single LLM interface for router-controlled query execution."""
    if llm_handler is None:
        return None
    return llm_handler(query, semantic_payload)


def build_execution_trace(
    runtime: QueryRuntime,
    path_prefix: list[str] | None = None,
) -> ExecutionTrace:
    prefix = path_prefix or []
    return ExecutionTrace(
        classification=runtime.classification.value,
        execution_path=[*prefix, *runtime.execution_path],
        structural_called=runtime.structural_called,
        retrieval_called=runtime.retrieval_called,
        llm_called=runtime.llm_called,
    )


def verify_execution_trace(trace: ExecutionTrace) -> None:
    classification = trace.classification
    if classification == QueryType.STRUCTURAL.value:
        if not trace.structural_called:
            raise RuntimeViolationError("INVALID_EXECUTION_PATH")
        if trace.retrieval_called or trace.llm_called:
            raise RuntimeViolationError("LLM_BYPASS_DETECTED")
        return
    if classification == QueryType.SEMANTIC.value and not trace.retrieval_called:
        raise RuntimeViolationError("ROUTER_AUTHORITY_VIOLATION")
    if classification == QueryType.HYBRID.value:
        if not trace.structural_called or not trace.retrieval_called:
            raise RuntimeViolationError("INVALID_EXECUTION_PATH")


def execute_query(
    *,
    classification: QueryType,
    query: str,
    structural_handler: Callable[[str], dict[str, Any]],
    retrieval_handler: Callable[[str], dict[str, Any]],
    llm_handler: Callable[[str, dict[str, Any]], str] | None = None,
) -> tuple[dict[str, Any], QueryRuntime]:
    runtime = QueryRuntime(classification=classification)
    try:
        if classification == QueryType.STRUCTURAL:
            _transition(runtime, QueryState.CLASSIFIED_STRUCTURAL)
            _transition(runtime, QueryState.EXECUTED_STRUCTURAL)
            runtime.structural_called = True
            runtime.execution_path.append("structural_handler")
            structural = structural_handler(query)
            if not _validate_structural_result(structural):
                _transition(runtime, QueryState.FAILED)
                return {"error_code": "STRUCTURAL_FAILURE"}, runtime
            return structural, runtime

        if classification == QueryType.SEMANTIC:
            _transition(runtime, QueryState.CLASSIFIED_SEMANTIC)
            _transition(runtime, QueryState.EXECUTED_SEMANTIC)
            runtime.retrieval_called = True
            runtime.execution_path.append("retrieval_handler")
            semantic = retrieval_handler(query)
            retrieved_chunks = int(semantic.get("retrieved_chunks", 0) or 0)
            if retrieved_chunks <= 0:
                return {"error_code": "INSUFFICIENT_CONTEXT"}, runtime
            if llm_handler is not None:
                runtime.llm_called = True
                runtime.execution_path.append("call_llm")
                llm_output = call_llm(query, semantic, llm_handler)
            else:
                llm_output = None
            return {
                "type": "semantic",
                "retrieved_chunks": retrieved_chunks,
                "source": "retrieval",
                "llm_output": llm_output,
            }, runtime

        _transition(runtime, QueryState.CLASSIFIED_HYBRID)
        _transition(runtime, QueryState.EXECUTED_STRUCTURAL)
        runtime.structural_called = True
        runtime.execution_path.append("structural_handler")
        structural = structural_handler(query)
        if not _validate_structural_result(structural):
            _transition(runtime, QueryState.FAILED)
            return {"error_code": "STRUCTURAL_FAILURE"}, runtime

        _transition(runtime, QueryState.EXECUTED_SEMANTIC)
        runtime.retrieval_called = True
        runtime.execution_path.append("retrieval_handler")
        semantic = retrieval_handler(query)
        retrieved_chunks = int(semantic.get("retrieved_chunks", 0) or 0)
        if retrieved_chunks <= 0:
            semantic_payload: dict[str, Any] = {"error_code": "INSUFFICIENT_CONTEXT"}
        else:
            if llm_handler is not None:
                runtime.llm_called = True
                runtime.execution_path.append("call_llm")
                llm_output = call_llm(query, semantic, llm_handler)
            else:
                llm_output = None
            semantic_payload = {
                "type": "semantic",
                "retrieved_chunks": retrieved_chunks,
                "source": "retrieval",
                "llm_output": llm_output,
            }

        return {"type": "hybrid", "structural": structural, "semantic": semantic_payload}, runtime
    except Exception:
        if (
            runtime.state != QueryState.FAILED
            and QueryState.FAILED in _ALLOWED_TRANSITIONS[runtime.state]
        ):
            runtime.state = QueryState.FAILED
            runtime.state_history.append(QueryState.FAILED)
        return {"error_code": "STRUCTURAL_FAILURE"}, runtime


__all__ = [
    "ExecutionTrace",
    "QueryRuntime",
    "QueryState",
    "RuntimeViolationError",
    "build_execution_trace",
    "execute_query",
    "route_query",
    "verify_execution_trace",
]
