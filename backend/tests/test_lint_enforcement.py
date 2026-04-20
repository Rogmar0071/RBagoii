"""
MQP-CONTRACT: AIC-v1.1-LINT-ENFORCEMENT-FIX

Test suite to enforce lint compliance and prevent lint errors from bypassing CI.

This test ensures:
1. Zero lint errors in backend/
2. Zero lint warnings in backend/
3. Proper import ordering (I001)
4. No unused imports (F401)
5. No whitespace violations (W293)

These tests MUST pass before any PR is merged.
"""

import subprocess
from pathlib import Path


class TestLintEnforcement:
    """
    Enforce lint compliance for backend code.

    MQP-CONTRACT: AIC-v1.1-LINT-ENFORCEMENT-FIX
    These tests prevent structural hygiene violations from entering the codebase.
    """

    def test_ruff_check_backend_passes_with_zero_errors(self):
        """
        CRITICAL: Ensure ruff check backend/ returns zero errors.

        This test enforces:
        - Proper import ordering (I001)
        - No unused imports (F401)
        - No whitespace violations (W293)
        - Line length limits (E501)
        - All other ruff rules configured in pyproject.toml

        Failure mode: If this test fails, lint errors exist in backend/
        Action: Run `ruff check backend/` locally to see errors
        Fix: Run `ruff check --fix backend/` to auto-fix most issues
        """
        # Get the repository root (2 levels up from this file)
        repo_root = Path(__file__).parent.parent.parent

        # Run ruff check on backend/ directory
        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "backend/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        # Assert zero exit code (success)
        assert result.returncode == 0, (
            f"Ruff check failed with {result.returncode} errors.\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}\n\n"
            f"To fix:\n"
            f"  1. Run: ruff check backend/\n"
            f"  2. Run: ruff check --fix backend/\n"
            f"  3. Run: ruff check --fix --unsafe-fixes backend/\n"
            f"  4. Manually fix any remaining errors\n"
        )

        # Ensure no errors or warnings in output
        assert "error" not in result.stdout.lower(), (
            f"Lint errors found:\n{result.stdout}"
        )

    def test_no_unused_imports(self):
        """
        Enforce F401: No unused imports.

        This specific test ensures that all imports are actually used.
        Unused imports bloat the codebase and can hide real issues.
        """
        repo_root = Path(__file__).parent.parent.parent

        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "backend/", "--select", "F401"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Unused imports detected (F401):\n{result.stdout}\n\n"
            f"To fix: Run `ruff check --fix backend/`"
        )

    def test_import_sorting(self):
        """
        Enforce I001: Import blocks must be sorted and formatted.

        Imports should be grouped:
        1. Standard library
        2. Third-party packages
        3. Local modules

        Within each group, imports should be sorted alphabetically.
        """
        repo_root = Path(__file__).parent.parent.parent

        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "backend/", "--select", "I001"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Import sorting violations detected (I001):\n{result.stdout}\n\n"
            f"To fix: Run `ruff check --fix backend/`"
        )

    def test_no_whitespace_violations(self):
        """
        Enforce W293: No blank lines with whitespace.

        Blank lines should be completely empty, not contain spaces or tabs.
        """
        repo_root = Path(__file__).parent.parent.parent

        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "backend/", "--select", "W293"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Whitespace violations detected (W293):\n{result.stdout}\n\n"
            f"To fix: Run `ruff check --fix --unsafe-fixes backend/`"
        )

    def test_line_length_limit(self):
        """
        Enforce E501: Line length must not exceed configured limit (100 chars).

        Long lines reduce readability and make code harder to review.
        """
        repo_root = Path(__file__).parent.parent.parent

        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "backend/", "--select", "E501"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Line length violations detected (E501):\n{result.stdout}\n\n"
            f"Lines must be ≤100 characters. Manually break long lines."
        )
