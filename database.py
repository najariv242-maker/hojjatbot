import os
import re
import json
import sqlite3
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

def adapt_datetime(val: datetime) -> str:
    return val.strftime("%Y-%m-%d %H:%M:%S")

sqlite3.register_adapter(datetime, adapt_datetime)

def get_now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def parse_db_datetime(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None)
    val_str = str(val).strip().replace("Z", "").replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(val_str, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(val_str).replace(tzinfo=None)
    except Exception:
        return None

def normalize_persian_text(text: Optional[str]) -> str:
    """Normalizes Persian/Arabic characters, digits, and whitespace for accurate matching."""
    if not text:
        return ""
    s = str(text).strip().lower()
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "آ": "ا",
        "أ": "ا",
        "إ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "ة": "ه",
        "ۀ": "ه",
        "\u200c": " ",
        "\u200f": "",
        "\u200e": "",
        "_": " ",
        "-": " ",
        "‌": " ",
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    }
    for src, dst in replacements.items():
        s = s.replace(src, dst)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def convert_digits_to_english(text: Optional[str]) -> str:
    """Converts Persian and Arabic-Indic digits to standard ASCII digits."""
    if not text:
        return ""
    s = str(text)
    digit_map = {
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    }
    for k, v in digit_map.items():
        s = s.replace(k, v)
    return s

def get_connection(db_path: str) -> sqlite3.Connection:
    dir_name = os.path.dirname(db_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        pass
    return conn

def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
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
                "stage_updated_at TIMESTAMP NULL,"
                "stage_warning_sent INTEGER DEFAULT 0,"
                "last_drip_sent_at TIMESTAMP NULL,"
                "day_8_sent INTEGER DEFAULT 0,"
                "day_30_sent INTEGER DEFAULT 0,"
                "admin_notified INTEGER DEFAULT 0"
                ");"
                "CREATE TABLE IF NOT EXISTS stage_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "stage INTEGER NOT NULL,"
                "from_chat_id INTEGER NOT NULL,"
                "message_id INTEGER NOT NULL,"
                "caption TEXT NULL,"
                "package_id INTEGER NULL,"
                "file_unique_id TEXT NULL,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY,"
                "value TEXT NOT NULL"
                ");"
                "CREATE TABLE IF NOT EXISTS user_states ("
                "user_id INTEGER PRIMARY KEY,"
                "state TEXT NULL,"
                "package_id INTEGER NULL,"
                "broadcast_json TEXT NULL,"
                "dossier_json TEXT NULL,"
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE TABLE IF NOT EXISTS delivered_drip_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL,"
                "message_record_id INTEGER NOT NULL,"
                "package_id INTEGER NULL,"
                "from_chat_id INTEGER NULL,"
                "source_message_id INTEGER NULL,"
                "delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "UNIQUE(user_id, message_record_id)"
                ");"
                "CREATE TABLE IF NOT EXISTS study_plans ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL,"
                "duration_days INTEGER NOT NULL,"
                "start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "end_date TIMESTAMP NOT NULL,"
                "reminder_date TIMESTAMP NOT NULL,"
                "reminder_sent INTEGER DEFAULT 0,"
                "report_submitted INTEGER DEFAULT 0,"
                "report_text TEXT NULL,"
                "status TEXT DEFAULT 'ACTIVE',"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE TABLE IF NOT EXISTS intro_contents ("
                "id INTEGER PRIMARY KEY CHECK (id = 1),"
                "from_chat_id INTEGER NULL,"
                "message_id INTEGER NULL,"
                "caption TEXT NULL,"
                "text_content TEXT NULL,"
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE TABLE IF NOT EXISTS delivered_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL,"
                "stage INTEGER NOT NULL,"
                "message_record_id INTEGER NOT NULL,"
                "from_chat_id INTEGER NULL,"
                "source_message_id INTEGER NULL,"
                "delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "UNIQUE(user_id, message_record_id)"
                ");"
                "CREATE TABLE IF NOT EXISTS student_sent_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL,"
                "sent_message_id INTEGER NOT NULL,"
                "content_type TEXT DEFAULT 'voice',"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "UNIQUE(user_id, sent_message_id)"
                ");"
                "CREATE TABLE IF NOT EXISTS removed_students ("
                "user_id INTEGER PRIMARY KEY,"
                "full_name TEXT NULL,"
                "assigned_name TEXT NULL,"
                "username TEXT NULL,"
                "removed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE INDEX IF NOT EXISTS idx_stage ON stage_messages(stage);"
                "CREATE INDEX IF NOT EXISTS idx_stage_pkg ON stage_messages(stage, package_id);"
                "CREATE INDEX IF NOT EXISTS idx_delivered_package ON delivered_drip_messages(user_id, package_id);"
                "CREATE INDEX IF NOT EXISTS idx_deliv_stage_msg ON delivered_messages(user_id, stage, message_record_id);"
                "CREATE INDEX IF NOT EXISTS idx_study_plans_user ON study_plans(user_id);"
                "CREATE INDEX IF NOT EXISTS idx_sent_msgs_user ON student_sent_messages(user_id);"
                "CREATE TABLE IF NOT EXISTS student_dossier ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL,"
                "from_chat_id INTEGER NULL,"
                "message_id INTEGER NULL,"
                "media_type TEXT NULL,"
                "caption TEXT NULL,"
                "text_content TEXT NULL,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
                "CREATE INDEX IF NOT EXISTS idx_dossier_user ON student_dossier(user_id);"
            )

            # Migration column checks for existing databases
            for col_def in [
                "assigned_name TEXT NULL",
                "last_drip_sent_at TIMESTAMP NULL",
                "day_8_sent INTEGER DEFAULT 0",
                "day_30_sent INTEGER DEFAULT 0",
                "stage_updated_at TIMESTAMP NULL",
                "stage_warning_sent INTEGER DEFAULT 0",
                "admin_notified INTEGER DEFAULT 0",
            ]:
                try:
                    conn.execute(f"ALTER TABLE students ADD COLUMN {col_def};")
                except sqlite3.OperationalError:
                    pass

            for col_def, tbl in [
                ("package_id INTEGER NULL", "stage_messages"),
                ("file_unique_id TEXT NULL", "stage_messages"),
                ("package_id INTEGER NULL", "delivered_drip_messages"),
                ("from_chat_id INTEGER NULL", "delivered_drip_messages"),
                ("source_message_id INTEGER NULL", "delivered_drip_messages"),
                ("from_chat_id INTEGER NULL", "delivered_messages"),
                ("source_message_id INTEGER NULL", "delivered_messages"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col_def};")
                except sqlite3.OperationalError:
                    pass

            # Backfill stage_updated_at for existing records
            try:
                conn.execute("UPDATE students SET stage_updated_at = COALESCE(last_active, registered_at, CURRENT_TIMESTAMP) WHERE stage_updated_at IS NULL;")
            except sqlite3.OperationalError:
                pass

            # Backfill package_id for existing stage_messages
            try:
                conn.execute("UPDATE stage_messages SET package_id = id WHERE package_id IS NULL;")
            except sqlite3.OperationalError:
                pass

            # Backfill source_message_id and from_chat_id in delivered_messages & delivered_drip_messages
            try:
                conn.execute(
                    "UPDATE delivered_messages SET "
                    "from_chat_id = (SELECT sm.from_chat_id FROM stage_messages sm WHERE sm.id = delivered_messages.message_record_id), "
                    "source_message_id = (SELECT sm.message_id FROM stage_messages sm WHERE sm.id = delivered_messages.message_record_id) "
                    "WHERE source_message_id IS NULL AND message_record_id > 0;"
                )
                conn.execute(
                    "UPDATE delivered_drip_messages SET "
                    "from_chat_id = (SELECT sm.from_chat_id FROM stage_messages sm WHERE sm.id = delivered_drip_messages.message_record_id), "
                    "source_message_id = (SELECT sm.message_id FROM stage_messages sm WHERE sm.id = delivered_drip_messages.message_record_id) "
                    "WHERE source_message_id IS NULL AND message_record_id > 0;"
                )
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE invites ADD COLUMN assigned_name TEXT NULL;")
            except sqlite3.OperationalError:
                pass

            # Default settings if not exist
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("drip_interval_days", "2"))
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("drip_send_hour", "23"))
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("morning_quote_index", "0"))
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", ("last_morning_quote_date", ""))
    finally:
        conn.close()

    # Automatically clean up any duplicate stage messages on startup
    try:
        deduplicate_stage_messages(db_path)
    except Exception:
        pass

# --- Persistent User State Helpers (Prevents Lost State Across Restarts/Workers) ---

def get_user_state(db_path: str, user_id: int) -> Optional[str]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT state FROM user_states WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row["state"] if row and row["state"] else None
    except Exception:
        return None
    finally:
        conn.close()

def set_user_state(db_path: str, user_id: int, state: Optional[str]) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO user_states (user_id, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (user_id, state, get_now_utc())
            )
    except Exception:
        pass
    finally:
        conn.close()

def get_user_package_id(db_path: str, user_id: int) -> Optional[int]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT package_id FROM user_states WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return int(row["package_id"]) if row and row["package_id"] is not None else None
    except Exception:
        return None
    finally:
        conn.close()

def set_user_package_id(db_path: str, user_id: int, package_id: Optional[int]) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO user_states (user_id, package_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET package_id = excluded.package_id, updated_at = excluded.updated_at",
                (user_id, package_id, get_now_utc())
            )
    except Exception:
        pass
    finally:
        conn.close()

def get_pending_broadcast(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT broadcast_json FROM user_states WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row["broadcast_json"]:
            return json.loads(row["broadcast_json"])
        return None
    except Exception:
        return None
    finally:
        conn.close()

def set_pending_broadcast(db_path: str, user_id: int, data: Optional[Dict[str, Any]]) -> None:
    conn = get_connection(db_path)
    try:
        raw = json.dumps(data, ensure_ascii=False) if data is not None else None
        with conn:
            conn.execute(
                "INSERT INTO user_states (user_id, broadcast_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET broadcast_json = excluded.broadcast_json, updated_at = excluded.updated_at",
                (user_id, raw, get_now_utc())
            )
    except Exception:
        pass
    finally:
        conn.close()

def get_pending_dossier_note(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT dossier_json FROM user_states WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row["dossier_json"]:
            return json.loads(row["dossier_json"])
        return None
    except Exception:
        return None
    finally:
        conn.close()

def set_pending_dossier_note(db_path: str, user_id: int, data: Optional[Dict[str, Any]]) -> None:
    conn = get_connection(db_path)
    try:
        raw = json.dumps(data, ensure_ascii=False) if data is not None else None
        with conn:
            conn.execute(
                "INSERT INTO user_states (user_id, dossier_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET dossier_json = excluded.dossier_json, updated_at = excluded.updated_at",
                (user_id, raw, get_now_utc())
            )
    except Exception:
        pass
    finally:
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

def claim_morning_quote_date(db_path: str, today_str: str, total_quotes: int) -> Tuple[bool, int]:
    """Atomically checks and claims today's morning quote delivery so it can never be sent twice."""
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT value FROM settings WHERE key = 'last_morning_quote_date'")
        row = cur.fetchone()
        last_date = row["value"] if row else ""
        if last_date == today_str:
            conn.rollback()
            return False, 0

        cur_idx_row = conn.execute("SELECT value FROM settings WHERE key = 'morning_quote_index'").fetchone()
        try:
            curr_idx = int(cur_idx_row["value"]) if cur_idx_row else 0
        except ValueError:
            curr_idx = 0

        next_idx = (curr_idx + 1) % max(1, total_quotes)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('last_morning_quote_date', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (today_str,)
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('morning_quote_index', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(next_idx),)
        )
        conn.commit()
        return True, curr_idx
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, 0
    finally:
        conn.close()

def create_invite(db_path: str, code: str, assigned_name: Optional[str] = None) -> str:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO invites (code, assigned_name, created_at, is_used) VALUES (?, ?, ?, 0)",
                (code, assigned_name, get_now_utc())
            )
    finally:
        conn.close()
    return code

def get_invite(db_path: str, code: str) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT code, assigned_name, created_at, is_used, used_by, used_at FROM invites WHERE code = ?",
            (code,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def validate_and_use_invite(db_path: str, code: str, user_id: int, username: Optional[str], full_name: str) -> Tuple[bool, Optional[str]]:
    """
    Atomically validates and marks an invite code as used using BEGIN IMMEDIATE.
    Prevents race conditions when a student taps Start multiple times concurrently.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT code, assigned_name, is_used, used_by FROM invites WHERE code = ?",
            (code,)
        )
        invite = cur.fetchone()
        if not invite or invite["is_used"] == 1:
            conn.rollback()
            return False, None

        assigned_name = invite["assigned_name"]
        now = get_now_utc()
        upd = conn.execute(
            "UPDATE invites SET is_used = 1, used_by = ?, used_at = ? WHERE code = ? AND is_used = 0",
            (user_id, now, code)
        )
        if upd.rowcount == 0:
            conn.rollback()
            return False, None

        # Remove from removed_students if re-invited with a fresh invite link
        conn.execute("DELETE FROM removed_students WHERE user_id = ?", (user_id,))

        conn.execute(
            "INSERT INTO students (user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, stage_updated_at, stage_warning_sent, day_8_sent, day_30_sent, admin_notified) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0, 0, 0, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, "
            "full_name = excluded.full_name, "
            "assigned_name = COALESCE(excluded.assigned_name, students.assigned_name), "
            "last_active = excluded.last_active",
            (user_id, username, full_name, assigned_name, now, now, now)
        )
        conn.commit()
        return True, assigned_name
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, None
    finally:
        conn.close()

def claim_admin_registration_notification(db_path: str, user_id: int) -> bool:
    """Atomically marks that the admin was notified of this student's registration. Returns True only once."""
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE students SET admin_notified = 1 WHERE user_id = ? AND (admin_notified IS NULL OR admin_notified = 0)",
            (user_id,)
        )
        claimed = cur.rowcount > 0
        conn.commit()
        return claimed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()

def get_student(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, stage_updated_at, stage_warning_sent, last_drip_sent_at, day_8_sent, day_30_sent FROM students WHERE user_id = ?",
            (user_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def is_student_removed(db_path: str, user_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT 1 FROM removed_students WHERE user_id = ? LIMIT 1", (user_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()

def record_student_sent_message(db_path: str, user_id: int, sent_message_id: int, content_type: str = "voice") -> None:
    if not user_id or not sent_message_id:
        return
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO student_sent_messages (user_id, sent_message_id, content_type, created_at) VALUES (?, ?, ?, ?)",
                (user_id, sent_message_id, content_type, get_now_utc())
            )
    finally:
        conn.close()

def get_student_sent_messages(db_path: str, user_id: int) -> List[int]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT sent_message_id FROM student_sent_messages WHERE user_id = ? ORDER BY sent_message_id DESC",
            (user_id,)
        )
        return [row["sent_message_id"] for row in cur.fetchall()]
    finally:
        conn.close()

def delete_student_completely(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    """Deletes a student and all their capabilities, study plans, delivery logs, states, and dossier records."""
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "SELECT user_id, username, full_name, assigned_name, current_stage FROM students WHERE user_id = ?",
                (user_id,)
            )
            row = cur.fetchone()
            student = dict(row) if row else None

            conn.execute(
                "INSERT OR REPLACE INTO removed_students (user_id, full_name, assigned_name, username, removed_at) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    student.get("full_name") if student else None,
                    student.get("assigned_name") if student else None,
                    student.get("username") if student else None,
                    get_now_utc(),
                )
            )
            conn.execute("DELETE FROM students WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM study_plans WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM student_dossier WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM delivered_messages WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM delivered_drip_messages WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM student_sent_messages WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE invites SET is_used = 1 WHERE used_by = ?", (user_id,))
            return student
    finally:
        conn.close()

def update_student_stage(db_path: str, user_id: int, new_stage: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            now = get_now_utc()
            conn.execute(
                "UPDATE students SET current_stage = ?, last_active = ?, stage_updated_at = ?, stage_warning_sent = 0 WHERE user_id = ?",
                (new_stage, now, now, user_id)
            )
    finally:
        conn.close()

def get_students_due_for_inactivity_warning(db_path: str) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, registered_at, stage_updated_at, stage_warning_sent "
            "FROM students "
            "WHERE current_stage BETWEEN 1 AND 3 AND (stage_warning_sent IS NULL OR stage_warning_sent = 0)"
        )
        candidates = [dict(row) for row in cur.fetchall()]
        due = []
        now = get_now_utc()
        for st in candidates:
            st_time = st.get("stage_updated_at") or st.get("registered_at")
            if not st_time:
                continue
            st_dt = parse_db_datetime(st_time)
            if not st_dt:
                continue
            diff_hours = (now - st_dt).total_seconds() / 3600.0
            stage = st.get("current_stage", 1)
            # Stage 1: 18 hours; Stage 2 & 3: 24 hours
            threshold_hours = 18.0 if stage == 1 else 24.0
            if diff_hours >= threshold_hours:
                st["hours_waiting"] = diff_hours
                due.append(st)
        return due
    finally:
        conn.close()

def mark_inactivity_warning_sent(db_path: str, user_id: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE students SET stage_warning_sent = 1 WHERE user_id = ?",
                (user_id,)
            )
    finally:
        conn.close()

def mark_milestone_sent(db_path: str, user_id: int, milestone_days: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            col = "day_8_sent" if milestone_days == 8 else "day_30_sent"
            conn.execute(f"UPDATE students SET {col} = 1 WHERE user_id = ?", (user_id,))
    finally:
        conn.close()

def get_students_due_for_milestones(db_path: str) -> List[Tuple[Dict[str, Any], int]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, registered_at, day_8_sent, day_30_sent FROM students"
        )
        students = [dict(row) for row in cur.fetchall()]
        due = []
        now = get_now_utc()

        for st in students:
            reg_str = st.get("registered_at")
            if not reg_str:
                continue
            reg_dt = parse_db_datetime(reg_str)
            if not reg_dt:
                continue
            diff_days = (now - reg_dt).total_seconds() / 86400.0

            if diff_days >= 8.0 and not st.get("day_8_sent"):
                due.append((st, 8))

            if diff_days >= 30.0 and not st.get("day_30_sent"):
                due.append((st, 30))
        return due
    finally:
        conn.close()

def add_stage_message(
    db_path: str,
    stage: int,
    from_chat_id: int,
    message_id: int,
    caption: Optional[str] = None,
    package_id: Optional[int] = None,
    file_unique_id: Optional[str] = None
) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            # 1. Prevent duplicate upload of the exact same (from_chat_id, message_id) in the same stage
            existing = conn.execute(
                "SELECT id FROM stage_messages WHERE stage = ? AND from_chat_id = ? AND message_id = ? LIMIT 1",
                (stage, from_chat_id, message_id)
            ).fetchone()
            if existing:
                return existing["id"]

            # 2. Prevent duplicate upload of the same media file (via Telegram file_unique_id) in the same stage
            if file_unique_id:
                existing_uid = conn.execute(
                    "SELECT id FROM stage_messages WHERE stage = ? AND file_unique_id = ? LIMIT 1",
                    (stage, file_unique_id)
                ).fetchone()
                if existing_uid:
                    return existing_uid["id"]

            # 3. For pure text messages or long captions without file_unique_id, prevent exact duplicate text in the same stage
            if not file_unique_id and caption and len(caption.strip()) >= 15:
                existing_cap = conn.execute(
                    "SELECT id FROM stage_messages WHERE stage = ? AND file_unique_id IS NULL AND caption = ? LIMIT 1",
                    (stage, caption)
                ).fetchone()
                if existing_cap:
                    return existing_cap["id"]

            cur = conn.execute(
                "INSERT INTO stage_messages (stage, from_chat_id, message_id, caption, package_id, file_unique_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stage, from_chat_id, message_id, caption, package_id, file_unique_id, get_now_utc())
            )
            inserted_id = cur.lastrowid
            if package_id is None:
                conn.execute("UPDATE stage_messages SET package_id = ? WHERE id = ?", (inserted_id, inserted_id))
            return inserted_id
    finally:
        conn.close()

def deduplicate_stage_messages(db_path: str) -> int:
    """Removes duplicate stage messages within the same stage (same message_id, same file_unique_id, or identical long text)."""
    conn = get_connection(db_path)
    removed = 0
    try:
        with conn:
            rows = conn.execute(
                "SELECT id, stage, from_chat_id, message_id, caption, file_unique_id FROM stage_messages ORDER BY id ASC"
            ).fetchall()
            seen_msg_keys = set()
            seen_uid_keys = set()
            seen_cap_keys = set()
            to_delete = []

            for r in rows:
                rid = r["id"]
                st = r["stage"]
                fchat = r["from_chat_id"]
                mid = r["message_id"]
                fuid = r["file_unique_id"]
                cap = (r["caption"] or "").strip()

                msg_key = (st, fchat, mid)
                if msg_key in seen_msg_keys:
                    to_delete.append(rid)
                    continue
                seen_msg_keys.add(msg_key)

                if fuid:
                    uid_key = (st, fuid)
                    if uid_key in seen_uid_keys:
                        to_delete.append(rid)
                        continue
                    seen_uid_keys.add(uid_key)
                elif len(cap) >= 15:
                    cap_key = (st, cap)
                    if cap_key in seen_cap_keys:
                        to_delete.append(rid)
                        continue
                    seen_cap_keys.add(cap_key)

            for rid in to_delete:
                conn.execute("DELETE FROM stage_messages WHERE id = ?", (rid,))
                removed += 1
        return removed
    finally:
        conn.close()

def delete_stage_message_by_id(db_path: str, record_id: int) -> Optional[Dict[str, Any]]:
    """Deletes a single stage message by its primary key ID and returns the deleted record."""
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "SELECT id, stage, from_chat_id, message_id, caption, package_id FROM stage_messages WHERE id = ?",
                (record_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            conn.execute("DELETE FROM stage_messages WHERE id = ?", (record_id,))
            return item
    finally:
        conn.close()

def get_stage_messages(db_path: str, stage: int) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, package_id, file_unique_id, created_at FROM stage_messages WHERE stage = ? ORDER BY id ASC",
            (stage,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def get_stage_4_package_count(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(package_id, id)) as cnt FROM stage_messages WHERE stage = 4"
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()

def clear_stage_messages(db_path: str, stage: int) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute("DELETE FROM stage_messages WHERE stage = ?", (stage,))
            return cur.rowcount
    finally:
        conn.close()

def get_stage_packages(db_path: str, stage: int = 4) -> List[List[Dict[str, Any]]]:
    """Returns packages strictly ordered by their first message's insertion order (id ASC), with messages inside each package also ordered by id ASC."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, package_id, file_unique_id, created_at "
            "FROM stage_messages WHERE stage = ? ORDER BY id ASC",
            (stage,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        packages: List[List[Dict[str, Any]]] = []
        pkg_map: Dict[Any, List[Dict[str, Any]]] = {}
        order: List[Any] = []
        for r in rows:
            pid = r.get("package_id") or r["id"]
            if pid not in pkg_map:
                pkg_map[pid] = []
                order.append(pid)
            pkg_map[pid].append(r)
        for pid in order:
            packages.append(pkg_map[pid])
        return packages
    finally:
        conn.close()

def list_students(db_path: str) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, last_drip_sent_at, day_8_sent, day_30_sent FROM students ORDER BY registered_at DESC"
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

# --- Generic & Stage 4 Delivered Message Tracking (Strict Duplicate Prevention) ---

def record_message_delivery(
    db_path: str,
    user_id: int,
    stage: int,
    message_record_id: int,
    from_chat_id: Optional[int] = None,
    source_message_id: Optional[int] = None
) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            if (from_chat_id is None or source_message_id is None) and message_record_id > 0:
                row = conn.execute(
                    "SELECT from_chat_id, message_id FROM stage_messages WHERE id = ?",
                    (message_record_id,)
                ).fetchone()
                if row:
                    from_chat_id = row["from_chat_id"]
                    source_message_id = row["message_id"]

            conn.execute(
                "INSERT INTO delivered_messages (user_id, stage, message_record_id, from_chat_id, source_message_id, delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, message_record_id) DO UPDATE SET "
                "from_chat_id = COALESCE(excluded.from_chat_id, delivered_messages.from_chat_id), "
                "source_message_id = COALESCE(excluded.source_message_id, delivered_messages.source_message_id)",
                (user_id, stage, message_record_id, from_chat_id, source_message_id, get_now_utc())
            )
    finally:
        conn.close()

def is_message_delivered(
    db_path: str,
    user_id: int,
    message_record_id: int,
    from_chat_id: Optional[int] = None,
    source_message_id: Optional[int] = None
) -> bool:
    conn = get_connection(db_path)
    try:
        if message_record_id is not None and message_record_id > 0:
            cur = conn.execute(
                "SELECT 1 FROM delivered_messages WHERE user_id = ? AND message_record_id = ? "
                "UNION ALL "
                "SELECT 1 FROM delivered_drip_messages WHERE user_id = ? AND message_record_id = ? "
                "LIMIT 1",
                (user_id, message_record_id, user_id, message_record_id)
            )
            if cur.fetchone() is not None:
                return True

            if from_chat_id is None or source_message_id is None:
                sm_row = conn.execute(
                    "SELECT from_chat_id, message_id FROM stage_messages WHERE id = ?",
                    (message_record_id,)
                ).fetchone()
                if sm_row:
                    from_chat_id = sm_row["from_chat_id"]
                    source_message_id = sm_row["message_id"]

        if from_chat_id is not None and source_message_id is not None:
            cur2 = conn.execute(
                "SELECT 1 FROM delivered_messages WHERE user_id = ? AND from_chat_id = ? AND source_message_id = ? "
                "UNION ALL "
                "SELECT 1 FROM delivered_drip_messages WHERE user_id = ? AND from_chat_id = ? AND source_message_id = ? "
                "LIMIT 1",
                (user_id, from_chat_id, source_message_id, user_id, from_chat_id, source_message_id)
            )
            if cur2.fetchone() is not None:
                return True

        return False
    finally:
        conn.close()

def get_next_unsent_drip_package(db_path: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """Returns the next unsent Stage 4 package in strict creation order (id ASC), filtering out any already-delivered files."""
    packages = get_stage_packages(db_path, stage=4)
    for pkg in packages:
        unsent_msgs = [
            m for m in pkg
            if not is_message_delivered(
                db_path,
                user_id,
                m["id"],
                m.get("from_chat_id"),
                m.get("message_id")
            )
        ]
        if unsent_msgs:
            return unsent_msgs
    return None

def get_next_unsent_drip_message(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    pkg = get_next_unsent_drip_package(db_path, user_id)
    return pkg[0] if pkg else None

def record_drip_package_delivery(db_path: str, user_id: int, package_id: int, message_ids: List[int]) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            now = get_now_utc()
            for msg_id in message_ids:
                row = conn.execute(
                    "SELECT from_chat_id, message_id FROM stage_messages WHERE id = ?",
                    (msg_id,)
                ).fetchone()
                f_chat = row["from_chat_id"] if row else None
                s_msg = row["message_id"] if row else None

                conn.execute(
                    "INSERT INTO delivered_drip_messages (user_id, message_record_id, package_id, from_chat_id, source_message_id, delivered_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, message_record_id) DO UPDATE SET "
                    "package_id = excluded.package_id, "
                    "from_chat_id = COALESCE(excluded.from_chat_id, delivered_drip_messages.from_chat_id), "
                    "source_message_id = COALESCE(excluded.source_message_id, delivered_drip_messages.source_message_id)",
                    (user_id, msg_id, package_id, f_chat, s_msg, now)
                )
                conn.execute(
                    "INSERT INTO delivered_messages (user_id, stage, message_record_id, from_chat_id, source_message_id, delivered_at) "
                    "VALUES (?, 4, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, message_record_id) DO UPDATE SET "
                    "from_chat_id = COALESCE(excluded.from_chat_id, delivered_messages.from_chat_id), "
                    "source_message_id = COALESCE(excluded.source_message_id, delivered_messages.source_message_id)",
                    (user_id, msg_id, f_chat, s_msg, now)
                )
            conn.execute(
                "UPDATE students SET last_drip_sent_at = ? WHERE user_id = ?",
                (now, user_id)
            )
    finally:
        conn.close()

def record_drip_delivery(db_path: str, user_id: int, message_record_id: int) -> None:
    record_drip_package_delivery(db_path, user_id, message_record_id, [message_record_id])

def get_eligible_students_for_drip(db_path: str, force_ignore_interval: bool = False) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    conn = get_connection(db_path)
    try:
        interval_days = int(get_setting(db_path, "drip_interval_days", "2"))
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name, current_stage, last_drip_sent_at FROM students "
            "WHERE current_stage >= 4 "
            "ORDER BY registered_at ASC, user_id ASC"
        )
        candidates = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    eligible = []
    now = get_now_utc()

    for st in candidates:
        next_pkg = get_next_unsent_drip_package(db_path, st["user_id"])
        if not next_pkg:
            continue

        if force_ignore_interval:
            eligible.append((st, next_pkg))
            continue

        last_sent_str = st.get("last_drip_sent_at")
        if not last_sent_str:
            eligible.append((st, next_pkg))
        else:
            last_sent = parse_db_datetime(last_sent_str)
            if not last_sent:
                eligible.append((st, next_pkg))
            else:
                diff_days = (now - last_sent).total_seconds() / 86400.0
                if diff_days >= interval_days:
                    eligible.append((st, next_pkg))
    return eligible

# --- Study Plans (Periodic 7-day, 8-day, 9-day schedules) ---

def create_study_plan(db_path: str, user_id: int, duration_days: int) -> int:
    duration_days = max(1, min(365, int(duration_days)))
    conn = get_connection(db_path)
    try:
        with conn:
            # Mark previous active plans as superseded
            conn.execute("UPDATE study_plans SET status = 'SUPERSEDED' WHERE user_id = ? AND status IN ('ACTIVE', 'REMINDER_SENT')", (user_id,))

            now = get_now_utc()
            end_date = now + timedelta(days=duration_days)
            # Reminder is 1 day before expiration (or now if 1-day plan)
            reminder_date = end_date - timedelta(days=1)
            if reminder_date < now:
                reminder_date = now

            cur = conn.execute(
                "INSERT INTO study_plans (user_id, duration_days, start_date, end_date, reminder_date, reminder_sent, report_submitted, status) "
                "VALUES (?, ?, ?, ?, ?, 0, 0, 'ACTIVE')",
                (user_id, duration_days, now, end_date, reminder_date)
            )
            return cur.lastrowid
    finally:
        conn.close()

def get_active_study_plan(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, user_id, duration_days, start_date, end_date, reminder_date, reminder_sent, report_submitted, report_text, status "
            "FROM study_plans WHERE user_id = ? AND status IN ('ACTIVE', 'REMINDER_SENT') ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_study_plans_due_for_reminder(db_path: str) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        now = get_now_utc()
        cur = conn.execute(
            "SELECT sp.id, sp.user_id, sp.duration_days, sp.start_date, sp.end_date, sp.reminder_date, "
            "s.full_name, s.assigned_name, s.username, s.current_stage "
            "FROM study_plans sp "
            "JOIN students s ON sp.user_id = s.user_id "
            "WHERE sp.status = 'ACTIVE' AND sp.reminder_sent = 0 AND sp.reminder_date <= ?",
            (now,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def mark_study_plan_reminder_sent(db_path: str, plan_id: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE study_plans SET reminder_sent = 1, status = 'REMINDER_SENT' WHERE id = ?",
                (plan_id,)
            )
    finally:
        conn.close()

def submit_study_plan_report(db_path: str, user_id: int, report_text: str) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "SELECT id, duration_days FROM study_plans WHERE user_id = ? AND status IN ('ACTIVE', 'REMINDER_SENT') ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            plan = cur.fetchone()
            if not plan:
                return None

            plan_id = plan["id"]
            conn.execute(
                "UPDATE study_plans SET report_submitted = 1, report_text = ?, status = 'REPORT_SUBMITTED' WHERE id = ?",
                (report_text, plan_id)
            )
            return {"plan_id": plan_id, "duration_days": plan["duration_days"]}
    finally:
        conn.close()

def create_database_backup(db_path: str, backup_path: Optional[str] = None) -> str:
    dir_name = os.path.dirname(db_path) or "."
    base_name = os.path.basename(db_path)
    if not backup_path:
        backup_path = os.path.join(dir_name, f"backup_{base_name}")
    if os.path.exists(backup_path) and os.path.abspath(backup_path) != os.path.abspath(db_path):
        try:
            os.remove(backup_path)
        except Exception:
            pass

    src = get_connection(db_path)
    dst = None
    try:
        try:
            src.execute("PRAGMA wal_checkpoint(FULL);")
        except Exception:
            pass
        dst = sqlite3.connect(backup_path, timeout=30.0)
        with dst:
            src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE;")
        dst.commit()
    finally:
        if dst:
            dst.close()
        src.close()
    return backup_path

def restore_database_backup(backup_file_path: str, target_db_path: str) -> bool:
    if not os.path.exists(backup_file_path) or os.path.getsize(backup_file_path) == 0:
        return False
    src = None
    dst = None
    try:
        with open(backup_file_path, "rb") as f:
            header = f.read(16)

        if header.startswith(b"SQLite format 3"):
            src = sqlite3.connect(backup_file_path, timeout=30.0)
            src.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            dst = sqlite3.connect(target_db_path, timeout=30.0)
            try:
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            with dst:
                src.backup(dst)
            dst.commit()
            dst.close()
            dst = None
            src.close()
            src = None
            init_db(target_db_path)
            return True
        else:
            with open(backup_file_path, "r", encoding="utf-8", errors="ignore") as f:
                sql_script = f.read()
            if "CREATE TABLE" in sql_script.upper() or "INSERT INTO" in sql_script.upper():
                dst = get_connection(target_db_path)
                with dst:
                    dst.executescript(sql_script)
                dst.close()
                dst = None
                init_db(target_db_path)
                return True
            return False
    except Exception:
        return False
    finally:
        if dst:
            dst.close()
        if src:
            src.close()

# --- Intro / Rules (Red Lines) Content ---

def set_intro_media(db_path: str, from_chat_id: int, message_id: int, caption: Optional[str] = None) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO intro_contents (id, from_chat_id, message_id, caption, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "from_chat_id = excluded.from_chat_id, "
                "message_id = excluded.message_id, "
                "caption = excluded.caption, "
                "updated_at = excluded.updated_at",
                (from_chat_id, message_id, caption, get_now_utc())
            )
    finally:
        conn.close()

def set_intro_rules(db_path: str, text_content: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO intro_contents (id, text_content, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "text_content = excluded.text_content, "
                "updated_at = excluded.updated_at",
                (text_content, get_now_utc())
            )
    finally:
        conn.close()

def clear_intro_media(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE intro_contents SET from_chat_id = NULL, message_id = NULL, caption = NULL WHERE id = 1"
            )
    finally:
        conn.close()

def clear_intro_rules(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE intro_contents SET text_content = NULL WHERE id = 1"
            )
    finally:
        conn.close()

def set_intro_content(db_path: str, from_chat_id: Optional[int] = None, message_id: Optional[int] = None, caption: Optional[str] = None, text_content: Optional[str] = None) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO intro_contents (id, from_chat_id, message_id, caption, text_content, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "from_chat_id = excluded.from_chat_id, "
                "message_id = excluded.message_id, "
                "caption = excluded.caption, "
                "text_content = excluded.text_content, "
                "updated_at = excluded.updated_at",
                (from_chat_id, message_id, caption, text_content, get_now_utc())
            )
    finally:
        conn.close()

def get_intro_content(db_path: str) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT id, from_chat_id, message_id, caption, text_content, updated_at FROM intro_contents WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def clear_intro_content(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM intro_contents WHERE id = 1")
    finally:
        conn.close()

# --- Student Dossier / Memory (Forwarded Notes & Files) ---

def add_student_dossier_note(
    db_path: str,
    user_id: int,
    from_chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    media_type: str = "text",
    caption: Optional[str] = None,
    text_content: Optional[str] = None
) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO student_dossier (user_id, from_chat_id, message_id, media_type, caption, text_content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, from_chat_id, message_id, media_type, caption, text_content, get_now_utc())
            )
            return cur.lastrowid
    finally:
        conn.close()

def get_student_dossier(db_path: str, user_id: int) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, user_id, from_chat_id, message_id, media_type, caption, text_content, created_at "
            "FROM student_dossier WHERE user_id = ? ORDER BY id ASC",
            (user_id,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def get_student_dossier_count(db_path: str, user_id: int) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) as cnt FROM student_dossier WHERE user_id = ?",
            (user_id,)
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()

def delete_dossier_note(db_path: str, note_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute("DELETE FROM student_dossier WHERE id = ?", (note_id,))
            return cur.rowcount > 0
    finally:
        conn.close()

def delete_latest_dossier_note(db_path: str, user_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM student_dossier WHERE id = ("
                "  SELECT id FROM student_dossier WHERE user_id = ? ORDER BY id DESC LIMIT 1"
                ")",
                (user_id,)
            )
            return cur.rowcount > 0
    finally:
        conn.close()

def clear_student_dossier(db_path: str, user_id: int) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute("DELETE FROM student_dossier WHERE user_id = ?", (user_id,))
            return cur.rowcount
    finally:
        conn.close()

def _compute_name_similarity(query_norm: str, target_norm: str) -> Tuple[float, str]:
    if not query_norm or not target_norm:
        return 0.0, "none"
    if query_norm == target_norm:
        return 1.0, "exact"

    # Substring or prefix match
    if query_norm in target_norm or target_norm in query_norm:
        ratio = min(len(query_norm), len(target_norm)) / max(len(query_norm), len(target_norm), 1)
        return 0.85 + 0.14 * ratio, "substring"

    # Token-level prefix/substring/fuzzy match
    q_tokens = query_norm.split()
    t_tokens = target_norm.split()
    best_tok_score = 0.0
    for qt in q_tokens:
        for tt in t_tokens:
            if qt == tt:
                best_tok_score = max(best_tok_score, 0.92)
            elif qt in tt or tt in qt:
                r = min(len(qt), len(tt)) / max(len(qt), len(tt), 1)
                best_tok_score = max(best_tok_score, 0.80 + 0.10 * r)
            else:
                sim = SequenceMatcher(None, qt, tt).ratio()
                best_tok_score = max(best_tok_score, sim * 0.85)

    full_sim = SequenceMatcher(None, query_norm, target_norm).ratio()
    score = max(full_sim, best_tok_score)
    match_type = "substring" if score >= 0.80 else "fuzzy"
    return score, match_type

def find_closest_students_by_name(db_path: str, query_name: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Finds students matching a full, partial, or fuzzy name query, ordered from closest to furthest."""
    q = normalize_persian_text(query_name)
    if not q:
        return []

    students = list_students(db_path)
    if not students:
        return []

    scored: List[Dict[str, Any]] = []
    for s in students:
        an = normalize_persian_text(s.get("assigned_name"))
        fn = normalize_persian_text(s.get("full_name"))
        un = normalize_persian_text((s.get("username") or "").lstrip("@"))

        best_score = 0.0
        best_type = "none"
        for candidate in (an, fn, un):
            if not candidate:
                continue
            sc, mtype = _compute_name_similarity(q, candidate)
            if sc > best_score:
                best_score = sc
                best_type = mtype

        item = dict(s)
        item["match_score"] = round(best_score, 4)
        item["match_type"] = best_type
        scored.append(item)

    scored.sort(key=lambda x: x["match_score"], reverse=True)

    exact = [x for x in scored if x["match_type"] == "exact"]
    if exact:
        return exact

    substring = [x for x in scored if x["match_type"] == "substring"]
    if substring:
        return substring[:limit]

    fuzzy = [x for x in scored if x["match_score"] >= 0.40]
    if fuzzy:
        return fuzzy[:limit]

    # Fallback: return top closest candidates so admin can still pick
    return scored[: min(3, len(scored))]

def find_students_by_name(db_path: str, query_name: str) -> List[Dict[str, Any]]:
    if not query_name:
        return []
    q = normalize_persian_text(query_name)
    if not q:
        return []
    students = list_students(db_path)
    matched = []
    for s in students:
        an = normalize_persian_text(s.get("assigned_name"))
        fn = normalize_persian_text(s.get("full_name"))
        un = normalize_persian_text((s.get("username") or "").lstrip("@"))
        if q == an or q == fn or (un and q == un):
            matched.append(s)
        elif (an and (an in q or q in an)) or (fn and (fn in q or q in fn)):
            if s not in matched:
                matched.append(s)
    return matched

# --- Section 4 Specific Package Queries ---

def get_stage_4_packages_summary(db_path: str) -> List[Dict[str, Any]]:
    packages = get_stage_packages(db_path, stage=4)
    summary = []
    for idx, pkg in enumerate(packages, 1):
        pid = pkg[0].get("package_id") or pkg[0]["id"]
        summary.append({
            "index": idx,
            "package_id": pid,
            "count": len(pkg),
            "is_package": len(pkg) > 1,
            "caption": pkg[0].get("caption") or "",
            "messages": pkg
        })
    return summary

def get_students_missing_stage_4_package(db_path: str, package_id: int) -> List[Dict[str, Any]]:
    packages = get_stage_4_packages_summary(db_path)
    target_pkg = next((p for p in packages if p["package_id"] == package_id), None)
    if not target_pkg:
        return []

    pkg_msgs = target_pkg["messages"]
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name FROM students "
            "WHERE current_stage >= 4 ORDER BY registered_at ASC, user_id ASC"
        )
        candidates = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    missing = []
    for st in candidates:
        uid = st["user_id"]
        has_unsent = any(
            not is_message_delivered(db_path, uid, m["id"], m.get("from_chat_id"), m.get("message_id"))
            for m in pkg_msgs
        )
        if has_unsent:
            missing.append(st)
    return missing

def split_stage_4_messages_to_solo(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute("UPDATE stage_messages SET package_id = id WHERE stage = 4")
            count = cur.rowcount
            conn.execute(
                "UPDATE delivered_drip_messages SET package_id = message_record_id "
                "WHERE message_record_id IN (SELECT id FROM stage_messages WHERE stage = 4)"
            )
            return count
    finally:
        conn.close()
