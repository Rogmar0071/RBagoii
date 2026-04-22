from __future__ import annotations

from backend.app.query_classifier import QueryType, route_query
from backend.app.query_router import QueryState, execute_query


def test_route_query_trigger_sets() -> None:
    assert route_query("how many files") == QueryType.STRUCTURAL
    assert route_query("list all files") == QueryType.STRUCTURAL
    assert route_query("what does this repo do") == QueryType.SEMANTIC
    assert route_query("how many files and what does this repo do") == QueryType.HYBRID


def test_route_query_misclassification_attempt_stays_structural() -> None:
    assert route_query("CoUnT... FILES??? now") == QueryType.STRUCTURAL


def test_execute_query_structural_uses_index_truth_only() -> None:
    retrieval_called = False
    llm_called = False

    def _structural(_: str) -> dict:
        return {
            "type": "structural",
            "file_count": 200,
            "files": [f"src/file_{i}.py" for i in range(200)],
            "source": "index_registry",
        }

    def _retrieval(_: str) -> dict:
        nonlocal retrieval_called
        retrieval_called = True
        return {"retrieved_chunks": 1}

    def _llm(_: str, __: dict) -> str:
        nonlocal llm_called
        llm_called = True
        return "should never execute"

    result, runtime = execute_query(
        classification=QueryType.STRUCTURAL,
        query="how many files",
        structural_handler=_structural,
        retrieval_handler=_retrieval,
        llm_handler=_llm,
    )

    assert result["type"] == "structural"
    assert result["file_count"] == 200
    assert result["source"] == "index_registry"
    assert runtime.structural_called is True
    assert runtime.retrieval_called is False
    assert runtime.llm_called is False
    assert retrieval_called is False
    assert llm_called is False
    assert runtime.state_history == [
        QueryState.RECEIVED,
        QueryState.CLASSIFIED_STRUCTURAL,
        QueryState.EXECUTED_STRUCTURAL,
    ]


def test_execute_query_semantic_empty_retrieval_blocks_llm() -> None:
    llm_called = False

    def _structural(_: str) -> dict:
        return {
            "type": "structural",
            "file_count": 1,
            "files": ["x.py"],
            "source": "index_registry",
        }

    def _retrieval(_: str) -> dict:
        return {"retrieved_chunks": 0}

    def _llm(_: str, __: dict) -> str:
        nonlocal llm_called
        llm_called = True
        return "never"

    result, runtime = execute_query(
        classification=QueryType.SEMANTIC,
        query="what does this repo do",
        structural_handler=_structural,
        retrieval_handler=_retrieval,
        llm_handler=_llm,
    )

    assert result == {"error_code": "INSUFFICIENT_CONTEXT"}
    assert runtime.retrieval_called is True
    assert runtime.llm_called is False
    assert llm_called is False
    assert runtime.state_history == [
        QueryState.RECEIVED,
        QueryState.CLASSIFIED_SEMANTIC,
        QueryState.EXECUTED_SEMANTIC,
    ]


def test_execute_query_hybrid_runs_structural_before_semantic() -> None:
    order: list[str] = []

    def _structural(_: str) -> dict:
        order.append("structural")
        return {
            "type": "structural",
            "file_count": 2,
            "files": ["a.py", "b.py"],
            "source": "index_registry",
        }

    def _retrieval(_: str) -> dict:
        order.append("retrieval")
        return {"retrieved_chunks": 2}

    result, runtime = execute_query(
        classification=QueryType.HYBRID,
        query="how many files and explain purpose",
        structural_handler=_structural,
        retrieval_handler=_retrieval,
        llm_handler=None,
    )

    assert order == ["structural", "retrieval"]
    assert result["type"] == "hybrid"
    assert result["structural"]["source"] == "index_registry"
    assert result["semantic"]["source"] == "retrieval"
    assert runtime.structural_called is True
    assert runtime.retrieval_called is True
    assert runtime.state_history == [
        QueryState.RECEIVED,
        QueryState.CLASSIFIED_HYBRID,
        QueryState.EXECUTED_STRUCTURAL,
        QueryState.EXECUTED_SEMANTIC,
    ]
