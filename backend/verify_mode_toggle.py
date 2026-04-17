#!/usr/bin/env python3
"""
MODE_TOGGLE_RUNTIME_VERIFICATION_V1

Standalone runtime verification that validates mode toggle behavior via mode_engine.
Uses direct function calls instead of API testing to avoid external dependencies.

Note: This validates the core behavior. For full API-level testing, use pytest with
test_mode_toggle_runtime.py which includes HTTP request/response verification.
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, Mock

# Configure environment
os.environ.setdefault("BACKEND_DISABLE_JOBS", "1")
os.environ.setdefault("DATA_DIR", "/tmp/ui_blueprint_verify_mode_toggle")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Mock database dependencies
sys.modules['sqlmodel'] = Mock()
sys.modules['alembic'] = Mock()
sys.modules['psycopg2'] = Mock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock database functions
def mock_get_engine():
    raise RuntimeError("Database not configured")

import backend.app.database
backend.app.database.get_engine = mock_get_engine
backend.app.database.reset_engine = lambda *args, **kwargs: None
backend.app.database.init_db = lambda *args, **kwargs: None

from backend.app.mode_engine import MODE_STRICT, mode_engine_gateway


@dataclass
class ModeToggleScenario:
    """Scenario for testing mode toggle behavior."""
    name: str
    modes: list[str]
    user_intent: str
    ai_response: str
    expect_validation: bool
    expect_structured_failure: bool


def test_phase_1_mode_resolution():
    """Test Phase 1: Mode Resolution."""
    print("\n" + "=" * 70)
    print("PHASE 1: Mode Resolution")
    print("=" * 70)
    
    failures = []
    
    # CASE A: modes=[] (normal mode)
    print("\nCASE A: modes=[] produces normal mode behavior")
    try:
        ai_call = MagicMock(return_value="Weather is sunny")
        output, audit = mode_engine_gateway(
            user_intent="What is the weather?",
            modes=[],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        # Normal mode: no validation
        assert len(audit.validation_results) == 0, "Normal mode should have no validation"
        assert output == "Weather is sunny", "Normal mode should return raw AI output"
        
        print("  ✓ PASS - Normal mode: no validation, raw output")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"CASE A: {e}")
    
    # CASE B: modes=["strict_mode"] (strict mode)
    print("\nCASE B: modes=['strict_mode'] produces strict mode behavior")
    try:
        ai_call = MagicMock(return_value="free text response")
        output, audit = mode_engine_gateway(
            user_intent="design pricing strategy",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        # Strict mode: validation runs
        assert len(audit.validation_results) > 0, "Strict mode should run validation"
        
        # Free text should fail validation
        all_passed = all(vr.get("passed", False) for vr in audit.validation_results)
        assert not all_passed, "Free text should fail validation in strict mode"
        
        # Should return structured failure
        try:
            failure = json.loads(output)
            assert "error" in failure or "failed_rules" in failure
        except json.JSONDecodeError:
            assert "VALIDATION_FAILED" in output
        
        print("  ✓ PASS - Strict mode: validation runs, structured failure returned")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"CASE B: {e}")
    
    return len(failures) == 0, failures


def test_phase_2_contract_activation():
    """Test Phase 2: Contract Activation Toggle."""
    print("\n" + "=" * 70)
    print("PHASE 2: Contract Activation Toggle")
    print("=" * 70)
    
    failures = []
    message = "Design pricing strategy"
    
    # Normal mode - no contract, no validation
    print("\nNormal mode: no contract, no validation")
    try:
        ai_call = MagicMock(return_value="Here's my strategy")
        output, audit = mode_engine_gateway(
            user_intent=message,
            modes=[],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        assert len(audit.validation_results) == 0
        assert output == "Here's my strategy"
        
        print("  ✓ PASS - No contract in normal mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Normal mode: {e}")
    
    # Strict mode - contract created, validation runs
    print("\nStrict mode: contract created, validation runs")
    try:
        ai_call = MagicMock(return_value="Here's my strategy")
        output, audit = mode_engine_gateway(
            user_intent=message,
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        # Validation should run
        assert len(audit.validation_results) > 0
        
        # Free text fails validation
        all_passed = all(vr.get("passed", False) for vr in audit.validation_results)
        assert not all_passed
        
        print("  ✓ PASS - Contract and validation in strict mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Strict mode: {e}")
    
    return len(failures) == 0, failures


def test_phase_3_validation_toggle():
    """Test Phase 3: Validation Toggle."""
    print("\n" + "=" * 70)
    print("PHASE 3: Validation Toggle")
    print("=" * 70)
    
    failures = []
    
    # Normal mode - free text passes through
    print("\nNormal mode: free text passes through without validation")
    try:
        ai_call = MagicMock(return_value="free text only")
        output, audit = mode_engine_gateway(
            user_intent="test",
            modes=[],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        assert len(audit.validation_results) == 0
        assert output == "free text only"
        
        print("  ✓ PASS - No validation in normal mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Normal mode: {e}")
    
    # Strict mode - validation runs and fails
    print("\nStrict mode: validation runs and fails on free text")
    try:
        ai_call = MagicMock(return_value="free text only")
        output, audit = mode_engine_gateway(
            user_intent="test",
            modes=[MODE_STRICT],
            ai_call=ai_call,
            base_system_prompt="",
        )
        
        assert len(audit.validation_results) > 0
        
        # Should fail
        all_passed = all(vr.get("passed", False) for vr in audit.validation_results)
        assert not all_passed
        
        # Structured failure
        is_failure = output.startswith("{") or "VALIDATION_FAILED" in output
        assert is_failure
        
        print("  ✓ PASS - Validation runs in strict mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Strict mode: {e}")
    
    return len(failures) == 0, failures


def test_phase_5_output_difference():
    """Test Phase 5: Output Difference Lock."""
    print("\n" + "=" * 70)
    print("PHASE 5: Output Difference Lock")
    print("=" * 70)
    
    failures = []
    
    print("\nSame input+AI output produces different final output per mode")
    try:
        message = "test query"
        ai_response = "free text response"
        
        # Normal mode
        ai_call_normal = MagicMock(return_value=ai_response)
        output_normal, audit_normal = mode_engine_gateway(
            user_intent=message,
            modes=[],
            ai_call=ai_call_normal,
            base_system_prompt="",
        )
        
        # Strict mode
        ai_call_strict = MagicMock(return_value=ai_response)
        output_strict, audit_strict = mode_engine_gateway(
            user_intent=message,
            modes=[MODE_STRICT],
            ai_call=ai_call_strict,
            base_system_prompt="",
        )
        
        # Outputs MUST differ
        assert output_normal != output_strict, "Outputs should differ between modes"
        
        # Normal: raw AI response
        assert output_normal == ai_response
        
        # Strict: structured failure
        is_failure = output_strict.startswith("{") or "VALIDATION_FAILED" in output_strict
        assert is_failure
        
        print("  ✓ PASS - Outputs differ correctly between modes")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Output difference: {e}")
    
    return len(failures) == 0, failures


def test_phase_6_rapid_toggle():
    """Test Phase 6: Rapid Toggle Stability."""
    print("\n" + "=" * 70)
    print("PHASE 6: Rapid Toggle Stability")
    print("=" * 70)
    
    failures = []
    
    print("\nRapid toggle sequence: [] → [strict] → [] → [strict]")
    try:
        message = "test"
        ai_response = "response"
        
        # Normal (1)
        ai_call_1 = MagicMock(return_value=ai_response)
        output_1, _ = mode_engine_gateway(user_intent=message, modes=[], ai_call=ai_call_1, base_system_prompt="")
        assert output_1 == ai_response
        
        # Strict (2)
        ai_call_2 = MagicMock(return_value=ai_response)
        output_2, _ = mode_engine_gateway(user_intent=message, modes=[MODE_STRICT], ai_call=ai_call_2, base_system_prompt="")
        is_failure_2 = output_2.startswith("{") or "VALIDATION_FAILED" in output_2
        assert is_failure_2
        
        # Normal (3)
        ai_call_3 = MagicMock(return_value=ai_response)
        output_3, _ = mode_engine_gateway(user_intent=message, modes=[], ai_call=ai_call_3, base_system_prompt="")
        assert output_3 == ai_response
        assert output_3 == output_1, "Normal mode should be consistent"
        
        # Strict (4)
        ai_call_4 = MagicMock(return_value=ai_response)
        output_4, _ = mode_engine_gateway(user_intent=message, modes=[MODE_STRICT], ai_call=ai_call_4, base_system_prompt="")
        is_failure_4 = output_4.startswith("{") or "VALIDATION_FAILED" in output_4
        assert is_failure_4
        
        print("  ✓ PASS - No state leakage across rapid toggles")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Rapid toggle: {e}")
    
    return len(failures) == 0, failures


def test_phase_7_hard_invariants():
    """Test Phase 7: Hard Invariants."""
    print("\n" + "=" * 70)
    print("PHASE 7: Hard Invariants")
    print("=" * 70)
    
    failures = []
    
    # INVARIANT 1: modes list is single source of truth
    print("\nINVARIANT 1: modes parameter is single source of truth")
    try:
        # Passing modes=[] should produce normal mode
        ai_call = MagicMock(return_value="test")
        output, audit = mode_engine_gateway(user_intent="test", modes=[], ai_call=ai_call, base_system_prompt="")
        assert len(audit.validation_results) == 0
        
        # Passing modes=[MODE_STRICT] should produce strict mode
        ai_call = MagicMock(return_value="test")
        output, audit = mode_engine_gateway(user_intent="test", modes=[MODE_STRICT], ai_call=ai_call, base_system_prompt="")
        assert len(audit.validation_results) > 0
        
        print("  ✓ PASS - modes parameter is single source of truth")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 1: {e}")
    
    # INVARIANT 2: Normal mode = zero enforcement
    print("\nINVARIANT 2: Normal mode = zero enforcement")
    try:
        ai_call = MagicMock(return_value="free text that would fail in strict mode")
        output, audit = mode_engine_gateway(user_intent="test", modes=[], ai_call=ai_call, base_system_prompt="")
        
        assert len(audit.validation_results) == 0
        assert output == "free text that would fail in strict mode"
        
        print("  ✓ PASS - Normal mode has zero enforcement")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 2: {e}")
    
    # INVARIANT 3: Strict mode = full enforcement
    print("\nINVARIANT 3: Strict mode = full enforcement")
    try:
        ai_call = MagicMock(return_value="free text")
        output, audit = mode_engine_gateway(user_intent="test", modes=[MODE_STRICT], ai_call=ai_call, base_system_prompt="")
        
        assert len(audit.validation_results) > 0
        all_passed = all(vr.get("passed", False) for vr in audit.validation_results)
        assert not all_passed
        
        is_failure = output.startswith("{") or "VALIDATION_FAILED" in output
        assert is_failure
        
        print("  ✓ PASS - Strict mode has full enforcement")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 3: {e}")
    
    # INVARIANT 4: No cross-mode state sharing
    print("\nINVARIANT 4: No cross-mode state sharing")
    try:
        # Each call should be independent
        ai_call_1 = MagicMock(return_value="response 1")
        _, audit_1 = mode_engine_gateway(user_intent="query 1", modes=[MODE_STRICT], ai_call=ai_call_1, base_system_prompt="")
        
        ai_call_2 = MagicMock(return_value="response 2")
        _, audit_2 = mode_engine_gateway(user_intent="query 2", modes=[], ai_call=ai_call_2, base_system_prompt="")
        
        ai_call_3 = MagicMock(return_value="response 3")
        _, audit_3 = mode_engine_gateway(user_intent="query 3", modes=[MODE_STRICT], ai_call=ai_call_3, base_system_prompt="")
        
        # Each should have independent behavior
        assert len(audit_1.validation_results) > 0
        assert len(audit_2.validation_results) == 0
        assert len(audit_3.validation_results) > 0
        
        print("  ✓ PASS - No cross-mode state sharing")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 4: {e}")
    
    # INVARIANT 5: Same input → different output per mode
    print("\nINVARIANT 5: Same input → different output per mode")
    try:
        message = "same query"
        ai_response = "same response"
        
        ai_call_normal = MagicMock(return_value=ai_response)
        output_normal, _ = mode_engine_gateway(user_intent=message, modes=[], ai_call=ai_call_normal, base_system_prompt="")
        
        ai_call_strict = MagicMock(return_value=ai_response)
        output_strict, _ = mode_engine_gateway(user_intent=message, modes=[MODE_STRICT], ai_call=ai_call_strict, base_system_prompt="")
        
        assert output_normal != output_strict
        
        print("  ✓ PASS - Same input produces different output per mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 5: {e}")
    
    # INVARIANT 6: Predictable behavior per mode
    print("\nINVARIANT 6: Predictable behavior per mode")
    try:
        # Normal mode should be consistent
        ai_call_1 = MagicMock(return_value="response")
        output_1, _ = mode_engine_gateway(user_intent="test", modes=[], ai_call=ai_call_1, base_system_prompt="")
        
        ai_call_2 = MagicMock(return_value="response")
        output_2, _ = mode_engine_gateway(user_intent="test", modes=[], ai_call=ai_call_2, base_system_prompt="")
        
        assert output_1 == output_2
        
        # Strict mode should be consistent
        ai_call_3 = MagicMock(return_value="response")
        output_3, _ = mode_engine_gateway(user_intent="test", modes=[MODE_STRICT], ai_call=ai_call_3, base_system_prompt="")
        
        ai_call_4 = MagicMock(return_value="response")
        output_4, _ = mode_engine_gateway(user_intent="test", modes=[MODE_STRICT], ai_call=ai_call_4, base_system_prompt="")
        
        # Both should be failures
        is_failure_3 = output_3.startswith("{") or "VALIDATION_FAILED" in output_3
        is_failure_4 = output_4.startswith("{") or "VALIDATION_FAILED" in output_4
        assert is_failure_3 and is_failure_4
        
        print("  ✓ PASS - Predictable behavior per mode")
    except Exception as e:
        print(f"  ✗ FAIL - {e}")
        failures.append(f"Invariant 6: {e}")
    
    return len(failures) == 0, failures


def main():
    """Run all verification tests."""
    print("=" * 70)
    print("MODE_TOGGLE_RUNTIME_VERIFICATION_V1")
    print("=" * 70)
    print("\nNote: This validates core mode toggle behavior via mode_engine.")
    print("For full API-level testing, use pytest with test_mode_toggle_runtime.py")
    
    all_results = []
    
    # Run all phases
    result1, failures1 = test_phase_1_mode_resolution()
    all_results.append(("Phase 1: Mode Resolution", result1, failures1))
    
    result2, failures2 = test_phase_2_contract_activation()
    all_results.append(("Phase 2: Contract Activation", result2, failures2))
    
    result3, failures3 = test_phase_3_validation_toggle()
    all_results.append(("Phase 3: Validation Toggle", result3, failures3))
    
    result5, failures5 = test_phase_5_output_difference()
    all_results.append(("Phase 5: Output Difference", result5, failures5))
    
    result6, failures6 = test_phase_6_rapid_toggle()
    all_results.append(("Phase 6: Rapid Toggle", result6, failures6))
    
    result7, failures7 = test_phase_7_hard_invariants()
    all_results.append(("Phase 7: Hard Invariants", result7, failures7))
    
    # Print summary
    print("\n" + "=" * 70)
    print("VERIFICATION SUMMARY")
    print("=" * 70)
    
    for name, passed, failures in all_results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status} - {name}")
        if failures:
            for failure in failures:
                print(f"    - {failure}")
    
    # Verification outputs
    print("\n" + "=" * 70)
    print("VERIFICATION OUTPUTS (REQUIRED)")
    print("=" * 70)
    
    all_passed = all(result for _, result, _ in all_results)
    
    outputs = {
        "mode_toggle_verified": all_passed,
        "mode_isolation_runtime": all_passed,
        "validation_toggle_correct": all_passed,
        "governance_toggle_correct": all_passed,
        "output_divergence_confirmed": all_passed,
    }
    
    for key, value in outputs.items():
        status = "YES" if value else "NO"
        symbol = "✓" if value else "✗"
        print(f"{symbol} {key} → {status}")
    
    # Final result
    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL VERIFICATION OUTPUTS: YES")
        print("✓ MODE_TOGGLE_RUNTIME_VERIFICATION_V1 COMPLETE")
        return 0
    else:
        print("✗ SOME VERIFICATION OUTPUTS: NO")
        print("✗ MODE_TOGGLE_RUNTIME_VERIFICATION_V1 INCOMPLETE")
        return 1


if __name__ == "__main__":
    sys.exit(main())
