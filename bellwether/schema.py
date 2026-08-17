"""Which migrations this code needs, and whether the database has them.

Lived in `api/main.py` and guarded only the API, which is half the problem it
describes. The pipeline deploys on every push while the schema is applied by
hand, so a job can run against a database that predates it — and the symptom
is a traceback naming a column, in a workflow log, minutes after the fact.
Metrics run #2 failed exactly that way on migration 018.

Jobs call :func:`require_current` and fail immediately, naming the migration
and the command that applies it.
"""

from __future__ import annotations

from typing import Any


class SchemaBehind(RuntimeError):
    """The code needs a migration this database has not had applied."""


def _column(schema: str, table: str, column: str) -> str:
    """Does this column exist — regardless of who is asking.

    NOT information_schema.columns, which only lists columns the querying role
    holds a privilege on. The health endpoint runs as bellwether_readonly, which
    has no privileges on anything in `app`, so an information_schema probe there
    returns false on a database where the column exists and the migration
    applied cleanly. That is exactly what it did for 027: the bootstrap reported
    OK for all twenty-eight files and /health reported one of them missing.

    pg_catalog is world-readable, so this answers the question actually being
    asked — whether the migration ran — rather than whether the caller can see
    its result.
    """
    return (
        "EXISTS (SELECT 1 FROM pg_attribute a"
        "          JOIN pg_class c     ON c.oid = a.attrelid"
        "          JOIN pg_namespace n ON n.oid = c.relnamespace"
        f"        WHERE n.nspname = '{schema}' AND c.relname = '{table}'"
        f"          AND a.attname = '{column}' AND NOT a.attisdropped)"
    )


# What each migration is expected to have left behind.
#
# The pipeline deploys on every push; the schema is applied by hand through
# bootstrap_database.py. So code can be ahead of the database, and the symptom
# is a column that does not exist — reported by a failing job, in a log, some
# time later. This makes the question "which migration does this database
# actually have" answerable from outside, in one request.
SCHEMA_EXPECTATIONS = {
    "031_m9_transfer_usage": "SELECT to_regclass('landing.db_transfer') IS NOT NULL AS present",
    "001_schema": (
        "SELECT to_regclass('landing.rc_events') IS NOT NULL"
        "   AND to_regclass('outcome.labels') IS NOT NULL AS present"
    ),
    "002_roles": (
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_writer')"
        "   AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_readonly')"
        "    AS present"
    ),
    "003_m1_frame": (
        "SELECT to_regclass('landing.tag_names') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'landing' AND table_name = 'rc_events'"
        "                  AND column_name = 'tag_ids')"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'landing' AND table_name = 'rc_events'"
        "                  AND column_name = 'in_maturity_cohort') AS present"
    ),
    "004_m1_gaps": ("SELECT to_regclass('landing.gap_attempts') IS NOT NULL AS present"),
    "006_m1_revert_events": ("SELECT to_regclass('outcome.revert_events') IS NOT NULL AS present"),
    "008_m2_evaluations": ("SELECT to_regclass('outcome.evaluations') IS NOT NULL AS present"),
    "014_m3_applied_reverts": (
        "SELECT to_regclass('landing.state_applied_reverts') IS NOT NULL AS present"
    ),
    "015_m3_reproducibility": (
        "SELECT to_regclass('register.reproductions') IS NOT NULL AS present"
    ),
    "030_m8_watchdog_faults": (
        "SELECT to_regclass('landing.watchdog_faults') IS NOT NULL AS present"
    ),
    "029_m7_outcome_blinding": (
        "SELECT "
        + _column("outcome", "label_agreement", "excluded_outcome_visible")
        + " AS present"
    ),
    "028_m7_human_label_training": (
        "SELECT "
        + _column("register", "model_registry", "human_labels")
        + "   AND EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        "                WHERE n.nspname = 'app' AND p.proname = 'labels_for_training') AS present"
    ),
    "027_m7_label_slices": (
        "SELECT "
        + _column("app", "human_labels", "queue_slice")
        + "   AND to_regclass('outcome.label_agreement') IS NOT NULL AS present"
    ),
    "026_m6_freeze_check": (
        "SELECT EXISTS ("
        "    SELECT 1 FROM pg_proc p"
        "    JOIN pg_namespace n ON n.oid = p.pronamespace"
        "    WHERE n.nspname = 'app' AND p.proname = 'is_automation_frozen'"
        ") AS present"
    ),
    "025_m6_session_prune": (
        "SELECT EXISTS ("
        "    SELECT 1 FROM pg_proc p"
        "    JOIN pg_namespace n ON n.oid = p.pronamespace"
        "    WHERE n.nspname = 'landing' AND p.proname = 'prune_expired'"
        "      AND p.prosrc LIKE '%app.sessions%'"
        ") AS present"
    ),
    "024_m6_schema_usage": (
        "SELECT has_schema_privilege('bellwether_readonly', 'app', 'USAGE')"
        "   AND has_table_privilege('bellwether_writer', 'app.sessions', 'DELETE') AS present"
    ),
    "023_m6_auth_lookups": (
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        "                WHERE n.nspname = 'app' AND p.proname = 'credentials_for')"
        "   AND EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        "                WHERE n.nspname = 'app' AND p.proname = 'session_for') AS present"
    ),
    "022_m6_app": (
        "SELECT to_regclass('app.users') IS NOT NULL"
        "   AND to_regclass('app.sessions') IS NOT NULL"
        "   AND to_regclass('app.human_labels') IS NOT NULL"
        "   AND to_regclass('app.audit_log') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bellwether_app') AS present"
    ),
    "021_m5_segment_bands": (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'register'"
        "                  AND table_name = 'model_registry'"
        "                  AND column_name = 'segment_bands') AS present"
    ),
    "020_m5_decisions": (
        "SELECT to_regclass('decide.model_decisions') IS NOT NULL"
        "   AND to_regclass('decide.trigger_evaluations') IS NOT NULL"
        "   AND to_regclass('decide.champion_history') IS NOT NULL AS present"
    ),
    "019_m4_liftwing_comparison": (
        "SELECT to_regclass('outcome.liftwing_attempts') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome'"
        "                  AND table_name = 'prediction_metrics'"
        "                  AND column_name = 'liftwing_margin') AS present"
    ),
    "018_m4_population": (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome'"
        "                  AND table_name = 'prediction_metrics'"
        "                  AND column_name = 'population') AS present"
    ),
    "017_m4_metrics": (
        "SELECT to_regclass('outcome.prediction_metrics') IS NOT NULL"
        "   AND to_regclass('outcome.calibration_bins') IS NOT NULL"
        "   AND to_regclass('outcome.liftwing_scores') IS NOT NULL AS present"
    ),
    "016_m3_reproduction_scope": (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'register' AND table_name = 'reproductions'"
        "                  AND column_name = 'state_predates_window') AS present"
    ),
    "013_m3_model_registry": (
        "SELECT to_regclass('register.model_registry') IS NOT NULL AS present"
    ),
    "012_m3_pipeline_state": (
        "SELECT to_regclass('landing.pipeline_state') IS NOT NULL AS present"
    ),
    "011_m3_register": ("SELECT to_regclass('register.predictions') IS NOT NULL AS present"),
    "010_m2_importance": (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome' AND table_name = 'evaluations'"
        "                  AND column_name = 'feature_importance') AS present"
    ),
    "009_m2_null_check": (
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome' AND table_name = 'evaluations'"
        "                  AND column_name = 'null_pr_auc') AS present"
    ),
    "007_m2_state": (
        "SELECT to_regclass('landing.editor_state') IS NOT NULL"
        "   AND to_regclass('landing.page_state') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome' AND table_name = 'revert_events'"
        "                  AND column_name = 'revert_user_id') AS present"
    ),
    "005_m1_retention": (
        "SELECT to_regclass('outcome.seals') IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM pg_proc p"
        "                 JOIN pg_namespace n ON n.oid = p.pronamespace"
        "                WHERE n.nspname = 'landing' AND p.proname = 'prune_expired')"
        "   AND EXISTS (SELECT 1 FROM information_schema.columns"
        "                WHERE table_schema = 'outcome' AND table_name = 'label_checks'"
        "                  AND column_name = 'in_maturity_cohort') AS present"
    ),
}


def missing(conn: Any) -> list[str]:
    """Migrations this code expects that the database does not have.

    A probe that cannot run counts as missing rather than raising. Some of
    these need a privilege the caller may lack — `to_regclass` requires USAGE
    on the schema it names — and a probe that raises would replace a precise
    answer with a traceback about permissions, which is exactly what this
    module exists to avoid.
    """
    absent = []
    for name, sql in SCHEMA_EXPECTATIONS.items():
        try:
            row = conn.execute(sql).fetchone()
            present = bool(row and row["present"])
        except Exception:  # noqa: BLE001 - unrunnable is indistinguishable from absent here
            conn.rollback()
            present = False
        if not present:
            absent.append(name)
    return sorted(absent)


def require_current(conn: Any) -> None:
    """Fail before doing any work, naming what is missing.

    A job that runs anyway produces a traceback about a column, which says
    nothing about what to do next. This says what to apply.
    """
    absent = missing(conn)
    if absent:
        raise SchemaBehind(
            f"This database is missing {len(absent)} migration(s): {', '.join(absent)}. "
            f"The pipeline deploys on every push; the schema is applied by hand. "
            f"Run: python scripts/bootstrap_database.py <owner-connection-string>"
        )
