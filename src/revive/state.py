import json
import sqlite3
import time
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL,
                signature TEXT NOT NULL, pipeline TEXT NOT NULL,
                state TEXT NOT NULL, stable_since REAL NOT NULL, samples INTEGER NOT NULL,
                last_sample REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, source_hash TEXT,
                output_path TEXT, output_hash TEXT, metadata TEXT,
                error_code TEXT, created_at REAL NOT NULL, completed_at REAL, staging_path TEXT,
                UNIQUE(relative_path, signature, pipeline)
            );
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, next_attempt);
            CREATE TABLE IF NOT EXISTS incidents (
                code TEXT PRIMARY KEY, count INTEGER NOT NULL, last_seen REAL NOT NULL,
                message TEXT NOT NULL, last_logged REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    @classmethod
    def readonly(cls, path):
        instance = cls.__new__(cls)
        instance.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        instance.db.row_factory = sqlite3.Row
        return instance

    def setting(self, key):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def close(self):
        self.db.close()

    def incident(self, code, message):
        now = time.time()
        previous = self.db.execute(
            "SELECT last_logged FROM incidents WHERE code=?", (code,)
        ).fetchone()
        should_log = previous is None or now - previous[0] >= 86400
        with self.db:
            self.db.execute(
                """INSERT INTO incidents VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET count=count+1, last_seen=excluded.last_seen,
                message=excluded.message,last_logged=excluded.last_logged""",
                (code, now, message, now if should_log else previous[0]),
            )
        return should_log

    def observe(self, relative, sig, pipeline, now, config, baseline=False):
        row = self.db.execute(
            "SELECT * FROM jobs WHERE relative_path=? AND signature=? AND pipeline=?",
            (relative, sig, pipeline),
        ).fetchone()
        with self.db:
            if row is None:
                completed = self.db.execute(
                    "SELECT 1 FROM jobs WHERE relative_path=? AND signature=? "
                    "AND state='completed' LIMIT 1",
                    (relative, sig),
                ).fetchone()
                if completed:
                    return True  # Model/rule updates never implicitly reprocess finished revisions.
                # Supersede stale revisions before counting capacity to avoid queue deadlock.
                self.db.execute(
                    "UPDATE jobs SET state='skipped',error_code='SOURCE_CHANGED' "
                    "WHERE relative_path=? AND state IN ('waiting_stability','queued','retry_pending')",
                    (relative,),
                )
                count = self.db.execute(
                    "SELECT count(*) FROM jobs WHERE state IN "
                    "('waiting_stability','queued','processing','retry_pending')"
                ).fetchone()[0]
                if count >= config.max_pending_jobs:
                    return False
                self.db.execute(
                    """INSERT INTO jobs(relative_path,signature,pipeline,state,stable_since,
                    samples,last_sample,created_at,error_code) VALUES (?,?,?,?,?,1,?,?,?)""",
                    (
                        relative,
                        sig,
                        pipeline,
                        "skipped" if baseline else "waiting_stability",
                        now,
                        now,
                        now,
                        "EXISTING_DISABLED" if baseline else None,
                    ),
                )
            elif row["state"] == "skipped" and row["error_code"] == "SOURCE_MISSING":
                count = self.db.execute(
                    "SELECT count(*) FROM jobs WHERE state IN "
                    "('waiting_stability','queued','processing','retry_pending')"
                ).fetchone()[0]
                if count >= config.max_pending_jobs:
                    return False
                self.db.execute(
                    "UPDATE jobs SET state='waiting_stability',stable_since=?,last_sample=?,"
                    "samples=1,error_code=NULL WHERE id=?",
                    (now, now, row["id"]),
                )
            elif (
                row["state"] == "waiting_stability"
                and now - row["last_sample"] >= config.sample_seconds
            ):
                samples = row["samples"] + 1
                ready = (
                    samples >= config.stable_samples
                    and now - row["stable_since"] >= config.quiet_seconds
                )
                self.db.execute(
                    "UPDATE jobs SET samples=?,last_sample=?,state=? WHERE id=?",
                    (
                        samples,
                        now,
                        "queued" if ready else "waiting_stability",
                        row["id"],
                    ),
                )
        return True

    def next_job(self):
        return self.db.execute(
            "SELECT * FROM jobs WHERE state='queued' OR "
            "(state='retry_pending' AND next_attempt<=?) ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def update(self, job_id, **fields):
        allowed = {
            "state",
            "attempts",
            "next_attempt",
            "source_hash",
            "output_path",
            "output_hash",
            "metadata",
            "error_code",
            "completed_at",
            "stable_since",
            "last_sample",
            "samples",
            "staging_path",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid state fields")
        with self.db:
            self.db.execute(
                f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                (*fields.values(), job_id),
            )

    def counts(self):
        return dict(self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))

    def export(self):
        for row in self.db.execute("SELECT * FROM jobs ORDER BY id"):
            item = dict(row)
            if item["metadata"]:
                item["metadata"] = json.loads(item["metadata"])
            yield item
