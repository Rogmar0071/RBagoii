#!/usr/bin/env python3
"""
MQP-CONTRACT: SCHEMA_ALIGNMENT_ENFORCEMENT_V1

Explicit schema validation to guarantee that database schema matches
application expectations before system startup.

This script MUST pass before any application code runs.

INVARIANT: Application Schema == Database Schema
IF FALSE → SYSTEM MUST NOT START
"""

import os
import sys
from typing import List, Tuple

import psycopg2


def validate_schema() -> None:
    """
    Validate that all required schema elements exist in the database.

    This function does not return normally. It exits the process with:
    - Exit code 0 if validation passes
    - Exit code 1 if validation fails (DATABASE_URL missing, connection
      failed, or schema misaligned)

    Note: This function calls sys.exit() and does not raise exceptions.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("FATAL: DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    # Required schema validations
    # Format: (table_name, column_name, description)
    required_columns: List[Tuple[str, str, str]] = [
        ("ingest_jobs", "repo_id", "Repository ID for ingestion jobs"),
        ("ingest_jobs", "avg_chunks_per_file", "Ingestion metric: average chunks per file"),
        ("ingest_jobs", "skipped_files_count", "Ingestion metric: skipped files count"),
        ("ingest_jobs", "min_chunks_per_file", "Ingestion metric: min chunks per file"),
        ("ingest_jobs", "max_chunks_per_file", "Ingestion metric: max chunks per file"),
        ("ingest_jobs", "median_chunks_per_file", "Ingestion metric: median chunks per file"),
        ("ingest_jobs", "chunk_variance_flagged", "Ingestion metric: chunk variance flag"),
        ("ingest_jobs", "chunk_variance_delta_pct", "Ingestion metric: chunk variance delta"),
        ("repo_chunks", "file_id", "Canonical source file identity for chunk lineage"),
    ]

    print("SCHEMA ENFORCEMENT: Validating database schema...")

    try:
        # Connect to database (using context manager for proper cleanup)
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                # Validate each required column
                failed_validations = []

                for table_name, column_name, description in required_columns:
                    cursor.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = %s
                          AND column_name = %s
                        """,
                        (table_name, column_name)
                    )

                    result = cursor.fetchone()

                    if result is None:
                        msg = f"  ✗ {table_name}.{column_name} MISSING"
                        failed_validations.append(f"{msg} ({description})")
                        print(
                            f"  ✗ {table_name}.{column_name} - MISSING",
                            file=sys.stderr
                        )
                    else:
                        print(f"  ✓ {table_name}.{column_name} - OK")

        # If any validation failed, abort startup
        if failed_validations:
            print("\nFATAL: Schema misalignment detected", file=sys.stderr)
            print(
                "The following required columns are missing:\n",
                file=sys.stderr
            )
            for failure in failed_validations:
                print(failure, file=sys.stderr)
            print(
                "\nDatabase schema is not aligned with application "
                "expectations.",
                file=sys.stderr
            )
            print(
                "Run 'alembic upgrade head' to apply missing migrations.",
                file=sys.stderr
            )
            sys.exit(1)

        print("SCHEMA VALIDATION PASSED ✓")
        print("All required schema elements are present.")
        sys.exit(0)

    except psycopg2.Error as e:
        print(f"FATAL: Database connection or query failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"FATAL: Unexpected error during schema validation: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    validate_schema()
