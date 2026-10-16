import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn

def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS invites ("
            "code TEXT PRIMARY KEY,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "is_used INTEGER DEFAULT 0,"
            "used_by INTEGER NULL,"
            "used_at TIMESTAMP NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS students ("
            "user_id INTEGER PRIMARY KEY,"
            "username TEXT NULL,"
            "full_name TEXT NOT NULL,"
            "current_stage INTEGER DEFAULT 1,"
            "registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ");"
            "CREATE TABLE IF NOT EXISTS stage_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "stage INTEGER NOT NULL,"
            "from_chat_id INTEGER NOT NULL,"
            "message_id INTEGER NOT NULL,"
            "caption TEXT NULL,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_stage ON stage_messages(stage);"
        )
    conn.close()

def create_invite(db_path: str, code: str) -> str:
    conn = get_connection(db_path)
    with conn:
        conn.execute(
            "INSERT INTO invites (code, created_at, is_used) VALUES (?, ?, 0)",
            (code, datetime.utcnow())
        )
    conn.close()
    return code

def validate_and_use_invite(db_path: str, code: str, user_id: int, username: Optional[str], full_name: str) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "SELECT code, is_used FROM invites WHERE code = ?",
                (code,)
            )
            invite = cur.fetchone()
            if not invite or invite["is_used"] == 1:
                return False

            now = datetime.utcnow()
            conn.execute(
                "UPDATE invites SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?",
                (user_id, now, code)
            )

            conn.execute(
                "INSERT INTO students (user_id, username, full_name, current_stage, registered_at, last_active) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username = excluded.username, "
                "full_name = excluded.full_name, "
                "last_active = excluded.last_active",
                (user_id, username, full_name, now, now)
            )
        return True
    finally:
        conn.close()

def get_student(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, current_stage, registered_at, last_active FROM students WHERE user_id = ?",
            (user_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def update_student_stage(db_path: str, user_id: int, new_stage: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE students SET current_stage = ?, last_active = ? WHERE user_id = ?",
                (new_stage, datetime.utcnow(), user_id)
            )
    finally:
        conn.close()

def add_stage_message(db_path: str, stage: int, from_chat_id: int, message_id: int, caption: Optional[str] = None) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO stage_messages (stage, from_chat_id, message_id, caption, created_at) VALUES (?, ?, ?, ?, ?)",
                (stage, from_chat_id, message_id, caption, datetime.utcnow())
            )
            return cur.lastrowid
    finally:
        conn.close()

def get_stage_messages(db_path: str, stage: int) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, created_at FROM stage_messages WHERE stage = ? ORDER BY id ASC",
            (stage,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def list_students(db_path: str) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, current_stage, registered_at, last_active FROM students ORDER BY registered_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def get_student_count_by_stage(db_path: str) -> Dict[int, int]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT current_stage, COUNT(*) as cnt FROM students GROUP BY current_stage"
        )
        return {row["current_stage"]: row["cnt"] for row in cur.fetchall()}
    finally:
        conn.close()
