"""SCAR Competition Dashboard.

Central results API and leaderboard for the workshop.

Endpoints:
  POST /submit          — pipeline report task POSTs run results here
  GET  /leaderboard     — JSON leaderboard
  GET  /runs/<team>     — all runs for a team
  GET  /stats           — cluster-wide totals
  GET  /                — HTML leaderboard dashboard (auto-refreshes)
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="SCAR Dashboard")
DB_PATH = os.environ.get("DB_PATH", "/data/dashboard.db")


# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id             TEXT    NOT NULL,
                submitted_at        REAL    NOT NULL,
                accepted_patches    INTEGER NOT NULL,
                unique_cwes         INTEGER NOT NULL,
                tool_diversity      INTEGER NOT NULL,
                score               INTEGER NOT NULL,
                prompt_tokens       INTEGER NOT NULL,
                completion_tokens   INTEGER NOT NULL,
                total_tokens        INTEGER NOT NULL,
                execution_seconds   REAL    NOT NULL,
                findings_json       TEXT
            )
        """)
        db.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def startup() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()


# ── API ───────────────────────────────────────────────────────────────────────

class RunSubmission(BaseModel):
    team_id:            str
    accepted_patches:   int
    unique_cwes:        int
    tool_diversity:     int
    prompt_tokens:      int
    completion_tokens:  int
    total_tokens:       int
    execution_seconds:  float
    findings:           list[dict[str, Any]] = []


@app.post("/submit")
def submit(run: RunSubmission) -> dict:
    score = (
        run.accepted_patches * 3
        + run.unique_cwes    * 2
        + run.tool_diversity * 1
    )
    with get_db() as db:
        db.execute(
            """INSERT INTO runs
               (team_id, submitted_at, accepted_patches, unique_cwes,
                tool_diversity, score, prompt_tokens, completion_tokens,
                total_tokens, execution_seconds, findings_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run.team_id, time.time(),
                run.accepted_patches, run.unique_cwes, run.tool_diversity, score,
                run.prompt_tokens, run.completion_tokens, run.total_tokens,
                run.execution_seconds, json.dumps(run.findings),
            ),
        )
        db.commit()
    return {"score": score, "team_id": run.team_id}


@app.get("/leaderboard")
def leaderboard() -> list[dict]:
    with get_db() as db:
        rows = db.execute("""
            SELECT
                team_id,
                COUNT(*)                    AS run_count,
                MAX(score)                  AS best_score,
                MAX(accepted_patches)       AS best_patches,
                MAX(unique_cwes)            AS best_cwes,
                MAX(tool_diversity)         AS best_diversity,
                SUM(prompt_tokens)          AS total_prompt_tokens,
                SUM(completion_tokens)      AS total_completion_tokens,
                SUM(total_tokens)           AS total_tokens,
                MIN(execution_seconds)      AS best_time_seconds
            FROM runs
            GROUP BY team_id
            ORDER BY best_score DESC, total_tokens ASC, best_time_seconds ASC
        """).fetchall()
        return [dict(r) for r in rows]


@app.get("/runs/{team_id}")
def team_runs(team_id: str) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            """SELECT id, submitted_at, accepted_patches, unique_cwes,
                      tool_diversity, score, prompt_tokens, completion_tokens,
                      total_tokens, execution_seconds
               FROM runs WHERE team_id = ?
               ORDER BY submitted_at DESC""",
            (team_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Team not found")
    return [dict(r) for r in rows]


@app.get("/stats")
def cluster_stats() -> dict:
    with get_db() as db:
        row = db.execute("""
            SELECT
                COUNT(*)                    AS total_runs,
                COUNT(DISTINCT team_id)     AS active_teams,
                SUM(prompt_tokens)          AS cluster_prompt_tokens,
                SUM(completion_tokens)      AS cluster_completion_tokens,
                SUM(total_tokens)           AS cluster_total_tokens,
                SUM(accepted_patches)       AS cluster_patches
            FROM runs
        """).fetchone()
    return dict(row)


# ── HTML Dashboard ────────────────────────────────────────────────────────────

def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_time(secs: float | None) -> str:
    if secs is None:
        return "—"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S UTC")


def build_dashboard() -> str:
    with get_db() as db:
        board = db.execute("""
            SELECT
                team_id,
                COUNT(*)                    AS run_count,
                MAX(score)                  AS best_score,
                MAX(accepted_patches)       AS best_patches,
                MAX(unique_cwes)            AS best_cwes,
                MAX(tool_diversity)         AS best_diversity,
                SUM(prompt_tokens)          AS total_prompt_tokens,
                SUM(completion_tokens)      AS total_completion_tokens,
                SUM(total_tokens)           AS total_tokens,
                MIN(execution_seconds)      AS best_time_seconds,
                MAX(submitted_at)           AS last_run_at
            FROM runs
            GROUP BY team_id
            ORDER BY best_score DESC, total_tokens ASC, best_time_seconds ASC
        """).fetchall()

        stats = db.execute("""
            SELECT
                COUNT(*)                    AS total_runs,
                COUNT(DISTINCT team_id)     AS active_teams,
                SUM(prompt_tokens)          AS cluster_prompt_tokens,
                SUM(completion_tokens)      AS cluster_completion_tokens,
                SUM(total_tokens)           AS cluster_total_tokens,
                SUM(accepted_patches)       AS cluster_patches
            FROM runs
        """).fetchone()

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Leaderboard rows
    rows_html = ""
    prev_score = None
    rank = 0
    for i, r in enumerate(board):
        if r["best_score"] != prev_score:
            rank = i + 1
            prev_score = r["best_score"]

        rank_label = {1: "1st", 2: "2nd", 3: "3rd"}.get(rank, f"{rank}th")
        row_class = "rank-first" if rank == 1 else ("rank-second" if rank == 2 else ("rank-third" if rank == 3 else ""))
        tie_note = " (tie)" if i > 0 and r["best_score"] == dict(board[i-1])["best_score"] else ""

        rows_html += f"""
        <tr class="{row_class}">
          <td class="center">{rank_label}{tie_note}</td>
          <td><strong>{r['team_id']}</strong></td>
          <td class="center score">{r['best_score']}</td>
          <td class="center">{r['best_patches']}</td>
          <td class="center">{r['best_cwes']}</td>
          <td class="center">{r['best_diversity']}</td>
          <td class="center">{r['run_count']}</td>
          <td class="center">{_fmt_tokens(r['total_prompt_tokens'])}</td>
          <td class="center">{_fmt_tokens(r['total_completion_tokens'])}</td>
          <td class="center"><strong>{_fmt_tokens(r['total_tokens'])}</strong></td>
          <td class="center">{_fmt_time(r['best_time_seconds'])}</td>
          <td class="center muted">{_fmt_ts(r['last_run_at'])}</td>
        </tr>"""

    if not rows_html:
        rows_html = '<tr><td colspan="12" class="center muted">No submissions yet</td></tr>'

    s = stats or {}
    cluster_prompt     = _fmt_tokens(s.get("cluster_prompt_tokens") or 0)
    cluster_completion = _fmt_tokens(s.get("cluster_completion_tokens") or 0)
    cluster_total      = _fmt_tokens(s.get("cluster_total_tokens") or 0)
    cluster_patches    = s.get("cluster_patches") or 0
    total_runs         = s.get("total_runs") or 0
    active_teams       = s.get("active_teams") or 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="30">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SCAR Leaderboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }}
    header {{ background: #1a1d2e; border-bottom: 1px solid #2d3148; padding: 1.5rem 2rem; }}
    header h1 {{ font-size: 1.6rem; font-weight: 700; letter-spacing: 0.05em; color: #a78bfa; }}
    header p {{ font-size: 0.85rem; color: #64748b; margin-top: 0.25rem; }}
    .stats-bar {{ display: flex; gap: 1rem; padding: 1rem 2rem; background: #141624; border-bottom: 1px solid #2d3148; flex-wrap: wrap; }}
    .stat {{ background: #1e2235; border: 1px solid #2d3148; border-radius: 8px; padding: 0.75rem 1.25rem; min-width: 140px; }}
    .stat .label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.1em; color: #64748b; }}
    .stat .value {{ font-size: 1.4rem; font-weight: 700; color: #e2e8f0; margin-top: 0.2rem; }}
    .stat .sub {{ font-size: 0.75rem; color: #64748b; margin-top: 0.1rem; }}
    main {{ padding: 1.5rem 2rem; }}
    h2 {{ font-size: 1rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 1rem; }}
    .table-wrap {{ overflow-x: auto; border-radius: 10px; border: 1px solid #2d3148; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    thead th {{ background: #1a1d2e; padding: 0.75rem 1rem; text-align: left; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: #64748b; white-space: nowrap; border-bottom: 1px solid #2d3148; }}
    tbody tr {{ border-bottom: 1px solid #1e2235; transition: background 0.15s; }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: #1e2235; }}
    td {{ padding: 0.85rem 1rem; white-space: nowrap; }}
    .center {{ text-align: center; }}
    .score {{ font-size: 1.1rem; font-weight: 700; color: #a78bfa; }}
    .muted {{ color: #475569; font-size: 0.8rem; }}
    .rank-first  {{ background: rgba(234, 179,  8, 0.08); }}
    .rank-second {{ background: rgba(148, 163, 184, 0.06); }}
    .rank-third  {{ background: rgba(180, 120,  60, 0.06); }}
    .rank-first td.score  {{ color: #eab308; }}
    .rank-second td.score {{ color: #94a3b8; }}
    .rank-third td.score  {{ color: #b47c3c; }}
    .tiebreaker-note {{ font-size: 0.75rem; color: #475569; margin-top: 1rem; }}
    footer {{ padding: 1rem 2rem; font-size: 0.75rem; color: #334155; border-top: 1px solid #1e2235; margin-top: 2rem; }}
  </style>
</head>
<body>
  <header>
    <h1>SCAR — Competition Leaderboard</h1>
    <p>Auto-refreshes every 30 seconds &nbsp;|&nbsp; Last updated: {now}</p>
  </header>

  <div class="stats-bar">
    <div class="stat">
      <div class="label">Active Teams</div>
      <div class="value">{active_teams}</div>
    </div>
    <div class="stat">
      <div class="label">Total Runs</div>
      <div class="value">{total_runs}</div>
    </div>
    <div class="stat">
      <div class="label">Cluster Patches</div>
      <div class="value">{cluster_patches}</div>
    </div>
    <div class="stat">
      <div class="label">Cluster Tokens</div>
      <div class="value">{cluster_total}</div>
      <div class="sub">{cluster_prompt} prompt &nbsp;/&nbsp; {cluster_completion} completion</div>
    </div>
  </div>

  <main>
    <h2>Leaderboard</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Rank</th>
            <th>Team (namespace)</th>
            <th>Score</th>
            <th>Patches</th>
            <th>CWEs</th>
            <th>Tools</th>
            <th>Runs</th>
            <th>Prompt Tokens</th>
            <th>Completion Tokens</th>
            <th>Total Tokens</th>
            <th>Best Time</th>
            <th>Last Run</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
    <p class="tiebreaker-note">
      Score = accepted patches x3 + unique CWEs x2 + tool diversity x1.
      Tiebreaker: fewest total tokens across all runs, then fastest best execution time.
    </p>
  </main>

  <footer>SCAR &mdash; Static C Analysis &amp; Repair &nbsp;|&nbsp; Chalmers Workshop</footer>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return build_dashboard()
