"""Regression guard for the alembic migration chain (DOO-1155).

A clean from-zero ``alembic upgrade head`` on an empty database must complete
without a duplicate-DDL error. The DOO-1155 class of bug — two migrations adding
the same column to the same table (the restic ``last_check_ok`` double-landing
introduced when the DOO-1065/DOO-1119 reconcile linearised both the 5.x AI line
and the 2.6->6.7 feature line into one chain) — never surfaced in the suite
because every other test builds its schema from ``Base.metadata.create_all`` and
never replays the migration chain. These tests close that gap so the failure
cannot silently recur on the next reconcile.

Two layers:

* **Static (always runs, no database).** Replay every revision reachable from
  head in dependency order against an in-memory model of the schema and assert no
  ``CREATE TABLE`` / ``ADD COLUMN`` / ``CREATE INDEX`` ever targets an object that
  already exists (drop/re-create is allowed — it models the real DDL, so a
  legitimate drop-then-re-add does not false-positive). Also asserts exactly one
  head via alembic's own :class:`ScriptDirectory` — NOT a regex head-parse, which
  gives false multi-head on multi-line ``down_revision`` tuples (DOO-1116).

* **Live (opt-in).** When ``MIGRATION_TEST_DATABASE_URL`` points at a *disposable*
  Postgres database, drop ``public`` to guarantee a genuinely empty DB and run the
  real ``alembic upgrade head``, then assert the DB reports the single head. This
  catches ordering/collision bugs a static scan could miss. Skipped otherwise so
  the default suite stays hermetic (no database service required).
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"


def _alembic_config() -> Config:
    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(_alembic_config())


# --- DDL extraction ---------------------------------------------------------
# We only match statements carried by an ``op.``/``batch_op.`` call, so prose in
# docstrings/comments (e.g. a neutralised migration that *mentions* last_check_ok)
# never counts as DDL.

_CREATE_TABLE = re.compile(r"op\.create_table\(\s*['\"]([^'\"]+)['\"]")
_DROP_TABLE = re.compile(r"op\.drop_table\(\s*['\"]([^'\"]+)['\"]")
_ADD_COLUMN = re.compile(
    r"op\.add_column\(\s*['\"]([^'\"]+)['\"]\s*,\s*sa\.Column\(\s*['\"]([^'\"]+)['\"]"
)
_DROP_COLUMN = re.compile(
    r"op\.drop_column\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
)
_BATCH_ALTER = re.compile(r"batch_alter_table\(\s*['\"]([^'\"]+)['\"]")
_BATCH_ADD = re.compile(r"batch_op\.add_column\(\s*sa\.Column\(\s*['\"]([^'\"]+)['\"]")
_BATCH_DROP = re.compile(r"batch_op\.drop_column\(\s*['\"]([^'\"]+)['\"]")
_CREATE_INDEX = re.compile(r"op\.create_index\(\s*(?:op\.f\(\s*)?['\"]([^'\"]+)['\"]")
_DROP_INDEX = re.compile(r"op\.drop_index\(\s*(?:op\.f\(\s*)?['\"]([^'\"]+)['\"]")


def _upgrade_body(source: str) -> str:
    """Return the text of the ``upgrade()`` function only.

    A from-zero install runs ``upgrade()``; the ``downgrade()`` DDL (which mirrors
    it with drops/adds) must not enter the schema model, or a revision's own
    ``downgrade`` drop would mask a genuine duplicate ``upgrade`` add elsewhere.
    """
    m = re.search(r"\ndef upgrade\(", source)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\ndef downgrade\(", source[start:])
    return source[start : start + (nxt.start() if nxt else len(source))]


def _ddl_ops(source: str):
    """Yield ``(kind, key)`` DDL operations from a migration's ``upgrade()``.

    ``kind`` is one of ``create_table``/``drop_table``/``add_column``/
    ``drop_column``/``create_index``/``drop_index``. ``key`` is the object name
    (table, ``table.column``, or index name).
    """
    source = _upgrade_body(source)
    for t in _CREATE_TABLE.findall(source):
        yield "create_table", t
    for t in _DROP_TABLE.findall(source):
        yield "drop_table", t
    for t, c in _ADD_COLUMN.findall(source):
        yield "add_column", f"{t}.{c}"
    for t, c in _DROP_COLUMN.findall(source):
        yield "drop_column", f"{t}.{c}"
    # batch_alter_table('t') ... batch_op.add_column/drop_column blocks.
    for m in _BATCH_ALTER.finditer(source):
        table = m.group(1)
        start = m.end()
        nxt = re.search(r"\n(?:def |\s*with op\.batch_alter_table)", source[start:])
        block = source[start : start + (nxt.start() if nxt else len(source))]
        for c in _BATCH_ADD.findall(block):
            yield "add_column", f"{table}.{c}"
        for c in _BATCH_DROP.findall(block):
            yield "drop_column", f"{table}.{c}"
    for i in _CREATE_INDEX.findall(source):
        yield "create_index", i
    for i in _DROP_INDEX.findall(source):
        yield "drop_index", i


def _ordered_revisions(sd: ScriptDirectory):
    """Revisions reachable from head(s), in apply order (base -> head)."""
    heads = sd.get_heads()
    return list(reversed(list(sd.iterate_revisions(heads, "base"))))


def test_single_head():
    """Exactly one head, computed by alembic itself (not a regex head-parse)."""
    heads = _script_directory().get_heads()
    assert len(heads) == 1, f"expected a single migration head, found {heads}"


def test_no_duplicate_ddl_in_reachable_chain():
    """Replay all reachable revisions; nothing may be created while it exists.

    This is the direct DOO-1155 guard: the moment a second migration re-adds a
    column (or re-creates a table/index) that is already live, the from-zero
    ``alembic upgrade head`` would raise DuplicateColumn/DuplicateTable — so we
    fail here instead, with the exact revision and object named.
    """
    sd = _script_directory()
    tables: set[str] = set()
    columns: set[str] = set()
    indexes: set[str] = set()
    errors: list[str] = []

    for rev in _ordered_revisions(sd):
        source = Path(rev.path).read_text()
        for kind, key in _ddl_ops(source):
            if kind == "create_table":
                if key in tables:
                    errors.append(f"{rev.revision}: re-creates existing table {key!r}")
                tables.add(key)
            elif kind == "drop_table":
                tables.discard(key)
                columns = {c for c in columns if not c.startswith(f"{key}.")}
            elif kind == "add_column":
                if key in columns:
                    errors.append(f"{rev.revision}: re-adds existing column {key!r}")
                columns.add(key)
            elif kind == "drop_column":
                columns.discard(key)
            elif kind == "create_index":
                if key in indexes:
                    errors.append(f"{rev.revision}: re-creates existing index {key!r}")
                indexes.add(key)
            elif kind == "drop_index":
                indexes.discard(key)

    assert not errors, "duplicate DDL in the migration chain:\n" + "\n".join(errors)


def test_no_object_defined_by_multiple_revisions():
    """Belt-and-suspenders: no table/column/index appears as a CREATE/ADD in more
    than one revision at all (even with an intervening drop). A double-landing
    from a reconcile that linearises two lines shows up here first."""
    sd = _script_directory()
    creators: dict[str, list[str]] = defaultdict(list)
    for rev in _ordered_revisions(sd):
        source = Path(rev.path).read_text()
        for kind, key in _ddl_ops(source):
            if kind in ("create_table", "add_column", "create_index"):
                creators[f"{kind}:{key}"].append(rev.revision)
    dups = {k: v for k, v in creators.items() if len(v) > 1}
    assert not dups, "object defined by >1 revision (possible double-landing):\n" + "\n".join(
        f"  {k} <- {v}" for k, v in sorted(dups.items())
    )


@pytest.mark.skipif(
    not os.environ.get("MIGRATION_TEST_DATABASE_URL"),
    reason="set MIGRATION_TEST_DATABASE_URL to a DISPOSABLE Postgres DB to run the "
    "real from-zero upgrade (drops schema public)",
)
def test_from_zero_upgrade_head_live():
    """Real from-zero ``alembic upgrade head`` against a disposable Postgres.

    Guarantees an empty starting point by recreating schema ``public``, then runs
    the full chain and asserts the DB is stamped at the single head. This is the
    end-to-end proof that a clean install migrates cleanly.

    The upgrade runs in a **subprocess** on purpose: alembic's ``env.py`` calls
    ``logging.config.fileConfig`` (which disables existing loggers) and mutates
    process-global state, so running it in-process would leak into unrelated tests
    (e.g. caplog-based assertions elsewhere). A subprocess also mirrors exactly how
    a real install invokes the migration.
    """
    import subprocess
    import sys

    from sqlalchemy import create_engine, text

    url = os.environ["MIGRATION_TEST_DATABASE_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    (expected_head,) = _script_directory().get_heads()

    # env.py derives the URL from settings.database_url (DATABASE_URL); point the
    # child process at the disposable DB. DEBUG=true opts out of the fail-closed
    # placeholder-secret startup check so the migration can run without real
    # JWT/FDM secrets (see conftest).
    child_env = {
        **os.environ,
        "DATABASE_URL": url,
        "DEBUG": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "from-zero `alembic upgrade head` failed:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    engine = create_engine(url)
    with engine.connect() as conn:
        current = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    engine.dispose()
    assert current == expected_head, f"DB at {current!r}, expected head {expected_head!r}"
