from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

import numpy as np

from memory.models import MemoryItem


MEMORY_ITEM_COLUMNS: dict[str, str] = {
    "id": "TEXT PRIMARY KEY",
    "type": "TEXT NOT NULL",
    "title": "TEXT",
    "summary": "TEXT NOT NULL",
    "body": "TEXT",
    "source": "TEXT",
    "source_ref": "TEXT",
    "happened_at": "TEXT",
    "importance": "REAL DEFAULT 0.5",
    "confidence": "REAL DEFAULT 0.7",
    "reinforcement": "INTEGER DEFAULT 0",
    "emotional_weight": "REAL DEFAULT 0",
    "status": "TEXT DEFAULT 'active'",
    "content_hash": "TEXT",
    "embedding_json": "TEXT",
    "extra_json": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
    "last_seen_at": "TEXT",
}


class SQLiteMemory2Store:
    def __init__(self, db_path: str | Path, embedding_dimension: int | None = None) -> None:
        self.db_path = Path(db_path)
        self.configured_embedding_dimension = (
            int(embedding_dimension) if embedding_dimension else None
        )
        self.sqlite_vec_available = _sqlite_vec_available()
        self._sqlite_vec_module: Any | None = None
        self._vec_enabled = False
        self._vector_mode = "disabled"
        self._fallback_reason = ""
        self._last_vector_error = ""

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT,
                    summary TEXT NOT NULL,
                    body TEXT,
                    source TEXT,
                    source_ref TEXT,
                    happened_at TEXT,
                    importance REAL DEFAULT 0.5,
                    confidence REAL DEFAULT 0.7,
                    reinforcement INTEGER DEFAULT 0,
                    emotional_weight REAL DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    content_hash TEXT,
                    embedding_json TEXT,
                    extra_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    last_seen_at TEXT
                )
                """
            )
            self._ensure_memory_item_columns(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_replacements (
                    old_id TEXT,
                    new_id TEXT,
                    reason TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS consolidation_events (
                    source_ref TEXT PRIMARY KEY,
                    happened_at TEXT,
                    metadata_json TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_access_events (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT,
                    query TEXT,
                    score REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_items(type)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_source_ref ON memory_items(source_ref)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_happened_at ON memory_items(happened_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_content_hash ON memory_items(content_hash)"
            )
            self._initialize_sqlite_vec(conn)

    async def upsert_item(
        self,
        item: MemoryItem,
        embedding: list[float] | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        created_at = _format_datetime(item.created_at or now)
        updated_at = _format_datetime(item.updated_at or now)
        extra = dict(item.metadata)
        extra.setdefault("tags", list(item.tags))
        if item.supersedes:
            extra.setdefault("supersedes", list(item.supersedes))
        body = str(extra.get("body") or item.text)
        summary = item.text
        payload = {
            "id": item.id,
            "type": item.type,
            "title": str(extra.get("title") or "") or None,
            "summary": summary,
            "body": body,
            "source": item.source,
            "source_ref": item.source_ref,
            "happened_at": _format_datetime(item.happened_at),
            "importance": float(item.importance),
            "confidence": float(item.confidence),
            "reinforcement": _coerce_int(extra.get("reinforcement"), default=0),
            "emotional_weight": float(item.emotional_weight),
            "status": item.status,
            "content_hash": _content_hash(summary, body),
            "embedding_json": _json_dumps(embedding) if embedding is not None else None,
            "extra_json": _json_dumps(extra),
            "created_at": created_at,
            "updated_at": updated_at,
            "last_seen_at": _format_datetime(item.last_used_at),
        }
        columns = list(MEMORY_ITEM_COLUMNS)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column != "id"
        )
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO memory_items ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET {updates}
                """,
                [payload[column] for column in columns],
            )
            if item.status == "active" and embedding is not None:
                self._try_upsert_vector(conn, item.id, embedding)
            elif item.status == "active" and embedding is None:
                self._try_delete_vector(conn, item.id)
            elif item.status != "active":
                self._try_delete_vector(conn, item.id)
        return item.id

    async def get_item(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_items WHERE id = ?",
                (memory_id,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    async def find_by_content_hash(self, content_hash: str) -> dict[str, Any] | None:
        clean_hash = str(content_hash or "").strip()
        if not clean_hash:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE content_hash = ? AND status = 'active'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (clean_hash,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    async def find_active_by_type(
        self,
        kind: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clean_kind = str(kind or "").strip()
        if not clean_kind:
            return []
        return await self.list_items(status="active", kind=clean_kind, limit=limit)

    async def list_items(
        self,
        status: str = "active",
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = _status_kind_filter(status=status, kinds=[kind] if kind else None)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_items
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, int(limit), int(offset)],
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    async def update_item(self, memory_id: str, fields: dict[str, Any]) -> bool:
        allowed = set(MEMORY_ITEM_COLUMNS) - {"id"}
        prepared_fields = dict(fields)
        if "extra_json" in prepared_fields:
            existing = await self.get_item(memory_id)
            existing_extra = existing.get("extra", {}) if existing else {}
            incoming_extra = _json_loads(prepared_fields["extra_json"], default={})
            if not isinstance(existing_extra, dict):
                existing_extra = {}
            if not isinstance(incoming_extra, dict):
                incoming_extra = {}
            prepared_fields["extra_json"] = {**existing_extra, **incoming_extra}
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in prepared_fields.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(_encode_field_value(key, value))
        if not assignments:
            return False
        if "updated_at" not in prepared_fields:
            assignments.append("updated_at = ?")
            values.append(_format_datetime(datetime.now(timezone.utc)))
        values.append(memory_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE memory_items SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount > 0:
                status = str(prepared_fields.get("status") or "")
                if status and status != "active":
                    self._try_delete_vector(conn, memory_id)
                elif "embedding_json" in prepared_fields:
                    embedding = _as_vector(prepared_fields.get("embedding_json"))
                    if embedding is not None:
                        self._try_upsert_vector(conn, memory_id, embedding.tolist())
        return cursor.rowcount > 0

    async def delete_item(self, memory_id: str, soft: bool = True) -> bool:
        with self._connect() as conn:
            if soft:
                cursor = conn.execute(
                    """
                    UPDATE memory_items
                    SET status = 'deleted', updated_at = ?
                    WHERE id = ?
                    """,
                    (_format_datetime(datetime.now(timezone.utc)), memory_id),
                )
            else:
                cursor = conn.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
            if cursor.rowcount > 0:
                self._try_delete_vector(conn, memory_id)
        return cursor.rowcount > 0

    async def batch_delete(self, ids: list[str], soft: bool = True) -> dict[str, list[str]]:
        deleted: list[str] = []
        missing: list[str] = []
        for memory_id in _dedupe_ids(ids):
            if await self.delete_item(memory_id, soft=soft):
                deleted.append(memory_id)
            else:
                missing.append(memory_id)
        return {"deleted": deleted, "missing": missing}

    async def keyword_search(
        self,
        query: str,
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        where, params = _status_kind_filter(status="active", kinds=kinds)
        if query:
            like = f"%{query}%"
            where = f"{where} AND (summary LIKE ? OR body LIKE ? OR type LIKE ? OR source_ref LIKE ?)"
            params.extend([like, like, like, like])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM memory_items
                {where}
                ORDER BY reinforcement DESC, importance DESC, updated_at DESC
                LIMIT ?
                """,
                [*params, int(limit) * 3],
            ).fetchall()
        results = [_row_to_dict(row) for row in rows]
        for result in results:
            result["score"] = _keyword_score(query, result)
        return sorted(results, key=lambda item: float(item["score"]), reverse=True)[:limit]

    async def vector_search(
        self,
        embedding: list[float],
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        query_vector = _as_vector(embedding)
        if query_vector is None:
            return []
        if self._can_use_sqlite_vec(query_vector):
            try:
                return self._sqlite_vec_search(query_vector, kinds=kinds, limit=limit)
            except Exception as exc:
                self._disable_sqlite_vec(f"sqlite-vec query failed: {exc}")
                self._last_vector_error = str(exc)
                self._vector_mode = "numpy_fallback"
        return self._numpy_vector_search(query_vector, kinds=kinds, limit=limit)

    def _numpy_vector_search(
        self,
        query_vector: np.ndarray,
        *,
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        where, params = _status_kind_filter(status="active", kinds=kinds)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_items {where}",
                params,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = _row_to_dict(row)
            item_vector = _as_vector(item.get("embedding"))
            if item_vector is None or item_vector.shape != query_vector.shape:
                continue
            score = _cosine_similarity(query_vector, item_vector)
            item["score"] = score
            results.append(item)
        return sorted(results, key=lambda item: float(item["score"]), reverse=True)[:limit]

    async def find_similar(
        self,
        embedding: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self.vector_search(embedding, limit=limit)

    async def reinforce_item(self, memory_id: str, amount: int = 1) -> bool:
        now = _format_datetime(datetime.now(timezone.utc))
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE memory_items
                SET reinforcement = COALESCE(reinforcement, 0) + ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (int(amount), now, now, memory_id),
            )
        return cursor.rowcount > 0

    async def record_replacement(self, old_id: str, new_id: str, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_replacements (old_id, new_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    old_id,
                    new_id,
                    reason,
                    _format_datetime(datetime.now(timezone.utc)),
                ),
            )

    async def list_replacements(self, memory_id: str) -> list[dict[str, Any]]:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT old_id, new_id, reason, created_at
                FROM memory_replacements
                WHERE old_id = ? OR new_id = ?
                ORDER BY created_at DESC
                """,
                (clean_id, clean_id),
            ).fetchall()
        return [dict(row) for row in rows]

    async def record_access(self, memory_id: str, query: str, score: float) -> None:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_access_events (id, memory_id, query, score, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    clean_id,
                    str(query or ""),
                    float(score),
                    _format_datetime(datetime.now(timezone.utc)),
                ),
            )

    async def describe(self) -> dict[str, Any]:
        initialized = self.db_path.exists()
        active_count = 0
        total_count = 0
        embedding_dimension: int | None = self.configured_embedding_dimension
        if initialized:
            try:
                with self._connect() as conn:
                    total_count = int(
                        conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
                    )
                    active_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM memory_items WHERE status = 'active'"
                        ).fetchone()[0]
                    )
                    stored_dimension = self._get_setting(conn, "embedding_dimension")
                    if stored_dimension:
                        embedding_dimension = int(stored_dimension)
            except sqlite3.Error:
                initialized = False
        return {
            "enabled": initialized,
            "backend": "sqlite-memory2",
            "path": str(self.db_path),
            "sqlite_vec_available": self.sqlite_vec_available,
            "sqlite_vec_enabled": self._vec_enabled,
            "vector_mode": self._vector_mode if initialized else "disabled",
            "disabled_reason": "" if initialized else "memory2 db is not initialized",
            "fallback_reason": self._fallback_reason,
            "last_vector_error": self._last_vector_error,
            "embedding_dimension": embedding_dimension,
            "vector_table": "memory_vec" if self._vec_enabled else "",
            "fallback_mode": "numpy-cosine",
            "items": {"total": total_count, "active": active_count},
        }

    async def rebuild_vector_index(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "ok": False,
                "rebuilt": 0,
                "skipped": 0,
                "errors": ["memory2 db is not initialized"],
            }
        if not self.sqlite_vec_available and self._sqlite_vec_module is None:
            skipped = 0
            try:
                with self._connect() as conn:
                    skipped = int(
                        conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM memory_items
                            WHERE status = 'active' AND embedding_json IS NOT NULL
                            """
                        ).fetchone()[0]
                    )
            except sqlite3.Error:
                skipped = 0
            return {
                "ok": False,
                "rebuilt": 0,
                "skipped": skipped,
                "errors": [self._fallback_reason or "sqlite-vec unavailable"],
            }
        rebuilt = 0
        skipped = 0
        errors: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, embedding_json
                FROM memory_items
                WHERE status = 'active' AND embedding_json IS NOT NULL
                ORDER BY updated_at DESC, created_at DESC
                """
            ).fetchall()
            self._try_clear_vector_table(conn)
            for row in rows:
                memory_id = str(row["id"])
                vector = _as_vector(row["embedding_json"])
                if vector is None:
                    skipped += 1
                    errors.append(f"{memory_id}: invalid embedding_json")
                    continue
                try:
                    self._ensure_sqlite_vec_ready(conn, int(vector.size))
                    self._upsert_vector_row(conn, memory_id, vector)
                    rebuilt += 1
                except Exception as exc:
                    skipped += 1
                    self._disable_sqlite_vec(f"sqlite-vec rebuild failed: {exc}")
                    self._last_vector_error = str(exc)
                    errors.append(f"{memory_id}: {exc}")
                    break
        return {
            "ok": rebuilt > 0 and not errors,
            "rebuilt": rebuilt,
            "skipped": skipped,
            "errors": errors,
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_memory_item_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(memory_items)").fetchall()
        }
        for column, definition in MEMORY_ITEM_COLUMNS.items():
            if column not in existing:
                relaxed = definition.replace(" PRIMARY KEY", "").replace(" NOT NULL", "")
                conn.execute(f"ALTER TABLE memory_items ADD COLUMN {column} {relaxed}")

    def _initialize_sqlite_vec(self, conn: sqlite3.Connection) -> None:
        try:
            self._ensure_sqlite_vec_loaded(conn)
        except Exception as exc:
            self.sqlite_vec_available = False
            self._disable_sqlite_vec(f"sqlite-vec unavailable: {exc}")
            return
        dimension = self._configured_or_stored_dimension(conn)
        if dimension is None:
            self._vector_mode = "numpy_fallback"
            self._fallback_reason = "embedding dimension is not configured yet"
            return
        try:
            self._ensure_sqlite_vec_ready(conn, dimension)
        except Exception as exc:
            self._disable_sqlite_vec(f"sqlite-vec table unavailable: {exc}")

    def _ensure_sqlite_vec_loaded(self, conn: sqlite3.Connection) -> Any:
        module = self._sqlite_vec_module
        if module is None:
            module = _import_sqlite_vec()
            self._sqlite_vec_module = module
        self.sqlite_vec_available = True
        load = getattr(module, "load", None)
        if callable(load):
            load(conn)
        return module

    def _ensure_sqlite_vec_ready(self, conn: sqlite3.Connection, dimension: int) -> None:
        self._ensure_sqlite_vec_loaded(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_vec_map (
                item_id TEXT PRIMARY KEY,
                vec_rowid INTEGER UNIQUE NOT NULL
            )
            """
        )
        normalized_dimension = self._validate_embedding_dimension(conn, dimension)
        self._create_sqlite_vec_table(conn, normalized_dimension)
        self._vec_enabled = True
        self._vector_mode = "sqlite_vec"
        self._fallback_reason = ""

    def _create_sqlite_vec_table(self, conn: sqlite3.Connection, dimension: int) -> None:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(embedding float[{int(dimension)}])"
        )

    def _validate_embedding_dimension(self, conn: sqlite3.Connection, dimension: int) -> int:
        clean_dimension = int(dimension)
        if clean_dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        expected = self.configured_embedding_dimension or _coerce_int(
            self._get_setting(conn, "embedding_dimension"),
            default=0,
        )
        if expected:
            if clean_dimension != int(expected):
                raise ValueError(
                    f"embedding dimension mismatch: expected {int(expected)}, got {clean_dimension}"
                )
            clean_dimension = int(expected)
        self._set_setting(conn, "embedding_dimension", str(clean_dimension))
        self._set_setting(conn, "vector_backend_version", "sqlite-vec")
        self._set_setting(conn, "sqlite_vec_enabled", "true")
        return clean_dimension

    def _configured_or_stored_dimension(self, conn: sqlite3.Connection) -> int | None:
        if self.configured_embedding_dimension:
            return self.configured_embedding_dimension
        stored = self._get_setting(conn, "embedding_dimension")
        return _coerce_int(stored, default=0) or None

    def _try_upsert_vector(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        embedding: list[float],
    ) -> None:
        if not self.sqlite_vec_available and self._sqlite_vec_module is None:
            return
        vector = _as_vector(embedding)
        if vector is None:
            return
        try:
            self._ensure_sqlite_vec_ready(conn, int(vector.size))
            self._upsert_vector_row(conn, memory_id, vector)
        except Exception as exc:
            self._disable_sqlite_vec(f"sqlite-vec upsert failed: {exc}")
            self._last_vector_error = str(exc)

    def _upsert_vector_row(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        vector: np.ndarray,
    ) -> None:
        row = conn.execute(
            "SELECT vec_rowid FROM memory_vec_map WHERE item_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            vec_rowid = int(
                conn.execute("SELECT COALESCE(MAX(vec_rowid), 0) + 1 FROM memory_vec_map").fetchone()[0]
            )
            conn.execute(
                "INSERT INTO memory_vec_map (item_id, vec_rowid) VALUES (?, ?)",
                (memory_id, vec_rowid),
            )
        else:
            vec_rowid = int(row["vec_rowid"])
            conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (vec_rowid,))
        conn.execute(
            "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
            (vec_rowid, self._serialize_vector(vector)),
        )

    def _try_delete_vector(self, conn: sqlite3.Connection, memory_id: str) -> None:
        try:
            self._delete_vector_row(conn, memory_id)
        except Exception as exc:
            self._last_vector_error = str(exc)

    def _delete_vector_row(self, conn: sqlite3.Connection, memory_id: str) -> None:
        row = conn.execute(
            "SELECT vec_rowid FROM memory_vec_map WHERE item_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return
        vec_rowid = int(row["vec_rowid"])
        if self._vec_enabled:
            self._ensure_sqlite_vec_loaded(conn)
            conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (vec_rowid,))
        conn.execute("DELETE FROM memory_vec_map WHERE item_id = ?", (memory_id,))

    def _try_clear_vector_table(self, conn: sqlite3.Connection) -> None:
        try:
            if self._vec_enabled:
                self._ensure_sqlite_vec_loaded(conn)
            conn.execute("DELETE FROM memory_vec")
            conn.execute("DELETE FROM memory_vec_map")
        except sqlite3.Error:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vec_map (
                    item_id TEXT PRIMARY KEY,
                    vec_rowid INTEGER UNIQUE NOT NULL
                )
                """
            )

    def _can_use_sqlite_vec(self, query_vector: np.ndarray) -> bool:
        if not self._vec_enabled or self._vector_mode != "sqlite_vec":
            return False
        expected = self.configured_embedding_dimension
        if expected is not None and int(expected) != int(query_vector.size):
            self._last_vector_error = (
                f"embedding dimension mismatch: expected {expected}, got {int(query_vector.size)}"
            )
            return False
        return True

    def _sqlite_vec_search(
        self,
        query_vector: np.ndarray,
        *,
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._ensure_sqlite_vec_loaded(conn)
            where, params = _status_kind_filter(status="active", kinds=kinds)
            filter_sql = where.replace("WHERE ", "AND ", 1) if where else ""
            rows = conn.execute(
                f"""
                SELECT memory_items.*, memory_vec.distance AS vector_distance
                FROM memory_vec
                JOIN memory_vec_map ON memory_vec_map.vec_rowid = memory_vec.rowid
                JOIN memory_items ON memory_items.id = memory_vec_map.item_id
                WHERE memory_vec.embedding MATCH ? AND k = ?
                {filter_sql}
                ORDER BY memory_vec.distance
                LIMIT ?
                """,
                [self._serialize_vector(query_vector), int(limit), *params, int(limit)],
            ).fetchall()
        results = []
        for row in rows:
            item = _row_to_dict(row)
            distance = _coerce_float(item.pop("vector_distance", 0.0), default=0.0)
            item["distance"] = distance
            item["score"] = _distance_to_score(distance)
            results.append(item)
        return sorted(results, key=lambda item: float(item["score"]), reverse=True)[:limit]

    def _serialize_vector(self, vector: np.ndarray) -> Any:
        module = self._sqlite_vec_module
        serialize = getattr(module, "serialize_float32", None) if module is not None else None
        values = [float(item) for item in vector.tolist()]
        if callable(serialize):
            return serialize(values)
        return json.dumps(values).encode("utf-8")

    def _disable_sqlite_vec(self, reason: str) -> None:
        self._vec_enabled = False
        self._vector_mode = "numpy_fallback" if self.db_path.exists() else "disabled"
        self._fallback_reason = str(reason)

    def _get_setting(self, conn: sqlite3.Connection, key: str) -> str:
        try:
            row = conn.execute(
                "SELECT value FROM memory_settings WHERE key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row["value"]) if row is not None and row["value"] is not None else ""

    def _set_setting(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO memory_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def memory_item_from_row(row: dict[str, Any]) -> MemoryItem:
    extra = row.get("extra")
    metadata = dict(extra) if isinstance(extra, dict) else {}
    tags = metadata.pop("tags", [])
    return MemoryItem(
        id=str(row.get("id") or ""),
        type=str(row.get("type") or "fact"),  # type: ignore[arg-type]
        text=str(row.get("summary") or row.get("body") or ""),
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        importance=_coerce_float(row.get("importance"), default=0.5),
        confidence=_coerce_float(row.get("confidence"), default=0.7),
        source=str(row.get("source") or "memory2"),
        source_ref=str(row.get("source_ref") or ""),
        happened_at=_parse_datetime(row.get("happened_at")),
        emotional_weight=_coerce_int(row.get("emotional_weight"), default=0),
        metadata=metadata,
        created_at=_parse_datetime(row.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_datetime(row.get("updated_at")) or datetime.now(timezone.utc),
        last_used_at=_parse_datetime(row.get("last_seen_at")),
        status=str(row.get("status") or "active"),  # type: ignore[arg-type]
    )


def _status_kind_filter(
    *,
    status: str = "active",
    kinds: list[str | None] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    clean_kinds = [str(kind) for kind in (kinds or []) if kind]
    if clean_kinds:
        placeholders = ", ".join("?" for _ in clean_kinds)
        clauses.append(f"type IN ({placeholders})")
        params.extend(clean_kinds)
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    embedding = _json_loads(data.get("embedding_json"), default=None)
    extra = _json_loads(data.get("extra_json"), default={})
    data["embedding"] = embedding if isinstance(embedding, list) else None
    data["extra"] = extra if isinstance(extra, dict) else {}
    return data


def _encode_field_value(key: str, value: Any) -> Any:
    if key in {"embedding_json", "extra_json"} and not isinstance(value, str):
        return _json_dumps(value)
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any, *, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _content_hash(summary: str, body: str) -> str:
    normalized = _normalize_text(f"{summary}\n{body}")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _keyword_score(query: str, item: dict[str, Any]) -> float:
    if not query:
        return float(item.get("importance") or 0.0)
    needle = query.lower()
    haystack = " ".join(
        str(item.get(key) or "").lower()
        for key in ("summary", "body", "type", "source_ref")
    )
    occurrences = haystack.count(needle)
    token_hits = sum(1 for token in needle.split() if token and token in haystack)
    return float(occurrences * 2 + token_hits) + float(item.get("importance") or 0.0)


def _as_vector(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = _json_loads(value, default=None)
    if not isinstance(value, list) or not value:
        return None
    try:
        vector = np.asarray([float(item) for item in value], dtype=float)
    except (TypeError, ValueError):
        return None
    if vector.ndim != 1 or vector.size == 0:
        return None
    return vector


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))


def _distance_to_score(distance: float) -> float:
    clean_distance = max(float(distance), 0.0)
    return 1.0 / (1.0 + clean_distance)


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in ids:
        memory_id = str(raw).strip()
        if memory_id and memory_id not in seen:
            seen.add(memory_id)
            deduped.append(memory_id)
    return deduped


def _sqlite_vec_available() -> bool:
    try:
        _import_sqlite_vec()
    except Exception:
        return False
    return True


def _import_sqlite_vec() -> Any:
    return importlib.import_module("sqlite_vec")
