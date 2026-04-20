#!/usr/bin/env python3
"""
System Health Check - Quick diagnostic tool for RBagoii

This script performs a comprehensive health check of the RBagoii system,
checking all major components and reporting their status.

Usage:
    python scripts/debug/health_check.py
    python scripts/debug/health_check.py --verbose
    python scripts/debug/health_check.py --json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class HealthCheck:
    """System health check runner"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "checks": {},
            "overall_status": "unknown",
        }

    def log(self, message: str, level: str = "INFO") -> None:
        """Log message if verbose mode is enabled"""
        if self.verbose or level == "ERROR":
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {level}: {message}")

    def check_python_environment(self) -> dict[str, Any]:
        """Check Python version and environment"""
        self.log("Checking Python environment...")
        result = {"status": "unknown", "details": {}}

        try:
            python_version = sys.version.split()[0]
            result["details"]["python_version"] = python_version

            # Check if Python 3.11+
            major, minor = sys.version_info.major, sys.version_info.minor
            if major == 3 and minor >= 11:
                result["status"] = "healthy"
                result["details"]["version_ok"] = True
            else:
                result["status"] = "warning"
                result["details"]["version_ok"] = False
                result["details"]["message"] = "Python 3.11+ recommended"

        except Exception as e:
            result["status"] = "error"
            result["details"]["error"] = str(e)

        return result

    def check_backend_imports(self) -> dict[str, Any]:
        """Check if backend can be imported"""
        self.log("Checking backend imports...")
        result = {"status": "unknown", "details": {}}

        try:
            # Try importing critical backend modules
            import_checks = {
                "fastapi": False,
                "sqlmodel": False,
                "pydantic": False,
                "backend.app.main": False,
            }

            for module in import_checks.keys():
                try:
                    __import__(module)
                    import_checks[module] = True
                except ImportError:
                    pass

            result["details"]["imports"] = import_checks
            missing = [k for k, v in import_checks.items() if not v]

            if not missing:
                result["status"] = "healthy"
            elif "backend.app.main" not in missing:
                result["status"] = "warning"
                result["details"]["missing"] = missing
            else:
                result["status"] = "error"
                result["details"]["missing"] = missing
                result["details"]["message"] = (
                    "Critical imports missing. "
                    "Run: pip install -r backend/requirements.txt"
                )

        except Exception as e:
            result["status"] = "error"
            result["details"]["error"] = str(e)

        return result

    def check_database(self) -> dict[str, Any]:
        """Check database connectivity"""
        self.log("Checking database...")
        result = {"status": "unknown", "details": {}}

        try:
            # Check for database file
            db_paths = [
                Path("app.db"),
                Path("backend/app.db"),
                Path("test_verify.db"),
            ]

            found_dbs = [str(p) for p in db_paths if p.exists()]
            result["details"]["database_files"] = found_dbs

            if found_dbs:
                result["status"] = "healthy"
                result["details"]["message"] = (
                    f"Found {len(found_dbs)} database(s)"
                )
            else:
                result["status"] = "warning"
                result["details"]["message"] = (
                    "No database files found (will be created on first run)"
                )

        except Exception as e:
            result["status"] = "error"
            result["details"]["error"] = str(e)

        return result

    def check_redis(self) -> dict[str, Any]:
        """Check Redis connectivity"""
        self.log("Checking Redis...")
        result = {"status": "unknown", "details": {}}

        redis_url = os.getenv("REDIS_URL")
        result["details"]["redis_url_set"] = redis_url is not None

        if not redis_url:
            result["status"] = "info"
            result["details"]["message"] = (
                "REDIS_URL not set (using threading fallback)"
            )
            return result

        try:
            import redis

            r = redis.from_url(redis_url, socket_connect_timeout=2)
            r.ping()
            result["status"] = "healthy"
            result["details"]["message"] = "Redis reachable"
        except ImportError:
            result["status"] = "warning"
            result["details"]["message"] = "redis package not installed"
        except Exception as e:
            result["status"] = "error"
            result["details"]["error"] = str(e)
            result["details"]["message"] = "Redis not reachable"

        return result

    def check_environment_variables(self) -> dict[str, Any]:
        """Check critical environment variables"""
        self.log("Checking environment variables...")
        result = {"status": "unknown", "details": {}}

        env_vars = {
            "API_KEY": os.getenv("API_KEY") is not None,
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY") is not None,
            "REDIS_URL": os.getenv("REDIS_URL") is not None,
            "DATABASE_URL": os.getenv("DATABASE_URL") is not None,
            "MAX_UPLOAD_BYTES": os.getenv("MAX_UPLOAD_BYTES") is not None,
        }

        result["details"]["environment"] = env_vars

        # API_KEY is critical for production
        if env_vars["API_KEY"]:
            result["status"] = "healthy"
        else:
            result["status"] = "warning"
            result["details"]["message"] = (
                "API_KEY not set (development mode)"
            )

        return result

    def check_file_structure(self) -> dict[str, Any]:
        """Check critical files and directories exist"""
        self.log("Checking file structure...")
        result = {"status": "unknown", "details": {}}

        critical_paths = {
            "backend/app/main.py": Path("backend/app/main.py").exists(),
            "backend/requirements.txt": (
                Path("backend/requirements.txt").exists()
            ),
            "pyproject.toml": Path("pyproject.toml").exists(),
            "DEBUGGING_CONTRACT.md": Path("DEBUGGING_CONTRACT.md").exists(),
            "AI_AGENT_CONTEXT.md": Path("AI_AGENT_CONTEXT.md").exists(),
        }

        result["details"]["critical_files"] = critical_paths
        missing = [k for k, v in critical_paths.items() if not v]

        if not missing:
            result["status"] = "healthy"
        else:
            result["status"] = "error"
            result["details"]["missing"] = missing

        return result

    def run_all_checks(self) -> dict[str, Any]:
        """Run all health checks"""
        print("Running RBagoii System Health Check...")
        print("=" * 50)

        checks = {
            "python_environment": self.check_python_environment,
            "backend_imports": self.check_backend_imports,
            "database": self.check_database,
            "redis": self.check_redis,
            "environment_variables": self.check_environment_variables,
            "file_structure": self.check_file_structure,
        }

        for name, check_func in checks.items():
            self.results["checks"][name] = check_func()

        # Determine overall status
        statuses = [c["status"] for c in self.results["checks"].values()]
        if "error" in statuses:
            self.results["overall_status"] = "unhealthy"
        elif "warning" in statuses:
            self.results["overall_status"] = "degraded"
        else:
            self.results["overall_status"] = "healthy"

        return self.results

    def print_results(self) -> None:
        """Print human-readable results"""
        print("\n" + "=" * 50)
        print("HEALTH CHECK RESULTS")
        print("=" * 50)

        status_emoji = {
            "healthy": "✅",
            "warning": "⚠️",
            "error": "❌",
            "info": "ℹ️",
            "unknown": "❓",
        }

        for check_name, check_result in self.results["checks"].items():
            status = check_result["status"]
            emoji = status_emoji.get(status, "?")
            check_display = check_name.replace("_", " ").title()
            print(f"\n{emoji} {check_display}: {status.upper()}")

            if self.verbose or status in ["error", "warning"]:
                for key, value in check_result["details"].items():
                    if key != "error":
                        print(f"   - {key}: {value}")

        overall_status = self.results["overall_status"]
        emoji = status_emoji.get(overall_status, "?")
        print("\n" + "=" * 50)
        print(f"{emoji} OVERALL STATUS: {overall_status.upper()}")
        print("=" * 50)

        if overall_status == "unhealthy":
            print("\n⚠️  CRITICAL ISSUES DETECTED")
            print("Review errors above and fix before proceeding.")
            sys.exit(1)
        elif overall_status == "degraded":
            print("\n⚠️  WARNINGS DETECTED")
            print("System may function with reduced capabilities.")
            sys.exit(0)
        else:
            print("\n✅ ALL SYSTEMS OPERATIONAL")
            sys.exit(0)


def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="RBagoii System Health Check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()

    # Change to repository root if script is run from scripts/debug/
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent.parent
    if repo_root.exists():
        os.chdir(repo_root)

    health_check = HealthCheck(verbose=args.verbose)
    results = health_check.run_all_checks()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        health_check.print_results()


if __name__ == "__main__":
    main()
