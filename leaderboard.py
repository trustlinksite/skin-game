"""SQLite-backed online leaderboard replacing Cloudflare Worker."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Any

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
VALID_MODES = {"easy", "normal", "hard", "endless"}

_DB_LOCK = threading.Lock()


def get_db_path() -> str:
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.db")
    return os.environ.get("LEADERBOARD_DB", default_path)


def init_db(db_path: str | None = None) -> None:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _DB_LOCK:
        conn = sqlite3.connect(path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scores (
                    phone TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    wave INTEGER NOT NULL DEFAULT 0,
                    kills INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'normal',
                    win INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scores_rank
                ON scores (wave DESC, kills DESC, updated_at ASC);
                """
            )
            conn.commit()
        finally:
            conn.close()


def normalize_phone(value: Any) -> str:
    s = str(value or "").translate(PERSIAN_DIGITS).translate(ARABIC_DIGITS)
    s = re.sub(r"[^\d+]", "", s)
    if s.startswith("+98"):
        s = "0" + s[3:]
    elif s.startswith("0098"):
        s = "0" + s[4:]
    elif s.startswith("98") and len(s) == 12:
        s = "0" + s[2:]
    elif s.startswith("9") and len(s) == 10:
        s = "0" + s
    return s if re.fullmatch(r"09\d{9}", s) else ""


def sanitize_name(value: Any) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned[:20]


def get_top_scores(limit: int = 10, db_path: str | None = None) -> list[dict[str, Any]]:
    path = db_path or get_db_path()
    with _DB_LOCK:
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT phone, name, wave, kills, mode, win, updated_at
                FROM scores
                ORDER BY wave DESC, kills DESC, updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

    return [
        {
            "phone": r["phone"],
            "name": r["name"],
            "wave": int(r["wave"]),
            "kills": int(r["kills"]),
            "mode": r["mode"],
            "win": bool(r["win"]),
        }
        for r in rows
    ]


def submit_score(payload: dict[str, Any], db_path: str | None = None) -> tuple[int, dict[str, Any]]:
    name = sanitize_name(payload.get("name"))
    phone = normalize_phone(payload.get("phone"))
    if not name:
        return 400, {"ok": False, "error": "bad_name"}
    if not phone:
        return 400, {"ok": False, "error": "bad_phone"}

    try:
        wave = max(0, min(9999, int(payload.get("wave", 0))))
        kills = max(0, min(999999, int(payload.get("kills", 0))))
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "bad_score"}

    mode = str(payload.get("mode", "normal"))
    if mode not in VALID_MODES:
        mode = "normal"
    win = 1 if payload.get("win") else 0
    now_ms = int(time.time() * 1000)

    path = db_path or get_db_path()
    with _DB_LOCK:
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute(
                "SELECT wave, kills, mode, win, updated_at FROM scores WHERE phone = ?",
                (phone,),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO scores (phone, name, wave, kills, mode, win, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (phone, name, wave, kills, mode, win, now_ms),
                )
            else:
                old_wave = int(existing["wave"])
                old_kills = int(existing["kills"])
                is_better = (wave > old_wave) or (wave == old_wave and kills > old_kills)
                if is_better:
                    conn.execute(
                        """
                        UPDATE scores
                        SET name = ?, wave = ?, kills = ?, mode = ?, win = ?, updated_at = ?
                        WHERE phone = ?
                        """,
                        (name, wave, kills, mode, max(win, int(existing["win"])), now_ms, phone),
                    )
                else:
                    conn.execute("UPDATE scores SET name = ? WHERE phone = ?", (name, phone))

            conn.commit()

            me_row = conn.execute(
                "SELECT wave, kills, updated_at FROM scores WHERE phone = ?",
                (phone,),
            ).fetchone()
            rank_row = conn.execute(
                """
                SELECT COUNT(*) + 1 AS rnk
                FROM scores
                WHERE (wave > ?)
                   OR (wave = ? AND kills > ?)
                   OR (wave = ? AND kills = ? AND updated_at < ?)
                """,
                (
                    int(me_row["wave"]),
                    int(me_row["wave"]),
                    int(me_row["kills"]),
                    int(me_row["wave"]),
                    int(me_row["kills"]),
                    int(me_row["updated_at"]),
                ),
            ).fetchone()
            rank = int(rank_row["rnk"]) if rank_row else 1
        finally:
            conn.close()

    top = get_top_scores(10, path)
    return 200, {"ok": True, "rank": rank, "top": top}
