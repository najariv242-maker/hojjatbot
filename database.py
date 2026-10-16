import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

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
            "assigned_name TEXT NULL,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "is_used INTEGER DEFAULT 0,"
            "used_by INTEGER NULL,"
            "used_at TIMESTAMP NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS students ("
            "user_id INTEGER PRIMARY KEY,"
            "username TEXT NULL,"
            "full_name TEXT NOT NULL,"
            "assigned_name TEXT NULL,"
            "current_stage INTEGER DEFAULT 1,"
            "registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "last_drip_sent_at TIMESTAMP NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS stage_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "stage INTEGER NOT NULL,"
            "from_chat_id INTEGER NOT NULL,"
            "message_id INTEGER NOT NULL,"
            "caption TEXT NULL,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ");"
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY,"
            "value TEXT NOT NULL"
            ");"
            "CREATE TABLE IF NOT EXISTS delivered_drip_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER NOT NULL,"
            "message_record_id INTEGER NOT NULL,"
            "delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "UNIQUE(user_id, message_record_id)"
            ");"
            "CREATE INDEX IF NOT EXISTS idx_stage ON stage_messages(stage);"
        )

        # Migration column checks
        try:
            conn.execute("ALTER TABLE invites ADD COLUMN assigned_name TEXT NULL;")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE students ADD COLUMN assigned_name TEXT NULL;")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE students ADD COLUMN last_drip_sent_at TIMESTAMP NULL;")
        except sqlite3.OperationalError:
            pass

        # Default settings if not exist
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("drip_interval_days", "2"))
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("drip_send_hour", "23"))

    conn.close()

def get_setting(db_path: str, key: str, default: str = "") -> str:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def set_setting(db_path: str, key: str, value: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value)
            )
    finally:
        conn.close()

def create_invite(db_path: str, code: str, assigned_name: Optional[str] = None) -> str:
    conn = get_connection(db_path)
    with conn:
        conn.execute(
            "INSERT INTO invites (code, assigned_name, created_at, is_used) VALUES (?, ?, ?, 0)",
            (code, assigned_name, datetime.utcnow())
        )
    conn.close()
    return code

def validate_and_use_invite(db_path: str, code: str, user_id: int, username: Optional[str], full_name: str) -> Tuple[bool, Optional[str]]:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "SELECT code, assigned_name, is_used FROM invites WHERE code = ?",
                (code,)
            )
            invite = cur.fetchone()
            if not invite or invite["is_used"] == 1:
                return False, None

            assigned_name = invite["assigned_name"]
            now = datetime.utcnow()
            conn.execute(
                "UPDATE invites SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?",
                (user_id, now, code)
            )

            conn.execute(
                "INSERT INTO students (user_id, username, full_name, assigned_name, current_stage, registered_at, last_active) "
                "VALUES (?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username = excluded.username, "
                "full_name = excluded.full_name, "
                "assigned_name = COALESCE(excluded.assigned_name, students.assigned_name), "
                "last_active = excluded.last_active",
                (user_id, username, full_name, assigned_name, now, now)
            )
        return True, assigned_name
    finally:
        conn.close()

def get_student(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, last_drip_sent_at FROM students WHERE user_id = ?",
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
            "SELECT user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, last_drip_sent_at FROM students ORDER BY registered_at DESC"
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

def get_next_unsent_drip_message(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT sm.id, sm.stage, sm.from_chat_id, sm.message_id, sm.caption, sm.created_at "
            "FROM stage_messages sm "
            "LEFT JOIN delivered_drip_messages ddm ON sm.id = ddm.message_record_id AND ddm.user_id = ? "
            "WHERE sm.stage = 4 AND ddm.id IS NULL "
            "ORDER BY sm.id ASC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def record_drip_delivery(db_path: str, user_id: int, message_record_id: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            now = datetime.utcnow()
            conn.execute(
                "INSERT OR IGNORE INTO delivered_drip_messages (user_id, message_record_id, delivered_at) VALUES (?, ?, ?)",
                (user_id, message_record_id, now)
            )
            conn.execute(
                "UPDATE students SET last_drip_sent_at = ? WHERE user_id = ?",
                (now, user_id)
            )
    finally:
        conn.close()

def get_eligible_students_for_drip(db_path: str, force_ignore_interval: bool = False) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        interval_days = int(get_setting(db_path, "drip_interval_days", "2"))
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, last_drip_sent_at FROM students "
            "WHERE current_stage >= 4"
        )
        candidates = [dict(row) for row in cur.fetchall()]
        eligible = []
        now = datetime.utcnow()

        for st in candidates:
            # Check if there is an unsent message in stage 4
            next_msg = get_next_unsent_drip_message(db_path, st["user_id"])
            if not next_msg:
                continue

            if force_ignore_interval:
                eligible.append((st, next_msg))
                continue

            last_sent_str = st.get("last_drip_sent_at")
            if not last_sent_str:
                eligible.append((st, next_msg))
            else:
                try:
                    last_sent = datetime.fromisoformat(last_sent_str) if isinstance(last_sent_str, str) else last_sent_str
                    diff_days = (now - last_sent).total_seconds() / 86400.0
                    if diff_days >= interval_days:
                        eligible.append((st, next_msg))
                except Exception:
                    eligible.append((st, next_msg))
        return eligible
    finally:
        conn.close()
