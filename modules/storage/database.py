"""SQLite persistence layer for job listings and applications."""
import sqlite3
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


def _job_fingerprint(url: str, title: str, company: str) -> str:
    """Stable dedup key — URL takes priority, title+company as fallback."""
    key = url.strip() if url.strip() else f"{title.lower().strip()}|{company.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class Database:
    def __init__(self, db_path: str = "data/jobs.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT    UNIQUE NOT NULL,
                    source      TEXT    NOT NULL,
                    title       TEXT    NOT NULL,
                    company     TEXT    NOT NULL DEFAULT '',
                    salary_raw  TEXT,
                    salary_min  REAL,
                    salary_max  REAL,
                    location    TEXT,
                    remote      INTEGER DEFAULT 0,
                    url         TEXT,
                    description TEXT,
                    fit_score   INTEGER,
                    fit_reason  TEXT,
                    discovered_at TEXT  NOT NULL,
                    updated_at  TEXT    NOT NULL,
                    status      TEXT    NOT NULL DEFAULT 'nueva'
                );

                CREATE TABLE IF NOT EXISTS responses (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      INTEGER REFERENCES jobs(id),
                    question    TEXT    NOT NULL,
                    answer      TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_jobs_source   ON jobs(source);
                CREATE INDEX IF NOT EXISTS idx_jobs_score    ON jobs(fit_score);
                CREATE INDEX IF NOT EXISTS idx_jobs_disc     ON jobs(discovered_at);
            """)
        logger.debug(f"Database ready at {self.db_path}")

    # ── Job CRUD ──────────────────────────────────────────────────────────────

    def has_job(self, url: str, title: str = "", company: str = "") -> bool:
        """True if a job with this fingerprint already exists."""
        fp = _job_fingerprint(url or "", title or "", company or "")
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint = ?", (fp,)
            ).fetchone() is not None

    def add_job(self, job: dict) -> tuple[bool, int]:
        """Insert job. Returns (is_new, job_id). Skips silently if duplicate."""
        fp = _job_fingerprint(job.get("url", ""), job.get("title", ""), job.get("company", ""))
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM jobs WHERE fingerprint = ?", (fp,)
            ).fetchone()
            if existing:
                return False, existing["id"]

            cur = conn.execute(
                """INSERT INTO jobs
                   (fingerprint, source, title, company, salary_raw, salary_min, salary_max,
                    location, remote, url, description, fit_score, fit_reason,
                    discovered_at, updated_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fp,
                    job.get("source", "unknown"),
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("salary_raw"),
                    job.get("salary_min"),
                    job.get("salary_max"),
                    job.get("location"),
                    int(job.get("remote", False)),
                    job.get("url"),
                    job.get("description"),
                    job.get("fit_score"),
                    job.get("fit_reason"),
                    now,
                    now,
                    "nueva",
                ),
            )
            return True, cur.lastrowid

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"status", "fit_score", "fit_reason", "description"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        placeholders = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE jobs SET {placeholders} WHERE id = ?", values)

    def get_job(self, job_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_jobs(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        min_score: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if source:
            query += " AND source = ?"
            params.append(source)
        if min_score is not None:
            query += " AND fit_score >= ?"
            params.append(min_score)
        query += " ORDER BY discovered_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            by_status = dict(
                conn.execute(
                    "SELECT status, COUNT(*) FROM jobs GROUP BY status"
                ).fetchall()
            )
            by_source = dict(
                conn.execute(
                    "SELECT source, COUNT(*) FROM jobs GROUP BY source"
                ).fetchall()
            )
            avg_score = conn.execute(
                "SELECT AVG(fit_score) FROM jobs WHERE fit_score IS NOT NULL"
            ).fetchone()[0]
            top_jobs = conn.execute(
                "SELECT title, company, fit_score, url FROM jobs "
                "WHERE fit_score IS NOT NULL ORDER BY fit_score DESC LIMIT 5"
            ).fetchall()
        return {
            "total": total,
            "by_status": by_status,
            "by_source": by_source,
            "avg_score": round(avg_score, 1) if avg_score else None,
            "top_jobs": [dict(r) for r in top_jobs],
        }

    # ── Responses ─────────────────────────────────────────────────────────────

    def save_responses(self, job_id: Optional[int], responses: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO responses (job_id, question, answer, created_at) VALUES (?,?,?,?)",
                [(job_id, r["question"], r["answer"], now) for r in responses],
            )

    def get_responses(self, job_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM responses WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]
