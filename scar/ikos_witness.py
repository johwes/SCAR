"""Extract IKOS counterexample witness traces from output.db.

IKOS stores each check result in a SQLite database alongside the abstract
interval state at the vulnerable statement. Surfacing this gives the LLM
proven execution context — exact checker, status, and call location —
rather than requiring it to re-derive the execution path from source alone.
"""

import sqlite3
from pathlib import Path


def extract(db_path: Path, file_path: str, line: int, radius: int = 3) -> str | None:
    """Return a formatted witness trace for the finding at file_path:line.

    Matches checks within `radius` lines of `line` to absorb minor
    statement-vs-expression offsets in the IKOS IR.

    Returns None on any failure (missing db, schema mismatch, no match) —
    callers should treat the witness as optional enrichment only.
    """
    if not db_path.exists():
        return None
    try:
        return _query(db_path, file_path, line, radius)
    except Exception:
        return None


def _query(db_path: Path, file_path: str, line: int, radius: int) -> str | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    file_id = _find_file_id(con, tables, file_path)
    if file_id is None:
        return None

    rows = _find_checks(con, tables, file_id, line, radius)
    if not rows:
        return None

    out = ["IKOS witness trace:"]
    for r in rows:
        checker = r.get("checker", "?")
        status  = r.get("status",  "?")
        at_line = r.get("line_number", line)
        info    = r.get("info") or ""
        context = r.get("call_context") or ""

        out.append(f"  [{checker}] {status} at line {at_line}")
        if info:
            # info may be raw JSON or a plain string
            try:
                import json as _json
                msg = _json.loads(info).get("message") or info
            except Exception:
                msg = info
            out.append(f"    {msg}")
        if context:
            out.append(f"    call context: {context}")

    return "\n".join(out) if len(out) > 1 else None


def _find_file_id(con: sqlite3.Connection, tables: set, file_path: str) -> int | None:
    for table in ("ar_files", "files"):
        if table not in tables:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "path" not in cols:
            continue
        basename = Path(file_path).name
        rows = con.execute(
            f"SELECT id, path FROM {table} WHERE path = ? OR path LIKE ?",
            (file_path, f"%/{basename}"),
        ).fetchall()
        if not rows:
            continue
        if len(rows) == 1:
            return rows[0][0]
        # Multiple files share the same basename — pick the entry whose path
        # shares the longest common suffix with the target (most specific match).
        target_parts = Path(file_path).parts
        best_id, best_score = rows[0][0], -1
        for row_id, db_path in rows:
            score = sum(
                1 for a, b in zip(reversed(target_parts), reversed(Path(db_path).parts))
                if a == b
            )
            if score > best_score:
                best_score, best_id = score, row_id
        return best_id
    return None


def _find_checks(
    con: sqlite3.Connection, tables: set, file_id: int, line: int, radius: int
) -> list[dict]:
    if "checks" not in tables or "statements" not in tables:
        return []

    stmt_cols = {r[1] for r in con.execute("PRAGMA table_info(statements)")}
    chk_cols  = {r[1] for r in con.execute("PRAGMA table_info(checks)")}

    # Column names vary slightly between IKOS versions
    line_col   = "line_number" if "line_number" in stmt_cols else "line"
    file_col   = "file_id"     if "file_id"     in stmt_cols else "ar_file_id"
    stmt_fk    = "statement_id" if "statement_id" in chk_cols else "stmt_id"
    status_col = "status"      if "status"      in chk_cols else "kind"
    ctx_col    = "call_context" if "call_context" in chk_cols else None

    if line_col not in stmt_cols or file_col not in stmt_cols:
        return []
    if stmt_fk not in chk_cols or "checker" not in chk_cols:
        return []

    select = [
        "ch.checker",
        f"ch.{status_col} AS status",
        f"s.{line_col} AS line_number",
    ]
    if "info" in chk_cols:
        select.append("ch.info")
    if ctx_col and ctx_col in chk_cols:
        select.append(f"ch.{ctx_col} AS call_context")

    try:
        rows = con.execute(
            f"""
            SELECT {', '.join(select)}
            FROM checks ch
            JOIN statements s ON ch.{stmt_fk} = s.id
            WHERE s.{file_col} = ?
              AND s.{line_col} BETWEEN ? AND ?
              AND ch.{status_col} IN ('error', 'warning')
            ORDER BY s.{line_col}
            LIMIT 10
            """,
            (file_id, line - radius, line + radius),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
