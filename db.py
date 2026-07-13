"""SQLite-хранилище отслеживаемых товаров и истории цен."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

DEFAULT_DB = "price_check.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT NOT NULL,
            title        TEXT,
            last_price   REAL,
            currency     TEXT,
            chat_id      TEXT,
            last_checked TEXT,
            created_at   TEXT NOT NULL,
            active       INTEGER NOT NULL DEFAULT 1
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS price_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL,
            price      REAL NOT NULL,
            currency   TEXT,
            checked_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            chat_id    TEXT PRIMARY KEY,
            username   TEXT,
            status     TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied
            created_at TEXT NOT NULL,
            decided_at TEXT
        )"""
    )
    conn.commit()
    return conn


def get_user(conn, chat_id: str):
    return conn.execute("SELECT * FROM users WHERE chat_id = ?", (str(chat_id),)).fetchone()


def upsert_pending(conn, chat_id: str, username: str = None):
    """Регистрирует нового юзера как pending (если ещё нет)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, username, status, created_at) VALUES (?, ?, 'pending', ?)",
        (str(chat_id), username, _now()),
    )
    if username:
        conn.execute("UPDATE users SET username = ? WHERE chat_id = ?", (username, str(chat_id)))
    conn.commit()
    return cur.rowcount > 0


def set_user_status(conn, chat_id: str, status: str):
    conn.execute(
        "UPDATE users SET status = ?, decided_at = ? WHERE chat_id = ?",
        (status, _now(), str(chat_id)),
    )
    conn.commit()


def add_item(conn, url: str, chat_id: str, title: Optional[str] = None,
             price: Optional[float] = None, currency: Optional[str] = None) -> int:
    cur = conn.execute(
        """INSERT INTO items (url, title, last_price, currency, chat_id, last_checked, created_at, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
        (url, title, price, currency, str(chat_id), _now() if price is not None else None, _now()),
    )
    conn.commit()
    return cur.lastrowid


def get_item(conn, item_id: int):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def list_items(conn, chat_id: Optional[str] = None, active_only: bool = True):
    if chat_id is not None:
        q = "SELECT * FROM items WHERE chat_id = ?"
        args = [str(chat_id)]
        if active_only:
            q += " AND active = 1"
        q += " ORDER BY id"
        return conn.execute(q, args).fetchall()
    q = "SELECT * FROM items"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY id"
    return conn.execute(q).fetchall()


def remove_item(conn, item_id: int, chat_id: Optional[str] = None) -> bool:
    """Удаляет товар. Если задан chat_id — только если товар принадлежит этому юзеру
    (защита от удаления чужих товаров)."""
    if chat_id is not None:
        res = conn.execute(
            "DELETE FROM items WHERE id = ? AND chat_id = ?", (item_id, str(chat_id))
        )
    else:
        res = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return res.rowcount > 0


def deactivate(conn, item_id: int):
    conn.execute("UPDATE items SET active = 0 WHERE id = ?", (item_id,))
    conn.commit()


def update_price(conn, item_id: int, price: float, currency: Optional[str], checked: bool = True):
    conn.execute(
        "UPDATE items SET last_price = ?, currency = ?, last_checked = ? WHERE id = ?",
        (price, currency, _now() if checked else None, item_id),
    )
    conn.execute(
        "INSERT INTO price_history (item_id, price, currency, checked_at) VALUES (?, ?, ?, ?)",
        (item_id, price, currency, _now()),
    )
    conn.commit()


def history(conn, item_id: int, limit: int = 50):
    return conn.execute(
        "SELECT * FROM price_history WHERE item_id = ? ORDER BY id DESC LIMIT ?",
        (item_id, limit),
    ).fetchall()


def all_active(conn):
    return conn.execute("SELECT * FROM items WHERE active = 1 ORDER BY id").fetchall()
