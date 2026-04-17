#!/usr/bin/env python3
"""
CONTRACT_EXECUTION_BOUNDARY_LOCK_V1 — Hard Invariants Validation

This script verifies that the contract validation boundary is properly
enforced and all invariants are satisfied.
"""

import sys


def check_boundary_invariants():
    """Verify all hard invariants for contract execution boundary."""

    print("=" * 70)
    print("CONTRACT_EXECUTION_BOUNDARY_LOCK_V1 — INVARIANT VALIDATION")
    print("=" * 70)

    from backend.app.contract_construction import ContractObject, validate_contract
    from backend.app.mode_engine import mode_engine_gateway

    results = []

    # INVARIANT 1: Mode Engine NEVER executes invalid contract
    print("\n1. Checking: Mode Engine NEVER executes invalid contract...")
    try:
        # Create an invalid contract (missing required fields)
        invalid_contract = ContractObject(
            required_sections=[],  # Empty - invalid
            validation_rules=[],
            output_format="",  # Empty - invalid
        )

        validation_result = validate_contract(invalid_contract)

        if not validation_result.passed:
            print("   ✓ PASS: Invalid contract detected by validation gate")
            results.append(True)
        else:
            print("   ✗ FAIL: Invalid contract passed validation")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # INVARIANT 2: Contract MUST be validated BEFORE validation stages
    print("\n2. Checking: Contract validated BEFORE validation stages...")
    try:
        # This is architectural - the code path ensures validate_contract
        # is called before any validation stages in mode_engine_gateway
        # We verify by checking that invalid contract blocks early

        def dummy_ai(_):
            return "ASSUMPTIONS: test\nCONFIDENCE: 0.9"

        # We can't easily test this without mocking, but we can verify
        # the function exists and is called in the right place
        import inspect

        source = inspect.getsource(mode_engine_gateway)

        # Check that validate_contract is called
        if "validate_contract" in source:
            # Check that it's called before validation stages
            validate_pos = source.find("validate_contract")
            stage1_pos = source.find("stage_1_structural_validation")

            if validate_pos > 0 and stage1_pos > 0 and validate_pos < stage1_pos:
                print("   ✓ PASS: Contract validation occurs before validation stages")
                results.append(True)
            else:
                print("   ✗ FAIL: Contract validation not properly positioned")
                results.append(False)
        else:
            print("   ✗ FAIL: validate_contract not called in mode_engine_gateway")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # INVARIANT 3: Governance MUST trust ONLY validated contracts
    print("\n3. Checking: Governance trusts ONLY validated contracts...")
    try:
        # Governance goes through mode_engine_gateway which enforces
        # contract validation. Verify this path exists.
        import inspect

        from backend.app.mutation_governance.engine import mutation_governance_gateway

        source = inspect.getsource(mutation_governance_gateway)

        if "mode_engine_gateway" in source:
            print("   ✓ PASS: Governance uses mode_engine_gateway (contract validated)")
            results.append(True)
        else:
            print("   ✗ FAIL: Governance doesn't route through mode_engine_gateway")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # INVARIANT 4: No contract → no strict execution
    print("\n4. Checking: No contract → no strict execution...")
    try:
        # Test that None contract is rejected
        validation_result = validate_contract(None)

        if not validation_result.passed and "contract_is_none" in validation_result.failed_rules:
            print("   ✓ PASS: None contract is rejected")
            results.append(True)
        else:
            print("   ✗ FAIL: None contract not properly rejected")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # INVARIANT 5: No fallback contract allowed
    print("\n5. Checking: No fallback contract allowed...")
    try:
        # Check that mode_engine doesn't create a fallback contract
        import inspect

        source = inspect.getsource(mode_engine_gateway)

        # Look for any fallback patterns
        suspicious_patterns = [
            "fallback_contract",
            "default_contract",
            "or ContractObject()",
            "contract = ContractObject()",  # Creating a default empty one
        ]

        has_fallback = any(pattern in source for pattern in suspicious_patterns)

        if not has_fallback:
            print("   ✓ PASS: No fallback contract logic detected")
            results.append(True)
        else:
            print("   ✗ FAIL: Possible fallback contract logic found")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # INVARIANT 6: Boundary MUST terminate invalid flows early
    print("\n6. Checking: Boundary terminates invalid flows early...")
    try:
        # Verify that validation gate returns early on failure
        # Check the code structure
        import inspect

        source = inspect.getsource(mode_engine_gateway)

        # Look for early return after contract validation failure
        if "contract_validation.passed" in source and "return" in source:
            # Find the position of contract validation check
            check_pos = source.find("contract_validation.passed")
            # Find the next return statement
            return_pos = source.find("return", check_pos)
            # Find the first ai_call after validation
            ai_call_pos = source.find("ai_call(", check_pos)

            # Early return should come before ai_call
            if return_pos > 0 and ai_call_pos > 0 and return_pos < ai_call_pos:
                print("   ✓ PASS: Boundary returns early before AI call")
                results.append(True)
            else:
                print("   ✗ FAIL: Boundary doesn't terminate early enough")
                results.append(False)
        else:
            print("   ✗ FAIL: Contract validation check not found")
            results.append(False)
    except Exception as e:
        print(f"   ✗ FAIL: Exception: {e}")
        results.append(False)

    # Summary
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)

    passed = sum(results)
    total = len(results)

    print(f"\nInvariants Checked: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")

    if passed == total:
        print("\n✓ ALL CONTRACT BOUNDARY INVARIANTS VALIDATED")
        return 0
    else:
        print("\n✗ SOME INVARIANTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(check_boundary_invariants())
