import os
import sqlite3
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

def get_connection(db_path: str) -> sqlite3.Connection:
    dir_name = os.path.dirname(db_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
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
                "day_30_sent INTEGER DEFAULT 0"
                ");"
                "CREATE TABLE IF NOT EXISTS stage_messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "stage INTEGER NOT NULL,"
                "from_chat_id INTEGER NOT NULL,"
                "message_id INTEGER NOT NULL,"
                "caption TEXT NULL,"
                "package_id INTEGER NULL,"
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
                "package_id INTEGER NULL,"
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
                "delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "UNIQUE(user_id, message_record_id)"
                ");"
                "CREATE INDEX IF NOT EXISTS idx_stage ON stage_messages(stage);"
                "CREATE INDEX IF NOT EXISTS idx_stage_pkg ON stage_messages(stage, package_id);"
                "CREATE INDEX IF NOT EXISTS idx_delivered_package ON delivered_drip_messages(user_id, package_id);"
                "CREATE INDEX IF NOT EXISTS idx_deliv_stage_msg ON delivered_messages(user_id, stage, message_record_id);"
                "CREATE INDEX IF NOT EXISTS idx_study_plans_user ON study_plans(user_id);"
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
                "stage_warning_sent INTEGER DEFAULT 0"
            ]:
                try:
                    conn.execute(f"ALTER TABLE students ADD COLUMN {col_def};")
                except sqlite3.OperationalError:
                    pass

            for col_def, tbl in [
                ("package_id INTEGER NULL", "stage_messages"),
                ("package_id INTEGER NULL", "delivered_drip_messages")
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
    try:
        with conn:
            conn.execute(
                "INSERT INTO invites (code, assigned_name, created_at, is_used) VALUES (?, ?, ?, 0)",
                (code, assigned_name, get_now_utc())
            )
    finally:
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
            now = get_now_utc()
            conn.execute(
                "UPDATE invites SET is_used = 1, used_by = ?, used_at = ? WHERE code = ?",
                (user_id, now, code)
            )

            conn.execute(
                "INSERT INTO students (user_id, username, full_name, assigned_name, current_stage, registered_at, last_active, stage_updated_at, stage_warning_sent, day_8_sent, day_30_sent) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0, 0, 0) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username = excluded.username, "
                "full_name = excluded.full_name, "
                "assigned_name = COALESCE(excluded.assigned_name, students.assigned_name), "
                "last_active = excluded.last_active",
                (user_id, username, full_name, assigned_name, now, now, now)
            )
        return True, assigned_name
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

def add_stage_message(db_path: str, stage: int, from_chat_id: int, message_id: int, caption: Optional[str] = None, package_id: Optional[int] = None) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO stage_messages (stage, from_chat_id, message_id, caption, package_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stage, from_chat_id, message_id, caption, package_id, get_now_utc())
            )
            inserted_id = cur.lastrowid
            if package_id is None:
                conn.execute("UPDATE stage_messages SET package_id = ? WHERE id = ?", (inserted_id, inserted_id))
            return inserted_id
    finally:
        conn.close()

def get_stage_messages(db_path: str, stage: int) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, package_id, created_at FROM stage_messages WHERE stage = ? ORDER BY id ASC",
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
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, package_id, created_at "
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

def get_next_unsent_drip_package(db_path: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT DISTINCT COALESCE(package_id, id) AS pkg_id "
            "FROM stage_messages sm "
            "WHERE sm.stage = 4 "
            "  AND NOT EXISTS ( "
            "    SELECT 1 FROM delivered_drip_messages ddm "
            "    WHERE ddm.user_id = ? AND COALESCE(ddm.package_id, ddm.message_record_id) = COALESCE(sm.package_id, sm.id) "
            "  ) "
            "ORDER BY pkg_id ASC "
            "LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            return None

        pkg_id = row["pkg_id"]
        cur_items = conn.execute(
            "SELECT id, stage, from_chat_id, message_id, caption, package_id, created_at "
            "FROM stage_messages "
            "WHERE stage = 4 AND COALESCE(package_id, id) = ? "
            "ORDER BY id ASC",
            (pkg_id,)
        )
        msgs = [dict(r) for r in cur_items.fetchall()]
        return msgs if msgs else None
    finally:
        conn.close()

def get_next_unsent_drip_message(db_path: str, user_id: int) -> Optional[Dict[str, Any]]:
    pkg = get_next_unsent_drip_package(db_path, user_id)
    return pkg[0] if pkg else None

def record_drip_package_delivery(db_path: str, user_id: int, package_id: int, message_ids: List[int]) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            now = get_now_utc()
            for msg_id in message_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO delivered_drip_messages (user_id, message_record_id, package_id, delivered_at) VALUES (?, ?, ?, ?)",
                    (user_id, msg_id, package_id, now)
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
            "WHERE current_stage >= 4"
        )
        candidates = [dict(row) for row in cur.fetchall()]
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
    finally:
        conn.close()

# --- Study Plans (Periodic 7-day, 8-day, 9-day schedules) ---

def create_study_plan(db_path: str, user_id: int, duration_days: int) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            # Mark previous active plans as superseded
            conn.execute("UPDATE study_plans SET status = 'SUPERSEDED' WHERE user_id = ? AND status = 'ACTIVE'", (user_id,))

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
            "s.full_name, s.assigned_name, s.username "
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
        # Check if file is a valid SQLite database or an SQL script dump
        with open(backup_file_path, "rb") as f:
            header = f.read(16)

        if header.startswith(b"SQLite format 3"):
            src = sqlite3.connect(backup_file_path, timeout=30.0)
            # Verify integrity and tables
            src.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            dst = sqlite3.connect(target_db_path, timeout=30.0)
            try:
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            with dst:
                src.backup(dst)
            dst.commit()
            return True
        else:
            # Try restoring from SQL text dump if user uploaded a .sql backup
            with open(backup_file_path, "r", encoding="utf-8", errors="ignore") as f:
                sql_script = f.read()
            if "CREATE TABLE" in sql_script.upper() or "INSERT INTO" in sql_script.upper():
                dst = get_connection(target_db_path)
                with dst:
                    dst.executescript(sql_script)
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

def find_students_by_name(db_path: str, query_name: str) -> List[Dict[str, Any]]:
    if not query_name:
        return []
    conn = get_connection(db_path)
    try:
        q = query_name.strip().lower()
        students = list_students(db_path)
        matched = []
        for s in students:
            an = (s.get("assigned_name") or "").lower()
            fn = (s.get("full_name") or "").lower()
            un = (s.get("username") or "").lower()
            if q == an or q == fn or (un and q == un):
                matched.append(s)
            elif (an and an in q) or (fn and fn in q) or (q in an) or (q in fn):
                if s not in matched:
                    matched.append(s)
        return matched
    finally:
        conn.close()

# --- Generic Stage Delivered Message Tracking (Duplicate Prevention) ---

def record_message_delivery(db_path: str, user_id: int, stage: int, message_record_id: int) -> None:
    conn = get_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO delivered_messages (user_id, stage, message_record_id, delivered_at) VALUES (?, ?, ?, ?)",
                (user_id, stage, message_record_id, get_now_utc())
            )
    finally:
        conn.close()

def is_message_delivered(db_path: str, user_id: int, message_record_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM delivered_messages WHERE user_id = ? AND message_record_id = ? LIMIT 1",
            (user_id, message_record_id)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()

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
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT user_id, username, full_name, assigned_name FROM students "
            "WHERE current_stage >= 4 "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM delivered_drip_messages "
            "    WHERE user_id = students.user_id AND package_id = ?"
            "  )",
            (package_id,)
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def split_stage_4_messages_to_solo(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            cur = conn.execute("UPDATE stage_messages SET package_id = id WHERE stage = 4")
            return cur.rowcount
    finally:
        conn.close()


