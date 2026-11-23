import json
import re
import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spool(
  id INTEGER PRIMARY KEY AUTOINCREMENT, short_code TEXT UNIQUE,
  spool_weight REAL, initial_weight REAL, last_used TEXT);
CREATE TABLE IF NOT EXISTS filament(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS vendor(id INTEGER PRIMARY KEY AUTOINCREMENT, name_lc TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS extra(
  spool_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(spool_id, key));
CREATE TABLE IF NOT EXISTS tag(uid TEXT PRIMARY KEY, spool_id INTEGER, format TEXT);
CREATE TABLE IF NOT EXISTS binding(slot INTEGER PRIMARY KEY, spool_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS unbound(spool_id INTEGER PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""

_LOCAL_FIELDS = ("spool_weight", "initial_weight", "last_used")


def normalise_uid(s: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", s).upper()


@dataclass
class Local:
    spool_weight: float | None = None
    initial_weight: float | None = None
    last_used: str | None = None


@dataclass
class Quota:
    month: str
    remaining: int | None
    used: int
    warned: bool


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self._db.execute(sql, args)
        self._db.commit()
        return cur

    def _one(self, sql: str, args: tuple = ()):
        return self._db.execute(sql, args).fetchone()

    def _intern(self, table: str, col: str, value: str) -> int:
        # select first: INSERT OR IGNORE would burn AUTOINCREMENT values
        row = self._one(f"SELECT id FROM {table} WHERE {col}=?", (value,))
        if row:
            return row[0]
        return self._write(f"INSERT INTO {table}({col}) VALUES(?)", (value,)).lastrowid

    # ids
    def spool_id(self, short_code: str) -> int:
        return self._intern("spool", "short_code", short_code)

    def short_code(self, spool_id: int) -> str | None:
        row = self._one("SELECT short_code FROM spool WHERE id=?", (spool_id,))
        return row[0] if row else None

    def filament_id(self, key: str) -> int:
        return self._intern("filament", "key", key)

    def vendor_id(self, name: str) -> int:
        return self._intern("vendor", "name_lc", name.lower())

    # local fields
    def local(self, spool_id: int) -> Local:
        row = self._one(
            "SELECT spool_weight, initial_weight, last_used FROM spool WHERE id=?",
            (spool_id,),
        )
        return Local(*row) if row else Local()

    def set_local(self, spool_id: int, **fields) -> None:
        bad = set(fields) - set(_LOCAL_FIELDS)
        if bad:
            raise ValueError(f"unknown local fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._write(f"UPDATE spool SET {sets} WHERE id=?", (*fields.values(), spool_id))

    # extra
    def extra(self, spool_id: int) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT key, value FROM extra WHERE spool_id=?", (spool_id,)
        ).fetchall()
        return dict(rows)

    def merge_extra(self, spool_id: int, d: dict[str, str]) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO extra(spool_id, key, value) VALUES(?,?,?)",
            [(spool_id, k, v) for k, v in d.items()],
        )
        self._db.commit()

    # tags
    def tags(self, spool_id: int) -> list[tuple[str, str]]:
        return self._db.execute(
            "SELECT uid, format FROM tag WHERE spool_id=? ORDER BY uid", (spool_id,)
        ).fetchall()

    def tag_owner(self, uid_norm: str) -> int | None:
        row = self._one("SELECT spool_id FROM tag WHERE uid=?", (uid_norm,))
        return row[0] if row else None

    def link_tag(self, spool_id: int, uid_norm: str, fmt: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO tag(uid, spool_id, format) VALUES(?,?,?)",
            (uid_norm, spool_id, fmt),
        )

    def unlink_tag(self, spool_id: int, uid_norm: str) -> bool:
        cur = self._write(
            "DELETE FROM tag WHERE uid=? AND spool_id=?", (uid_norm, spool_id)
        )
        return cur.rowcount > 0

    # bindings
    def bindings(self) -> dict[int, int]:
        return dict(self._db.execute("SELECT slot, spool_id FROM binding").fetchall())

    def set_binding(self, slot: int, spool_id: int) -> None:
        self._write(
            "INSERT OR REPLACE INTO binding(slot, spool_id) VALUES(?,?)",
            (slot, spool_id),
        )

    def clear_binding(self, slot: int) -> None:
        self._write("DELETE FROM binding WHERE slot=?", (slot,))

    def last_unbound(self, spool_id: int) -> float | None:
        row = self._one("SELECT ts FROM unbound WHERE spool_id=?", (spool_id,))
        return row[0] if row else None

    def mark_unbound(self, spool_id: int, ts: float) -> None:
        self._write(
            "INSERT OR REPLACE INTO unbound(spool_id, ts) VALUES(?,?)", (spool_id, ts)
        )

    # kv: quota and snapshot
    def _kv_get(self, key: str):
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row[0]) if row else None

    def _kv_set(self, key: str, value) -> None:
        self._write(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, json.dumps(value))
        )

    def quota(self) -> Quota:
        d = self._kv_get("quota")
        if not d:
            return Quota(month="", remaining=None, used=0, warned=False)
        return Quota(**d)

    def set_quota(
        self, month: str, remaining: int | None, used: int, warned: bool
    ) -> None:
        self._kv_set(
            "quota",
            {"month": month, "remaining": remaining, "used": used, "warned": warned},
        )

    def snapshot(self) -> list[dict] | None:
        return self._kv_get("snapshot")

    def save_snapshot(self, raw: list[dict], ts: float) -> None:
        self._kv_set("snapshot", raw)
        self._kv_set("snapshot_ts", ts)

    def snapshot_ts(self) -> float:
        return self._kv_get("snapshot_ts") or 0.0
