import hashlib
import json
import sqlite3
from pathlib import Path

from .contracts import Execution


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "executions.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.db.commit()

    def save(self, execution: Execution):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO executions VALUES (?, ?)",
                            (execution.id, execution.model_dump_json()))

    def get(self, execution_id: str) -> Execution | None:
        row = self.db.execute("SELECT document FROM executions WHERE id=?", (execution_id,)).fetchone()
        return Execution.model_validate_json(row[0]) if row else None

    def unfinished(self):
        return [e for (doc,) in self.db.execute("SELECT document FROM executions")
                if (e := Execution.model_validate_json(doc)).status in {"pending", "running"}]

    def list(self, limit=20, offset=0):
        rows = self.db.execute("SELECT document FROM executions ORDER BY json_extract(document, '$.created_at') DESC, id DESC LIMIT ? OFFSET ?", (limit, offset))
        return [Execution.model_validate_json(doc) for (doc,) in rows]

    def artifact(self, execution_id: str, agent_id: str, evidence_id: str, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        relative = Path(execution_id) / agent_id / f"{evidence_id}.json"
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        return relative.as_posix(), hashlib.sha256(raw).hexdigest()

    def close(self):
        self.db.close()
