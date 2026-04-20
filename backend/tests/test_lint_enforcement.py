"""
MQP-CONTRACT: AIC-v1.1-LINT-ENFORCEMENT-TEST

Test suite to enforce linting standards and prevent lint errors from bypassing CI.

This test file ensures:
- All Python code passes ruff linting
- No unused imports exist
- Import ordering is correct
- No whitespace violations
- No line length violations

If this test fails, the codebase has lint violations that must be fixed.
"""

import subprocess
from pathlib import Path


class TestLintEnforcement:
    """Enforce lint compliance across the entire backend codebase."""

    def test_ruff_check_backend_passes(self):
        """
        CRITICAL: Backend code must pass ruff linting with zero errors.

        This test prevents lint errors from bypassing CI.
        """
        result = subprocess.run(
            ["ruff", "check", "backend/"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Ruff linting failed with {result.returncode} exit code.\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}\n\n"
            f"Fix by running: ruff check --fix backend/"
        )

    def test_no_unused_imports(self):
        """
        Ensure no F401 (unused import) violations exist.

        Unused imports indicate dead code or incorrect refactoring.
        """
        result = subprocess.run(
            ["ruff", "check", "backend/", "--select", "F401"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Found unused imports (F401 violations):\n\n"
            f"{result.stdout}\n\n"
            f"Fix by running: ruff check --fix backend/"
        )

    def test_import_sorting(self):
        """
        Ensure imports are sorted correctly (I001 violations).

        Import order should be:
        1. Standard library
        2. Third-party packages
        3. Local modules
        """
        result = subprocess.run(
            ["ruff", "check", "backend/", "--select", "I"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Found import sorting violations:\n\n"
            f"{result.stdout}\n\n"
            f"Fix by running: ruff check --fix backend/"
        )

    def test_no_whitespace_violations(self):
        """
        Ensure no W293 (whitespace in blank lines) violations exist.

        Blank lines should contain no whitespace characters.
        """
        result = subprocess.run(
            ["ruff", "check", "backend/", "--select", "W293"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Found whitespace violations (W293):\n\n"
            f"{result.stdout}\n\n"
            f"Fix by running: ruff check --fix --unsafe-fixes backend/"
        )

    def test_line_length_compliance(self):
        """
        Ensure all lines are ≤ 100 characters (E501 violations).

        Long lines reduce code readability.
        """
        result = subprocess.run(
            ["ruff", "check", "backend/", "--select", "E501"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Found line length violations (E501):\n\n"
            f"{result.stdout}\n\n"
            f"Fix manually or with: ruff format backend/"
        )

    def test_all_pyflakes_errors(self):
        """
        Ensure no Pyflakes errors (F) exist.

        Pyflakes detects undefined names, unused variables, and similar issues.
        """
        result = subprocess.run(
            ["ruff", "check", "backend/", "--select", "F"],
            cwd=Path(__file__).parent.parent.parent,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Found Pyflakes violations:\n\n"
            f"{result.stdout}\n\n"
            f"Fix by running: ruff check --fix backend/"
        )
