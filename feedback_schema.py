"""Additive migrations for the feedback work queue. Only ADD columns/tables:
an older main.py must keep running against a migrated database."""
from __future__ import annotations

import sqlite3

NEW_COLUMNS = {
    "comments": [
        ("status", "TEXT NOT NULL DEFAULT 'new'"),
        ("recipient", "TEXT NOT NULL DEFAULT 'llm'"),
        ("expected_result", "TEXT"),
        ("verify_selector", "TEXT"),
        ("viewport_w", "INTEGER"),
        ("lane", "TEXT"),
        ("run_id", "TEXT"),
        ("commit_sha", "TEXT"),
        ("after_path", "TEXT"),
        ("verified_at", "TEXT"),
        ("verify_json", "TEXT"),
        ("repeat_of", "TEXT"),
        ("repeat_kind", "TEXT"),
        ("status_note", "TEXT"),
        ("updated_at", "TEXT"),
        ("history_json", "TEXT"),
        ("verb", "TEXT"),
        ("urgent", "INTEGER"),
        ("round", "INTEGER"),
        ("status_json", "TEXT"),
        ("response_json", "TEXT"),
        ("v6_extra_json", "TEXT"),
    ],
    "batches": [
        ("kind", "TEXT NOT NULL DEFAULT 'export'"),
        ("repo_hints_json", "TEXT"),
        ("shipped_at", "TEXT"),
        ("rereview_sent_at", "TEXT"),
        ("project_id", "TEXT"),
        ("export_meta_json", "TEXT"),
    ],
    "projects": [
        ("repo", "TEXT"),
        ("local_path", "TEXT"),
        ("url_prefixes_json", "TEXT"),
        ("preview_cmd", "TEXT"),
        ("preview_url", "TEXT"),
    ],
}

NEW_TABLES = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    cap INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT
);
CREATE TABLE IF NOT EXISTS status_events (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_events_comment ON status_events(comment_id);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_direct ON batches(project_id) WHERE kind = 'direct';
"""


def migrate(c: sqlite3.Connection) -> None:
    for table, cols in NEW_COLUMNS.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, ddl in cols:
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    c.executescript(NEW_TABLES)
