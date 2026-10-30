import os
import re
import sys
import html
import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import config
import database
import motivational_quotes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# Silence httpx/httpcore INFO logs so BOT_TOKEN is never printed in GitHub Actions logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Persistent SQLite-Backed State Dictionaries ---
# Ensures admin & student states survive process restarts, hot-reloads, and multi-worker setups.

class PersistentStateDict(dict):
    def get(self, key: int, default: Any = None) -> Optional[str]:
        try:
            db_val = database.get_user_state(config.DB_PATH, int(key))
            if db_val is not None:
                super().__setitem__(key, db_val)
                return db_val
        except Exception:
            pass
        return super().get(key, default)

    def __getitem__(self, key: int) -> Optional[str]:
        val = self.get(key, None)
        if val is None and not super().__contains__(key):
            raise KeyError(key)
        return val

    def __setitem__(self, key: int, value: Optional[str]) -> None:
        super().__setitem__(key, value)
        try:
            database.set_user_state(config.DB_PATH, int(key), value)
        except Exception:
            pass

    def pop(self, key: int, *args) -> Any:
        res = super().pop(key, *args)
        try:
            database.set_user_state(config.DB_PATH, int(key), None)
        except Exception:
            pass
        return res


class PersistentPackageDict(dict):
    def get(self, key: int, default: Any = None) -> Optional[int]:
        try:
            db_val = database.get_user_package_id(config.DB_PATH, int(key))
            if db_val is not None:
                super().__setitem__(key, db_val)
                return db_val
        except Exception:
            pass
        return super().get(key, default)

    def __setitem__(self, key: int, value: Optional[int]) -> None:
        super().__setitem__(key, value)
        try:
            database.set_user_package_id(config.DB_PATH, int(key), value)
        except Exception:
            pass

    def pop(self, key: int, *args) -> Any:
        val = self.get(key, args[0] if args else None)
        super().pop(key, None)
        try:
            database.set_user_package_id(config.DB_PATH, int(key), None)
        except Exception:
            pass
        return val


class PersistentBroadcastDict(dict):
    def get(self, key: int, default: Any = None) -> Optional[Dict[str, Any]]:
        try:
            db_val = database.get_pending_broadcast(config.DB_PATH, int(key))
            if db_val is not None:
                super().__setitem__(key, db_val)
                return db_val
        except Exception:
            pass
        return super().get(key, default)

    def __setitem__(self, key: int, value: Optional[Dict[str, Any]]) -> None:
        super().__setitem__(key, value)
        try:
            database.set_pending_broadcast(config.DB_PATH, int(key), value)
        except Exception:
            pass

    def pop(self, key: int, *args) -> Any:
        val = self.get(key, args[0] if args else None)
        super().pop(key, None)
        try:
            database.set_pending_broadcast(config.DB_PATH, int(key), None)
        except Exception:
            pass
        return val


class PersistentDossierDict(dict):
    def get(self, key: int, default: Any = None) -> Optional[Dict[str, Any]]:
        try:
            db_val = database.get_pending_dossier_note(config.DB_PATH, int(key))
            if db_val is not None:
                super().__setitem__(key, db_val)
                return db_val
        except Exception:
            pass
        return super().get(key, default)

    def __setitem__(self, key: int, value: Optional[Dict[str, Any]]) -> None:
        super().__setitem__(key, value)
        try:
            database.set_pending_dossier_note(config.DB_PATH, int(key), value)
        except Exception:
            pass

    def pop(self, key: int, *args) -> Any:
        val = self.get(key, args[0] if args else None)
        super().pop(key, None)
        try:
            database.set_pending_dossier_note(config.DB_PATH, int(key), None)
        except Exception:
            pass
        return val


# Active states:
# "AWAITING_STUDENT_NAME", "AWAITING_DELETE_STUDENT_NAME", "RECORD_STAGE_...",
# "AWAITING_CUSTOM_DAYS_<user_id>", "AWAITING_STUDY_REPORT",
# "AWAITING_INTRO_MEDIA", "AWAITING_INTRO_RULES",
# "AWAITING_BROADCAST_CONTENT", "AWAITING_MANUAL_DOSSIER_<user_id>"
active_states: Dict[int, Optional[str]] = PersistentStateDict()
active_package_ids: Dict[int, int] = PersistentPackageDict()
pending_broadcast: Dict[int, Dict[str, Any]] = PersistentBroadcastDict()
pending_dossier_notes: Dict[int, Dict[str, Any]] = PersistentDossierDict()

# Concurrency locks to guarantee zero duplicate deliveries and strict ordering
user_delivery_locks: Dict[int, asyncio.Lock] = {}
drip_cycle_lock = asyncio.Lock()
scheduler_cycle_lock = asyncio.Lock()
_CACHED_BOT_USERNAME: Optional[str] = None
_CONFLICT_COUNT: int = 0

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_delivery_locks:
        user_delivery_locks[user_id] = asyncio.Lock()
    return user_delivery_locks[user_id]

try:
    from telegram import (
        Update,
        InlineKeyboardButton as _TGInlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyKeyboardMarkup,
        ReplyKeyboardRemove,
        KeyboardButton,
    )
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        ContextTypes,
        filters,
    )

    def InlineKeyboardButton(text, callback_data=None, style=None, **kwargs):
        """Compatibility wrapper that passes style safely across all python-telegram-bot versions."""
        if style is not None:
            try:
                return _TGInlineKeyboardButton(text, callback_data=callback_data, style=style, **kwargs)
            except TypeError:
                return _TGInlineKeyboardButton(text, callback_data=callback_data, **kwargs)
        return _TGInlineKeyboardButton(text, callback_data=callback_data, **kwargs)

except ImportError:
    Update = None
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None, **kwargs):
            self.text = text
            self.callback_data = callback_data
    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard, **kwargs):
            self.inline_keyboard = inline_keyboard
    class KeyboardButton:
        def __init__(self, text, **kwargs):
            self.text = text
    class ReplyKeyboardMarkup:
        def __init__(self, keyboard, **kwargs):
            self.keyboard = keyboard
    class ReplyKeyboardRemove:
        def __init__(self, **kwargs):
            self.remove_keyboard = True
    Application = None
    CommandHandler = None
    CallbackQueryHandler = None
    MessageHandler = None
    ContextTypes = None
    filters = None

IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

def get_iran_now():
    return datetime.now(IRAN_TZ)

def escape_md(text: Optional[str]) -> str:
    """Escapes Telegram Markdown (V1) special characters: _, *, `, ["""
    if text is None:
        return ""
    text_str = str(text)
    for ch in ("_", "*", "`", "["):
        text_str = text_str.replace(ch, f"\\{ch}")
    return text_str

def extract_media_unique_id(msg) -> Optional[str]:
    """Extracts Telegram's permanent file_unique_id from any media message to prevent duplicate saves."""
    if not msg:
        return None
    for attr in ("voice", "audio", "video", "video_note", "document", "animation"):
        media_obj = getattr(msg, attr, None)
        if media_obj and getattr(media_obj, "file_unique_id", None):
            return str(media_obj.file_unique_id)
    photos = getattr(msg, "photo", None)
    if photos and len(photos) > 0:
        best_photo = photos[-1]
        if getattr(best_photo, "file_unique_id", None):
            return str(best_photo.file_unique_id)
    return None

async def safe_reply_text(message_obj, text: str, **kwargs):
    """Replies with text; if Markdown parsing fails due to unescaped user characters, automatically falls back to plain text."""
    try:
        return await message_obj.reply_text(text, **kwargs)
    except Exception as e:
        if kwargs.get("parse_mode") and ("can't parse entities" in str(e).lower() or "bad request" in str(e).lower()):
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("parse_mode", None)
            return await message_obj.reply_text(text, **fallback_kwargs)
        raise

async def safe_bot_send_message(bot, chat_id: int, text: str, **kwargs):
    """Sends a message via bot; if Markdown parsing fails, automatically falls back to plain text."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        if kwargs.get("parse_mode") and ("can't parse entities" in str(e).lower() or "bad request" in str(e).lower()):
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("parse_mode", None)
            return await bot.send_message(chat_id=chat_id, text=text, **fallback_kwargs)
        raise

def is_admin(user_id: int, username: Optional[str]) -> bool:
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        return True
    if username and config.ADMIN_USERNAME:
        if username.lower().lstrip("@") == config.ADMIN_USERNAME.lower().lstrip("@"):
            return True
    return False

def format_invite_link_message(student_name: str, invite_link: str) -> str:
    """Formats the student-facing invite message ready to be forwarded directly to the student."""
    safe_html_name = html.escape(student_name.strip() or "دانش‌آموز")
    safe_link = html.escape(invite_link)
    return (
        f"سلام {safe_html_name} عزیز 🌸\n"
        f"🔗 <b>این لینک ورود اختصاصی تقدیم شما:</b>\n\n"
        f"{safe_link}\n\n"
        "📌 <b>مراحل شروع کار:</b>\n"
        "۱. وقتی وارد شدی، اول <b>خط قرمزها</b> رو کامل بخون و <b>ویس اولیه</b> رو گوش بده.\n"
        "۲. بعد وارد <b>مرحله اول</b> شو و <b>ویس‌های مرحله اول</b> رو با دقت گوش بده.\n"
        "۳. وقتی ویس‌های مرحله اول رو کامل گوش دادی، <b>به هاشمی اطلاع بده</b>."
    )

async def resolve_bot_username(bot) -> str:
    global _CACHED_BOT_USERNAME
    if _CACHED_BOT_USERNAME:
        return _CACHED_BOT_USERNAME
    uname = getattr(bot, "username", None)
    if uname:
        _CACHED_BOT_USERNAME = uname
        return uname
    bot_info = await bot.get_me()
    _CACHED_BOT_USERNAME = getattr(bot_info, "username", None) or "Bot"
    return _CACHED_BOT_USERNAME

async def create_and_send_invite_link(msg, bot, admin_id: int, raw_student_name: str):
    """Creates a fresh invite link for `raw_student_name` and sends the formatted message to the admin."""
    cleaned_name = (raw_student_name or "").strip()
    for prefix in ("ساخت لینک جدید برای ", "ساخت لینک برای ", "ساخت لینک ", "لینک دعوت برای ", "لینک دعوت ", "لینک برای ", "لینک "):
        if cleaned_name.startswith(prefix):
            cleaned_name = cleaned_name[len(prefix):].strip()
            break
    student_name = cleaned_name if cleaned_name else "دانش‌آموز"

    token = secrets.token_hex(4)
    code = f"inv_{token}"
    database.create_invite(config.DB_PATH, code, assigned_name=student_name)
    active_states[admin_id] = None

    bot_user = await resolve_bot_username(bot)
    invite_link = f"https://t.me/{bot_user}?start={code}"

    reply_text = format_invite_link_message(student_name, invite_link)
    await safe_reply_text(msg, reply_text, parse_mode="HTML", reply_markup=get_admin_reply_keyboard())

def parse_admin_delete_intent(text: str) -> Tuple[bool, Optional[str]]:
    """
    Detects if the admin is asking in chat to delete a student.
    Returns (is_delete_intent, extracted_student_name_or_None).
    """
    if not text:
        return False, None
    cleaned = text.strip()
    if cleaned == "🔴 🗑 حذف دانش‌آموز ❌":
        return True, None

    norm = database.normalize_persian_text(cleaned)
    if not norm:
        return False, None

    # Ignore non-student deletion phrases (e.g. deleting files, voices, stages, backups)
    non_student_keywords = ("فایل", "ویس", "محتوا", "مرحله", "پیام", "بکاپ", "پرونده", "برنامه", "قوانین", "خط قرمز")
    if any(kw in norm for kw in non_student_keywords):
        return False, None

    # Exact generic triggers without a specific name yet
    generic_phrases = {
        "حذف",
        "حذف دانش اموز",
        "حذف یه دانش اموز",
        "حذف یک دانش اموز",
        "حذف شاگرد",
        "حذف یه شاگرد",
        "حذف یک شاگرد",
        "حذف کاربر",
        "اکانت دانش اموز حذف",
        "حذف کردن دانش اموز",
        "اقا حذف یه دانش اموز",
        "اقا حذف دانش اموز",
    }
    if norm in generic_phrases:
        return True, None

    # Pattern 1: Starts with optional polite prefix + حذف + (optional: یه/یک دانش‌آموز/شاگرد/کاربر به اسم/نام) + <NAME>
    m1 = re.match(
        r"^(?:اقا\s+|لطفا\s+|بیزحمت\s+)?حذف(?:\s+کن)?(?:\s+(?:یه|یک))?(?:\s+(?:دانش\s*اموز|شاگرد|کاربر))?(?:\s+(?:به\s+)?(?:اسم|نام))?\s+(.+?)(?:\s+(?:رو|را)\s*(?:حذف\s*کن|پاک\s*کن)?)?$",
        norm
    )
    if m1:
        candidate = m1.group(1).strip()
        for suffix in (" رو حذف کن", " را حذف کن", " رو پاک کن", " را پاک کن", " حذف شه", " حذف بشه", " رو", " را"):
            if candidate.endswith(suffix):
                candidate = candidate[: -len(suffix)].strip()
        if candidate and candidate not in ("دانش اموز", "یه دانش اموز", "یک دانش اموز", "شاگرد", "کاربر", "کن"):
            return True, candidate
        return True, None

    # Pattern 2: <NAME> رو/را حذف کن / از لیست حذف کن
    m2 = re.match(
        r"^(?:اقا\s+|لطفا\s+)?(?:دانش\s*اموز\s+|شاگرد\s+|کاربر\s+)?(.+?)\s+(?:رو|را)\s+(?:از\s+لیست\s+)?حذف\s*(?:کن|بکن|بشه|شید|کنید)$",
        norm
    )
    if m2:
        candidate = m2.group(1).strip()
        if candidate and candidate not in ("دانش اموز", "یه دانش اموز", "یک دانش اموز", "شاگرد", "کاربر"):
            return True, candidate
        return True, None

    return False, None

# --- Tracked Student Message Delivery Helpers ---

async def send_student_msg(bot, user_id: int, text: str, content_type: str = "text", track: bool = True, **kwargs):
    sent = await safe_bot_send_message(bot, chat_id=user_id, text=text, **kwargs)
    if track and sent and getattr(sent, "message_id", None):
        database.record_student_sent_message(config.DB_PATH, user_id, sent.message_id, content_type=content_type)
    return sent

async def reply_student_msg(message_obj, user_id: int, text: str, content_type: str = "text", track: bool = True, **kwargs):
    sent = await safe_reply_text(message_obj, text, **kwargs)
    if track and sent and getattr(sent, "message_id", None):
        database.record_student_sent_message(config.DB_PATH, user_id, sent.message_id, content_type=content_type)
    return sent

async def copy_student_msg(bot, user_id: int, from_chat_id: int, message_id: int, content_type: str = "voice", track: bool = True, **kwargs):
    copied = await bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id, **kwargs)
    if track and copied and getattr(copied, "message_id", None):
        database.record_student_sent_message(config.DB_PATH, user_id, copied.message_id, content_type=content_type)
    return copied

# --- Persistent Bottom Reply Keyboards (With Vivid Color Badges) ---

def get_admin_reply_keyboard():
    keyboard = [
        [KeyboardButton("🟢 🔗 ساخت لینک جدید (با نام) 👤"), KeyboardButton("🔴 🗑 حذف دانش‌آموز ❌")],
        [KeyboardButton("📁 🗂 مدیریت و مشاهده محتوای مراحل 📥"), KeyboardButton("🟣 🎙 افزودن فایل به بخش ۴ 💡")],
        [KeyboardButton("📢 📨 ارسال پیام همگانی (پین‌دار) 📌"), KeyboardButton("📜 📌 پیام خوش‌آمد و خط قرمزها")],
        [KeyboardButton("🟠 🎯 محتوای روز ۸ و ۳۰ 🏆"), KeyboardButton("🟡 🗓 برنامه درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯")],
        [KeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار ⚙️"), KeyboardButton("🗂 👤 پرونده و یادداشت‌های دانش‌آموزان")],
        [KeyboardButton("📊 آمار و وضعیت دانش‌آموزان 📈"), KeyboardButton("☀️ 💬 تست جمله انگیزشی ۷ صبح")],
        [KeyboardButton("💾 دریافت بکاپ دیتابیس 📦"), KeyboardButton("👑 🏠 منوی اصلی مدیریت 👑")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_recording_reply_keyboard():
    keyboard = [
        [KeyboardButton("🟢 ✅ اتمام ذخیره پیام‌ها 🟢")],
        [KeyboardButton("🔴 🔙 انصراف و بازگشت به منو ❌")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_student_reply_keyboard(current_stage: int = 1):
    if current_stage <= 3:
        next_st = current_stage + 1
        top_btn = f"🟢 ✅ گوش دادم | رفتن به مرحله {next_st} ➡️" if next_st <= 3 else "🚀 🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
    else:
        top_btn = "📌 🌟 این صفحه را همیشه پین داشته باشید 🌟"

    keyboard = [
        [KeyboardButton(top_btn)],
        [KeyboardButton("🟣 📝 ارسال لیست مباحث خوانده‌شده 📩")],
        [KeyboardButton("💬 🩺 پیام به دکتر هاشمی 💬")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- Inline Keyboards (With Vivid Color Badges) ---

def get_admin_main_inline_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟢 🔗 ساخت لینک اختصاصی (با نام) 👤", callback_data="admin_ask_name", style="success")],
        [InlineKeyboardButton("🔴 🗑 حذف کامل دانش‌آموز ❌", callback_data="admin_delete_student_menu", style="danger")],
        [InlineKeyboardButton("🔵 📁 مدیریت و بارگذاری محتوای مراحل 📥", callback_data="admin_manage_stages", style="primary")],
        [InlineKeyboardButton("🟣 🎙 مدیریت و افزودن فایل به بخش ۴ 💡", callback_data="admin_record_4_menu", style="primary")],
        [InlineKeyboardButton("📢 📨 ارسال پیام همگانی برای همه (با پین) 📌", callback_data="admin_broadcast_menu", style="primary")],
        [InlineKeyboardButton("📜 📌 تنظیم پیام خوش‌آمد و خط قرمزها", callback_data="admin_intro_menu", style="primary")],
        [InlineKeyboardButton("🗂 👤 پرونده و اطلاعات ذخیره‌شده دانش‌آموزان", callback_data="admin_dossier_menu", style="primary")],
        [InlineKeyboardButton("🟠 🎯 تنظیم محتوای روز ۸ و ۳۰ 🏆", callback_data="admin_milestones_menu", style="primary")],
        [InlineKeyboardButton("🟡 🗓 برنامه‌ریزی درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯", callback_data="admin_plans_menu", style="primary")],
        [InlineKeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار بخش ۴ ⚙️", callback_data="admin_drip_settings", style="primary")],
        [InlineKeyboardButton("☀️ 💬 تست و مشاهده پیام انگیزشی ۷ صبح 🚀", callback_data="admin_test_quote", style="primary")],
        [InlineKeyboardButton("📊 📈 لیست و وضعیت دقیق دانش‌آموزان 📈", callback_data="admin_stats", style="success")],
        [InlineKeyboardButton("💾 📦 دریافت نسخه پشتیبان دیتابیس (Backup)", callback_data="admin_download_db", style="primary")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_delete_students_keyboard(students: list):
    keyboard = []
    row = []
    for st in students:
        uid = st["user_id"]
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        row.append(InlineKeyboardButton(f"🗑 حذف «{name}»", callback_data=f"admin_del_exec_{uid}", style="danger"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 انصراف و بازگشت به منوی اصلی", callback_data="admin_menu", style="primary")])
    return InlineKeyboardMarkup(keyboard)

def get_stage_display_name(stage_num: int) -> str:
    if stage_num == 1:
        return "مرحله ۱ 🎓"
    elif stage_num == 2:
        return "مرحله ۲ 📚"
    elif stage_num == 3:
        return "مرحله ۳ 🧠"
    elif stage_num == 4:
        return "بخش ۴ (آموزش‌های دوره‌ای) 💡"
    elif stage_num == 8:
        return "محتوای روز ۸ مشاوره 🎯"
    elif stage_num == 30:
        return "محتوای روز ۳۰ مشاوره 🏆"
    return f"مرحله {stage_num}"

def get_stages_management_keyboard():
    c1 = len(database.get_stage_messages(config.DB_PATH, 1))
    c2 = len(database.get_stage_messages(config.DB_PATH, 2))
    c3 = len(database.get_stage_messages(config.DB_PATH, 3))
    pkg4 = database.get_stage_4_package_count(config.DB_PATH)
    c4 = len(database.get_stage_messages(config.DB_PATH, 4))
    c8 = len(database.get_stage_messages(config.DB_PATH, 8))
    c30 = len(database.get_stage_messages(config.DB_PATH, 30))

    keyboard = [
        [InlineKeyboardButton(f"🟢 🎓 مرحله ۱ ({c1} فایل ذخیره)", callback_data="stage_detail_1", style="success")],
        [InlineKeyboardButton(f"🔵 📚 مرحله ۲ ({c2} فایل ذخیره)", callback_data="stage_detail_2", style="primary")],
        [InlineKeyboardButton(f"🟣 🧠 مرحله ۳ ({c3} فایل ذخیره)", callback_data="stage_detail_3", style="primary")],
        [InlineKeyboardButton(f"🟠 💡 بخش ۴ ({pkg4} بسته/نوبت | {c4} فایل)", callback_data="stage_detail_4", style="primary")],
        [
            InlineKeyboardButton(f"🎯 روز ۸ ({c8} فایل)", callback_data="stage_detail_8", style="primary"),
            InlineKeyboardButton(f"🏆 روز ۳۰ ({c30} فایل)", callback_data="stage_detail_30", style="primary"),
        ],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stages_selection_keyboard():
    return get_stages_management_keyboard()

def get_stage_action_keyboard(stage_num: int, has_content: bool = True):
    keyboard = []
    if stage_num == 4:
        keyboard.append([InlineKeyboardButton("🎙 ➕ افزودن ویس تک و مجزا", callback_data="admin_record_4_solo", style="primary")])
        keyboard.append([InlineKeyboardButton("📦 ➕ افزودن پکیج چندرسانه‌ای (ویدیو/عکس + ویس)", callback_data="admin_record_4_package", style="primary")])
        keyboard.append([InlineKeyboardButton("🎯 🚀 انتخاب و ارسال دستی یک نوبت", callback_data="stage4_manual_menu", style="success")])
        if has_content:
            keyboard.append([InlineKeyboardButton("🔄 تفکیک فایل‌های موجود به ویس‌های تکی", callback_data="stage4_split_solo", style="primary")])
            keyboard.append([InlineKeyboardButton("👁 📥 مشاهده تمام فایل‌های بخش ۴", callback_data="stage_preview_4", style="success")])
            keyboard.append([InlineKeyboardButton("🗑 ⚠️ پاکسازی کامل محتوای بخش ۴", callback_data="stage_clear_ask_4", style="danger")])
    else:
        if has_content:
            keyboard.append([InlineKeyboardButton("👁 📥 مشاهده و پیش‌نمایش فایل‌ها (ارسال در چت)", callback_data=f"stage_preview_{stage_num}", style="success")])
        keyboard.append([InlineKeyboardButton("➕ 🎙 افزودن فایل جدید به این مرحله", callback_data=f"admin_record_{stage_num}", style="primary")])
        if has_content:
            keyboard.append([InlineKeyboardButton("🗑 ⚠️ پاکسازی و حذف کامل محتوای این مرحله", callback_data=f"stage_clear_ask_{stage_num}", style="danger")])

    keyboard.append([InlineKeyboardButton("🔙 📁 بازگشت به لیست مراحل", callback_data="admin_manage_stages", style="primary")])
    return InlineKeyboardMarkup(keyboard)

def get_stage_4_options_keyboard(has_content: bool = True):
    return get_stage_action_keyboard(4, has_content=has_content)

def get_intro_management_keyboard(has_media: bool = False, has_rules: bool = False):
    keyboard = [
        [InlineKeyboardButton("👁 📥 پیش‌نمایش کامل پیام دو مرحله‌ای", callback_data="admin_preview_intro", style="success")],
        [InlineKeyboardButton("🎙 ➕ " + ("تغییر ویس/فایل معرفی" if has_media else "بارگذاری ویس/فایل معرفی"), callback_data="admin_edit_intro_media", style="primary")],
        [InlineKeyboardButton("📝 ➕ " + ("ویرایش متن خط قرمزها" if has_rules else "تنظیم متن خط قرمزها (اختصاصی)"), callback_data="admin_edit_intro_rules", style="primary")],
    ]
    del_row = []
    if has_media:
        del_row.append(InlineKeyboardButton("🗑 حذف ویس/فایل معرفی", callback_data="admin_clear_intro_media", style="danger"))
    if has_rules:
        del_row.append(InlineKeyboardButton("🔄 بازنشانی متن قوانین", callback_data="admin_reset_intro_rules", style="danger"))
    if del_row:
        keyboard.append(del_row)
    keyboard.append([InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_students_for_dossier_keyboard(students: list):
    keyboard = []
    row = []
    for st in students:
        uid = st["user_id"]
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        cnt = database.get_student_dossier_count(config.DB_PATH, uid)
        badge = f" ({cnt} پیام)" if cnt > 0 else ""
        row.append(InlineKeyboardButton(f"👤 {name}{badge}", callback_data=f"dossier_select_{uid}", style="primary"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_dossier_actions_keyboard(target_user_id: int, has_notes: bool = True):
    keyboard = []
    if has_notes:
        keyboard.append([InlineKeyboardButton("👁 📥 مشاهده تمام پیام‌ها و ویس‌های پرونده", callback_data=f"dossier_view_{target_user_id}", style="success")])
    keyboard.append([InlineKeyboardButton("➕ 📝 ثبت یادداشت متنی جدید", callback_data=f"dossier_addmanual_{target_user_id}", style="primary")])
    if has_notes:
        keyboard.append([InlineKeyboardButton("🗑 ⚠️ پاکسازی کامل پرونده این دانش‌آموز", callback_data=f"dossier_clear_{target_user_id}", style="danger")])
    keyboard.append([InlineKeyboardButton("🔙 👥 بازگشت به لیست پرونده‌ها", callback_data="admin_dossier_menu", style="primary")])
    return InlineKeyboardMarkup(keyboard)

def get_dossier_assign_keyboard(students: list):
    keyboard = []
    row = []
    for st in students:
        uid = st["user_id"]
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        row.append(InlineKeyboardButton(f"👤 {name}", callback_data=f"dossier_assign_{uid}", style="primary"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ انصراف و عدم ذخیره", callback_data="dossier_assign_cancel", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_broadcast_confirm_keyboard():
    keyboard = [
        [InlineKeyboardButton("📌 🚀 ارسال برای همه + پین (Pin) در چت 📌", callback_data="broadcast_confirm_pin", style="success")],
        [InlineKeyboardButton("📨 فقط ارسال برای همه (بدون پین) 📨", callback_data="broadcast_confirm_nopin", style="primary")],
        [InlineKeyboardButton("🔴 ❌ انصراف و لغو ارسال 🔙", callback_data="broadcast_cancel", style="danger")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stage_clear_confirm_keyboard(stage_num: int):
    keyboard = [
        [InlineKeyboardButton("🔴 بله، حذف و پاکسازی کامل شود 🗑", callback_data=f"stage_clear_do_{stage_num}", style="danger")],
        [InlineKeyboardButton("🟢 خیر، انصراف و بازگشت 🔙", callback_data=f"stage_detail_{stage_num}", style="success")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_milestones_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎯 📥 بارگذاری محتوای روز ۸ مشاوره 🎯", callback_data="admin_record_8", style="primary")],
        [InlineKeyboardButton("🏆 📥 بارگذاری محتوای روز ۳۰ مشاوره 🏆", callback_data="admin_record_30", style="primary")],
        [InlineKeyboardButton("⚡️ 🚀 ارسال فوری نوبت‌های روز ۸ و ۳۰ ⚡️", callback_data="admin_force_milestones", style="success")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_drip_settings_keyboard(interval_days: int, hour: int):
    keyboard = [
        [InlineKeyboardButton("⚡️ 🚀 ارسال نوبت امروز (اجرای دستی) ⚡️", callback_data="admin_force_drip", style="success")],
        [
            InlineKeyboardButton(f"هر ۱ روز {'✅' if interval_days == 1 else ''}", callback_data="drip_set_int_1", style="primary"),
            InlineKeyboardButton(f"هر ۲ روز {'✅' if interval_days == 2 else ''}", callback_data="drip_set_int_2", style="primary"),
            InlineKeyboardButton(f"هر ۳ روز {'✅' if interval_days == 3 else ''}", callback_data="drip_set_int_3", style="primary"),
        ],
        [
            InlineKeyboardButton(f"ساعت ۱۰ شب (۲۲){' ✅' if hour == 22 else ''}", callback_data="drip_set_hour_22", style="primary"),
            InlineKeyboardButton(f"ساعت ۱۱ شب (۲۳){' ✅' if hour == 23 else ''}", callback_data="drip_set_hour_23", style="primary"),
        ],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی مدیریت 🏠", callback_data="admin_menu", style="danger")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_list_for_plans_keyboard(students: list):
    keyboard = []
    row = []
    for st in students:
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        uid = st["user_id"]
        row.append(InlineKeyboardButton(f"👤 {name}", callback_data=f"plan_user_{uid}", style="primary"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")])
    return InlineKeyboardMarkup(keyboard)

def get_plan_duration_keyboard(target_user_id: int):
    keyboard = [
        [
            InlineKeyboardButton("🟢 🗓 برنامه ۷ روزه 🎯", callback_data=f"plan_set_{target_user_id}_7", style="success"),
            InlineKeyboardButton("🔵 🗓 برنامه ۸ روزه 🎯", callback_data=f"plan_set_{target_user_id}_8", style="primary"),
            InlineKeyboardButton("🟣 🗓 برنامه ۹ روزه 🎯", callback_data=f"plan_set_{target_user_id}_9", style="primary"),
        ],
        [InlineKeyboardButton("🟠 ✏️ تعداد روز دلخواه (دستی)", callback_data=f"plan_custom_{target_user_id}", style="primary")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به لیست دانش‌آموزان 👥", callback_data="admin_plans_menu", style="danger")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Complete Student Deletion & Chat Wiping/Editing Logic ---

REMOVAL_NOTICE_TEXT = "⛔️ تمام محتوا حذف شد و شما دیگر دانش‌آموز دکتر هاشمی نیستید."

async def _clean_or_edit_single_message(bot, chat_id: int, mid: int) -> bool:
    """
    Attempts to delete message `mid` in `chat_id`.
    If Telegram does not allow deletion (e.g. message older than 48 hours), edits the message text/caption/markup in-place
    to replace its content with REMOVAL_NOTICE_TEXT.
    Returns True if the message belonged to this chat and was deleted or edited.
    """
    if not mid or mid <= 0:
        return False
    try:
        res = await bot.delete_message(chat_id=chat_id, message_id=mid)
        if res:
            return True
    except Exception as del_err:
        err_str = str(del_err).lower()
        if "message to delete not found" in err_str or "message_id_invalid" in err_str or "chat not found" in err_str:
            return False

        edited = False
        if hasattr(bot, "edit_message_text"):
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=mid,
                    text=REMOVAL_NOTICE_TEXT,
                    reply_markup=None
                )
                edited = True
            except Exception as edit_err:
                if "message is not modified" in str(edit_err).lower():
                    edited = True

        if not edited and hasattr(bot, "edit_message_caption"):
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=mid,
                    caption=REMOVAL_NOTICE_TEXT,
                    reply_markup=None
                )
                edited = True
            except Exception:
                pass

        if not edited and hasattr(bot, "edit_message_reply_markup"):
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=mid,
                    reply_markup=None
                )
                edited = True
            except Exception:
                pass

        return edited
    return False

async def perform_student_deletion(bot, target_user_id: int, notify_chat_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Completely removes a student:
    1. Sends a probe message with ReplyKeyboardRemove() to strip capabilities and obtain the latest message_id in the chat.
    2. Deletes or edits all previous voices, files, and messages in the student's chat (both tracked DB IDs and backward scan for older messages).
    3. Unpins all pinned messages in the student's chat.
    4. Removes all student records, study plans, dossier, and scheduled tasks from the database.
    5. Edits/sends the final notice: «تمام محتوا حذف شد و شما دیگر دانش‌آموز دکتر هاشمی نیستید.»
    """
    async with get_user_lock(target_user_id):
        st = database.get_student(config.DB_PATH, target_user_id)
        raw_name = (st.get("assigned_name") or st.get("full_name") or f"کاربر {target_user_id}") if st else f"کاربر {target_user_id}"
        safe_name = escape_md(raw_name)

        # 1. Gather all tracked message IDs for this student
        tracked_ids = set(database.get_student_sent_messages(config.DB_PATH, target_user_id))

        # 2. Unpin any pinned messages in student's chat
        try:
            await bot.unpin_all_chat_messages(chat_id=target_user_id)
        except Exception:
            pass

        # 3. Send initial probe message with ReplyKeyboardRemove() to immediately strip bottom keyboard and get current message_id
        kb_remove = ReplyKeyboardRemove() if ReplyKeyboardRemove else None
        probe_mid = None
        notified_student = False
        try:
            probe_msg = await bot.send_message(
                chat_id=target_user_id,
                text="⏳ در حال حذف تمامی محتوا و ویس‌های قبلی...",
                reply_markup=kb_remove
            )
            probe_mid = getattr(probe_msg, "message_id", None)
            notified_student = True
        except Exception as e:
            logger.warning(f"Could not send initial probe to student {target_user_id}: {e}")

        # 4. Build candidate message IDs to wipe/edit
        candidate_ids = set(tracked_ids)
        if probe_mid and probe_mid > 1:
            scan_start = probe_mid - 1
            scan_end = max(1, probe_mid - 160)
            for m in range(scan_start, scan_end - 1, -1):
                candidate_ids.add(m)
        for anchor in list(tracked_ids):
            for m in range(max(1, anchor - 15), anchor + 15):
                if not probe_mid or m < probe_mid:
                    candidate_ids.add(m)

        sorted_candidates = sorted(candidate_ids, reverse=True)
        sem = asyncio.Semaphore(25)
        cleaned_count = 0
        lowest_hit_mid = None

        async def _worker(mid: int) -> Tuple[int, bool]:
            async with sem:
                ok = await _clean_or_edit_single_message(bot, target_user_id, mid)
                return mid, ok

        # Execute in batches of 50 for fast parallel wiping/editing
        for i in range(0, len(sorted_candidates), 50):
            batch = sorted_candidates[i : i + 50]
            results = await asyncio.gather(*(_worker(m) for m in batch))
            for m, ok in results:
                if ok:
                    cleaned_count += 1
                    if lowest_hit_mid is None or m < lowest_hit_mid:
                        lowest_hit_mid = m

        # Adaptive extension: if we found messages near the bottom of the backward scan window, scan further back
        if lowest_hit_mid and probe_mid and lowest_hit_mid <= max(2, probe_mid - 140):
            extra_ids = [
                m for m in range(lowest_hit_mid - 1, max(0, lowest_hit_mid - 120), -1)
                if m not in candidate_ids
            ]
            for i in range(0, len(extra_ids), 50):
                batch = extra_ids[i : i + 50]
                results = await asyncio.gather(*(_worker(m) for m in batch))
                for _, ok in results:
                    if ok:
                        cleaned_count += 1

        # 5. Clear in-memory state and wipe database records for this student
        active_states.pop(target_user_id, None)
        database.delete_student_completely(config.DB_PATH, target_user_id)

        # 6. Remove temporary probe message and send the final removal notice with ReplyKeyboardRemove()
        if probe_mid:
            deleted_probe = False
            try:
                await bot.delete_message(chat_id=target_user_id, message_id=probe_mid)
                deleted_probe = True
            except Exception:
                if hasattr(bot, "edit_message_text"):
                    try:
                        await bot.edit_message_text(
                            chat_id=target_user_id,
                            message_id=probe_mid,
                            text=REMOVAL_NOTICE_TEXT
                        )
                    except Exception:
                        pass

            if deleted_probe:
                try:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=REMOVAL_NOTICE_TEXT,
                        reply_markup=kb_remove
                    )
                    notified_student = True
                except Exception:
                    pass
        else:
            try:
                await bot.send_message(
                    chat_id=target_user_id,
                    text=REMOVAL_NOTICE_TEXT,
                    reply_markup=kb_remove
                )
                notified_student = True
            except Exception:
                pass

        # 7. Notify admin if requested
        if notify_chat_id:
            admin_report = (
                f"🗑 **دانش‌آموز «{safe_name}» به طور کامل حذف شد.** ✅\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"• 🎙 تمام ویس‌ها و چت‌های قبلی ({cleaned_count} پیام) حذف یا ادیت شدند.\n"
                "• 🔒 تمام قابلیت‌ها، دکمه‌ها و ارسال‌های خودکار برای او غیرفعال و پاک شدند.\n"
                f"• 📩 پیام **«{REMOVAL_NOTICE_TEXT}»** در چت او ثبت گردید و دیگر هیچ پیامی برایش فرستاده نخواهد شد."
            )
            await safe_bot_send_message(
                bot,
                chat_id=notify_chat_id,
                text=admin_report,
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )

        return {
            "user_id": target_user_id,
            "name": raw_name,
            "deleted_messages_count": cleaned_count,
            "notified_student": notified_student,
        }

async def handle_admin_delete_student_query(msg, bot, admin_id: int, query_name: str):
    """Finds the closest student(s) by full or partial name and either deletes immediately (if unambiguous) or offers closest matches."""
    all_students = database.list_students(config.DB_PATH)
    if not all_students:
        active_states[admin_id] = None
        await safe_reply_text(
            msg,
            "ℹ️ در حال حاضر هیچ دانش‌آموزی در لیست شما وجود ندارد.",
            reply_markup=get_admin_reply_keyboard()
        )
        return

    matches = database.find_closest_students_by_name(config.DB_PATH, query_name, limit=6)
    if not matches:
        await safe_reply_text(
            msg,
            "ℹ️ دانش‌آموزی با این مشخصات یافت نشد.",
            reply_markup=get_admin_reply_keyboard()
        )
        return

    top = matches[0]
    is_single_clear_match = (
        (len(matches) == 1 and top.get("match_score", 0) >= 0.45)
        or (top.get("match_type") == "exact" and (len(matches) == 1 or matches[1].get("match_type") != "exact"))
    )

    if is_single_clear_match:
        active_states[admin_id] = None
        await perform_student_deletion(bot, top["user_id"], notify_chat_id=msg.chat_id)
        return

    active_states[admin_id] = "AWAITING_DELETE_STUDENT_NAME"
    safe_q = escape_md(query_name)
    await safe_reply_text(
        msg,
        f"🔍 **نزدیک‌ترین نام‌های پیداشده به «{safe_q}»:**\n\n"
        "لطفاً روی نام دانش‌آموز مورد نظر بزنید تا بلافاصله حذف کامل انجام شود (یا نام دقیق‌تر را تایپ کنید):",
        parse_mode="Markdown",
        reply_markup=get_delete_students_keyboard(matches)
    )

# --- Content Delivery Helpers (Strictly Ordered & Deduplicated) ---

async def _deliver_stage_content_unlocked(bot, chat_id: int, stage: int, next_stage: int, greeting_name: str = ""):
    student = database.get_student(config.DB_PATH, chat_id)
    if not student:
        return
    curr_st = student["current_stage"]
    reply_kb = get_student_reply_keyboard(curr_st)

    msgs = database.get_stage_messages(config.DB_PATH, stage)
    if not msgs:
        await send_student_msg(
            bot,
            user_id=chat_id,
            text=f"📌 محتوای **مرحله {stage}** به زودی توسط دکتر هاشمی بارگذاری می‌شود ⏳",
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        return

    # Strictly filter out any message already delivered to this student (never send any voice twice)
    msgs_to_send = []
    seen_in_batch = set()
    for m in msgs:
        if database.is_message_delivered(
            config.DB_PATH,
            chat_id,
            m["id"],
            m.get("from_chat_id"),
            m.get("message_id")
        ):
            continue
        dedup_token = m.get("file_unique_id") or (m.get("from_chat_id"), m.get("message_id"))
        if dedup_token in seen_in_batch:
            database.record_message_delivery(
                config.DB_PATH,
                chat_id,
                stage,
                m["id"],
                m.get("from_chat_id"),
                m.get("message_id")
            )
            continue
        seen_in_batch.add(dedup_token)
        msgs_to_send.append(m)

    safe_greeting = escape_md(greeting_name)
    name_phrase = f"{safe_greeting} عزیز، " if safe_greeting else ""

    if msgs_to_send:
        await send_student_msg(
            bot,
            user_id=chat_id,
            text=f"📚 **{name_phrase}محتوای مرحله {stage} به ترتیب برای شما ارسال می‌شود:**\n"
                 "لطفاً ویس‌ها و فایل‌های آموزشی را با دقت گوش داده و یادداشت‌برداری کنید 🌱✨",
            parse_mode="Markdown"
        )

        for item in msgs_to_send:
            if database.is_message_delivered(
                config.DB_PATH,
                chat_id,
                item["id"],
                item.get("from_chat_id"),
                item.get("message_id")
            ):
                continue
            try:
                await copy_student_msg(
                    bot,
                    user_id=chat_id,
                    from_chat_id=item["from_chat_id"],
                    message_id=item["message_id"],
                    content_type="voice"
                )
                database.record_message_delivery(
                    config.DB_PATH,
                    chat_id,
                    stage,
                    item["id"],
                    item.get("from_chat_id"),
                    item.get("message_id")
                )
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Failed to copy message {item.get('message_id')} for stage {stage}: {e}")
    else:
        await send_student_msg(
            bot,
            user_id=chat_id,
            text=f"ℹ️ **{name_phrase}ویس‌های مرحله {stage} قبلاً برای شما ارسال شده‌اند و در بالای همین صفحه قرار دارند.** 🌱",
            parse_mode="Markdown"
        )

    btn_text = f"🟢 ✅ گوش دادم | رفتن به مرحله {next_stage} ➡️" if next_stage <= 3 else "🚀 🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=f"student_goto_{next_stage}", style="success")]])

    extra_stage1_note = (
        "پس از گوش دادن کامل به ویس‌های مرحله اول، به هاشمی اطلاع دهید و سپس دکمه تایید زیر را لمس نمایید:"
        if stage == 1
        else "پس از گوش دادن کامل به فایل‌ها، دکمه تایید زیر را لمس نمایید:"
    )

    await send_student_msg(
        bot,
        user_id=chat_id,
        text=f"✨ وضعیت **مرحله {stage}** 🎓\n{extra_stage1_note}",
        parse_mode="Markdown",
        reply_markup=inline_kb
    )
    await send_student_msg(
        bot,
        user_id=chat_id,
        text="👇 *گزینه‌های دسترسی سریع:*",
        parse_mode="Markdown",
        reply_markup=reply_kb
    )

async def deliver_stage_content(bot, chat_id: int, stage: int, next_stage: int, greeting_name: str = ""):
    lock = get_user_lock(chat_id)
    if lock.locked():
        # If called from inside an already-locked block for this user, avoid deadlock
        await _deliver_stage_content_unlocked(bot, chat_id, stage, next_stage, greeting_name)
    else:
        async with lock:
            await _deliver_stage_content_unlocked(bot, chat_id, stage, next_stage, greeting_name)

async def execute_drip_cycle(bot, force_ignore_interval: bool = False) -> int:
    async with drip_cycle_lock:
        eligible_pairs = database.get_eligible_students_for_drip(config.DB_PATH, force_ignore_interval=force_ignore_interval)
        sent_count = 0

        for st, _ in eligible_pairs:
            user_id = st["user_id"]
            async with get_user_lock(user_id):
                fresh_st = database.get_student(config.DB_PATH, user_id)
                if not fresh_st or fresh_st.get("current_stage", 1) < 4:
                    continue

                pkg_msgs = database.get_next_unsent_drip_package(config.DB_PATH, user_id)
                if not pkg_msgs:
                    continue

                unsent_msgs = [
                    m for m in pkg_msgs
                    if not database.is_message_delivered(
                        config.DB_PATH,
                        user_id,
                        m["id"],
                        m.get("from_chat_id"),
                        m.get("message_id")
                    )
                ]
                if not unsent_msgs:
                    continue

                raw_name = fresh_st.get("assigned_name") or fresh_st.get("full_name") or "دانش‌آموز"
                name = escape_md(raw_name)
                pkg_id = unsent_msgs[0].get("package_id") or unsent_msgs[0]["id"]

                try:
                    if len(unsent_msgs) > 1:
                        intro = (
                            f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎙🎬 **مبحث تکمیلی جدید دکتر هاشمی ({len(unsent_msgs)} فایل پیوسته) برای شما ارسال می‌شود:**\n\n"
                            "📌 لطفاً تمام فایل‌های این بسته آموزشی (ویدیو/عکس و ویس توضیحات) را با دقت و به ترتیب مشاهده کرده و گوش دهید 🎓"
                        )
                    else:
                        caption_text = f"\n\n{escape_md(unsent_msgs[0].get('caption'))}" if unsent_msgs[0].get("caption") else ""
                        intro = (
                            f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎙 **مبحث تکمیلی جدید از طرف دکتر هاشمی برای شما ارسال می‌شود:**{caption_text}"
                        )

                    await send_student_msg(
                        bot,
                        user_id=user_id,
                        text=intro,
                        parse_mode="Markdown"
                    )

                    delivered_ids = []
                    for m in unsent_msgs:
                        if database.is_message_delivered(
                            config.DB_PATH,
                            user_id,
                            m["id"],
                            m.get("from_chat_id"),
                            m.get("message_id")
                        ):
                            continue
                        try:
                            await copy_student_msg(
                                bot,
                                user_id=user_id,
                                from_chat_id=m["from_chat_id"],
                                message_id=m["message_id"],
                                content_type="voice"
                            )
                            delivered_ids.append(m["id"])
                        except Exception as copy_err:
                            logger.warning(f"Could not copy drip message {m['id']} to {user_id}: {copy_err}")
                        finally:
                            # Always record so a deleted source message never causes an infinite intro loop
                            database.record_drip_package_delivery(config.DB_PATH, user_id, pkg_id, [m["id"]])
                            database.record_message_delivery(
                                config.DB_PATH,
                                user_id,
                                4,
                                m["id"],
                                m.get("from_chat_id"),
                                m.get("message_id")
                            )
                        await asyncio.sleep(0.4)

                    if delivered_ids:
                        sent_count += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error delivering drip package {pkg_id} to student {user_id}: {e}")

        return sent_count

async def execute_milestones_check(bot) -> int:
    due_list = database.get_students_due_for_milestones(config.DB_PATH)
    sent_count = 0

    for st, milestone in due_list:
        user_id = st["user_id"]
        async with get_user_lock(user_id):
            fresh_st = database.get_student(config.DB_PATH, user_id)
            if not fresh_st:
                continue
            if milestone == 8 and fresh_st.get("day_8_sent"):
                continue
            if milestone == 30 and fresh_st.get("day_30_sent"):
                continue

            raw_name = fresh_st.get("assigned_name") or fresh_st.get("full_name") or "دانش‌آموز"
            name = escape_md(raw_name)
            msgs = database.get_stage_messages(config.DB_PATH, milestone)
            if not msgs:
                continue

            unsent_msgs = []
            seen_batch = set()
            for m in msgs:
                if database.is_message_delivered(
                    config.DB_PATH,
                    user_id,
                    m["id"],
                    m.get("from_chat_id"),
                    m.get("message_id")
                ):
                    continue
                dedup_token = m.get("file_unique_id") or (m.get("from_chat_id"), m.get("message_id"))
                if dedup_token in seen_batch:
                    database.record_message_delivery(
                        config.DB_PATH,
                        user_id,
                        milestone,
                        m["id"],
                        m.get("from_chat_id"),
                        m.get("message_id")
                    )
                    continue
                seen_batch.add(dedup_token)
                unsent_msgs.append(m)

            if not unsent_msgs:
                database.mark_milestone_sent(config.DB_PATH, user_id, milestone)
                continue

            try:
                # Claim milestone first before sending so no race or partial failure resends the header repeatedly
                database.mark_milestone_sent(config.DB_PATH, user_id, milestone)

                if milestone == 8:
                    header = (
                        f"🌸 **سلام {name} عزیز، خداقوت!** 🎯\n\n"
                        "📌 امروز دقیقاً **روز هشتم** از شروع مسیر مشاوره شماست.\n"
                        "این محتوای بسیار مهم، ویژه و کلیدی روز هشتم برای شما ارسال می‌شود. لطفاً با تمرکز کامل گوش دهید:"
                    )
                else:
                    header = (
                        f"🌟 **سلام {name} عزیز، تبریک ویژه!** 🏆✨\n\n"
                        "🏅 امروز **روز سی‌ام (پایان ماه اول)** مسیر مشاوره طلایی شماست.\n"
                        "این محتوای بسیار ارزشمند و استراتژیک روز سی‌ام برای ارزیابی و گام‌های بعدی شما ارسال می‌شود:"
                    )

                await send_student_msg(bot, user_id=user_id, text=header, parse_mode="Markdown")
                for item in unsent_msgs:
                    try:
                        await copy_student_msg(
                            bot,
                            user_id=user_id,
                            from_chat_id=item["from_chat_id"],
                            message_id=item["message_id"],
                            content_type="voice"
                        )
                    except Exception as copy_err:
                        logger.warning(f"Failed copying milestone {milestone} item {item['id']} to {user_id}: {copy_err}")
                    finally:
                        database.record_message_delivery(
                            config.DB_PATH,
                            user_id,
                            milestone,
                            item["id"],
                            item.get("from_chat_id"),
                            item.get("message_id")
                        )
                    await asyncio.sleep(0.3)

                sent_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error sending milestone {milestone} to user {user_id}: {e}")

    return sent_count

async def execute_study_plans_reminders(bot) -> int:
    due_plans = database.get_study_plans_due_for_reminder(config.DB_PATH)
    sent_count = 0

    for plan in due_plans:
        user_id = plan["user_id"]
        async with get_user_lock(user_id):
            st = database.get_student(config.DB_PATH, user_id)
            if not st:
                continue
            # Re-verify that reminder hasn't already been sent inside lock
            active_plan = database.get_active_study_plan(config.DB_PATH, user_id)
            if not active_plan or active_plan["id"] != plan["id"] or active_plan.get("reminder_sent") == 1:
                continue

            database.mark_study_plan_reminder_sent(config.DB_PATH, plan["id"])
            raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
            name = escape_md(raw_name)
            duration = plan["duration_days"]
            curr_stage = st.get("current_stage", 4)

            msg = (
                f"⚠️🔔 **یادآوری بسیار مهم برنامه درسی ({duration} روزه):**\n\n"
                f"🌸 **{name} عزیز، فردا برنامه مطالعاتی شما به پایان می‌رسد!** ⏳\n\n"
                "📌 لطفاً حتماً قبل از گزارش کارت برای دکتر هاشمی، لیست تمام مباحثی که تا این لحظه خوانده‌اید را ارسال کنید.\n"
                "💡 *راهنمای نمونه:* بنویسید کدام مباحث را خوانده‌اید، چند تا تست زده‌اید، و اگر ویدیو دیده‌اید تا کجای ویدیو پیش رفته‌اید.\n\n"
                "👇 *برای ارسال گزارش، دکمه زیر را لمس کرده و متنتان را بفرستید:*"
            )
            report_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🟣 📝 ارسال لیست مباحث خوانده‌شده 📩", callback_data="student_start_report", style="primary")
            ]])

            try:
                await send_student_msg(
                    bot,
                    user_id=user_id,
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=report_kb
                )
                await send_student_msg(
                    bot,
                    user_id=user_id,
                    text="👇 یا مستقیماً از دکمه «🟣 📝 ارسال لیست مباحث خوانده‌شده 📩» در پایین کیبورد استفاده کنید:",
                    reply_markup=get_student_reply_keyboard(curr_stage)
                )
                sent_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error sending plan reminder to user {user_id}: {e}")

    return sent_count

async def execute_inactivity_warnings(bot) -> int:
    due_students = database.get_students_due_for_inactivity_warning(config.DB_PATH)
    sent_count = 0

    for st in due_students:
        user_id = st["user_id"]
        async with get_user_lock(user_id):
            fresh_st = database.get_student(config.DB_PATH, user_id)
            if not fresh_st or fresh_st.get("stage_warning_sent") == 1:
                continue
            stage = fresh_st.get("current_stage", 1)
            if stage > 3:
                continue

            database.mark_inactivity_warning_sent(config.DB_PATH, user_id)
            raw_name = fresh_st.get("assigned_name") or fresh_st.get("full_name") or "دانش‌آموز"
            name = escape_md(raw_name)
            next_stage = stage + 1
            hours = int(st.get("hours_waiting", 18 if stage == 1 else 24))

            if stage == 1:
                time_desc = f"بیش از ۱۸ ساعت ({hours} ساعت)"
                action_desc = "ویس‌های **مرحله ۱** را گوش نداده‌اید و وارد مرحله دوم نشده‌اید!"
            elif stage == 2:
                time_desc = f"بیش از ۲۴ ساعت ({hours} ساعت)"
                action_desc = "در **مرحله ۲** هستید و هنوز وارد مرحله سوم نشده‌اید!"
            else:
                time_desc = f"بیش از ۲۴ ساعت ({hours} ساعت)"
                action_desc = "در **مرحله ۳** هستید و وارد فاز ۴ نشده‌اید!"

            warning_text = (
                f"⚠️ **{name} عزیز، یک یادآوری بسیار مهم!** ⏳\n\n"
                f"الان {time_desc} است که {action_desc}\n"
                "مسیر مشاوره نیازمند استمرار و تعهد شماست و نباید از برنامه عقب بمانید 🌱\n\n"
                "📌 **حتماً تا آخر امشب وقت بگذارید، ویس‌های این مرحله را کامل گوش دهید و وارد مرحله بعد شوید.**\n\n"
                f"🔒 **توجه:** در حال حاضر برای شما محدودیت ایجاد شده است چون هنوز در مرحله {stage} هستید. "
                "به محض اینکه وارد فاز ۴ شوید، یک سری ویس‌ها و آموزش‌های بسیار تکمیلی و کلیدی برایتان ارسال می‌شود که جهش بزرگی در روند مطالعه شما ایجاد خواهد کرد 🚀✨\n\n"
                "👇 همین الان ویس‌ها را گوش دهید و پس از اتمام دکمه زیر را لمس کنید:"
            )

            btn_text = f"🟢 ✅ گوش دادم | رفتن به مرحله {next_stage} ➡️" if next_stage <= 3 else "🚀 🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_text, callback_data=f"student_goto_{next_stage}", style="success")],
            ])

            try:
                await send_student_msg(
                    bot,
                    user_id=user_id,
                    text=warning_text,
                    parse_mode="Markdown",
                    reply_markup=kb
                )
                sent_count += 1
                logger.info(f"Inactivity warning sent to student {raw_name} ({user_id}) for stage {stage} ({hours}h).")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error sending inactivity warning to user {user_id}: {e}")

    return sent_count

async def execute_morning_quotes_cycle(bot, target_user_id: Optional[int] = None) -> int:
    """Delivers daily 7:00 AM authentic motivational quotes to active students."""
    today_str = get_iran_now().strftime("%Y-%m-%d")
    total_quotes = len(motivational_quotes.MOTIVATIONAL_QUOTES)

    if target_user_id:
        st_obj = database.get_student(config.DB_PATH, target_user_id)
        if st_obj:
            students = [st_obj]
        else:
            students = [{"user_id": target_user_id, "full_name": "محمد", "assigned_name": "محمد"}]
        try:
            curr_idx = int(database.get_setting(config.DB_PATH, "test_morning_quote_index", "0"))
        except ValueError:
            curr_idx = 0
    else:
        students = database.list_students(config.DB_PATH)
        if not students:
            return 0
        claimed, curr_idx = database.claim_morning_quote_date(config.DB_PATH, today_str, total_quotes)
        if not claimed:
            return 0

    quote_data = motivational_quotes.get_motivational_quote(curr_idx)
    sent_count = 0

    for st in students:
        if not st:
            continue
        user_id = st["user_id"]
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        msg_text = motivational_quotes.format_morning_message(escape_md(raw_name), quote_data)
        try:
            is_real_student = database.get_student(config.DB_PATH, user_id) is not None
            await send_student_msg(
                bot,
                user_id=user_id,
                text=msg_text,
                parse_mode="Markdown",
                track=is_real_student
            )
            sent_count += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.warning(f"Could not send morning quote to {user_id}: {e}")

    if target_user_id and sent_count > 0:
        next_test_idx = (curr_idx + 1) % total_quotes
        database.set_setting(config.DB_PATH, "test_morning_quote_index", str(next_test_idx))
    elif not target_user_id and sent_count > 0:
        logger.info(f"Morning quote #{curr_idx + 1} sent to {sent_count} students successfully.")

    return sent_count

def generate_students_stats_report() -> str:
    students = database.list_students(config.DB_PATH)
    counts = database.get_student_count_by_stage(config.DB_PATH)
    pkg4_count = database.get_stage_4_package_count(config.DB_PATH)

    s4 = counts.get(4, 0)
    total = len(students)

    report = (
        "📊 **آمار و وضعیت دانش‌آموزان مشاوره:** 📈\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 کل دانش‌آموزان ثبت‌شده: **{total} نفر**\n"
        f"🚀 وارد شده به فاز ۴: **{s4} نفر**\n"
        f"📦 بسته‌های آموزشی آماده در بخش ۴: **{pkg4_count} بسته**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 **لیست دانش‌آموزان:**\n"
    )

    if not students:
        report += "هنوز دانش‌آموزی ثبت‌نام نکرده است.\n"
        return report

    now = database.get_now_utc()
    for idx, st in enumerate(students, start=1):
        disp_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        reg_str = st.get("registered_at")
        day_num = 1
        if reg_str:
            dt = database.parse_db_datetime(reg_str)
            if dt:
                day_num = max(1, (now - dt).days + 1)
        safe_name = escape_md(disp_name)
        report += f"{idx}. **{safe_name}** ⬅️ روز {day_num}\n"

    return report

async def send_database_backup_file(bot, chat_id: int):
    if not os.path.exists(config.DB_PATH):
        await bot.send_message(chat_id=chat_id, text="⚠️ فایل دیتابیس هنوز ایجاد نشده است.")
        return

    backup_file = database.create_database_backup(config.DB_PATH)
    now_str = get_iran_now().strftime("%Y-%m-%d_%H-%M")
    try:
        with open(backup_file, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"backup_consulting_bot_{now_str}.db",
                caption=(
                    "💾 **نسخه پشتیبان کامل دیتابیس ربات** 📦\n\n"
                    f"🗓 تاریخ بکاپ: `{now_str}`\n"
                    "📌 **شامل:** کلیه اطلاعات دانش‌آموزان، مراحل، فایل‌های ذخیره‌شده و تنظیمات زمان‌بندی.\n\n"
                    "💡 *هر زمان که نیاز به بازیابی یا انتقال اطلاعات داشتید، کافیست همین فایل پشتیبان (`.db`) را مستقیماً در پی‌وی برای ربات بفرستید تا بلافاصله پشتیبانی و جایگزین شود.*"
                ),
                parse_mode="Markdown"
            )
    finally:
        if os.path.exists(backup_file) and os.path.abspath(backup_file) != os.path.abspath(config.DB_PATH):
            try:
                os.remove(backup_file)
            except Exception:
                pass

# --- Core Handlers ---

async def _show_existing_student_panel(update, context, student: Dict[str, Any], user_name: str):
    """Displays the appropriate current-stage panel for an already-registered student without duplicating registration."""
    user_id = student["user_id"]
    stage = student.get("current_stage", 1)
    name = student.get("assigned_name") or user_name
    safe_name = escape_md(name)
    reply_kb = get_student_reply_keyboard(stage)
    if stage <= 3:
        await reply_student_msg(
            update.message,
            user_id=user_id,
            text=(
                f"👋 **سلام مجدد {safe_name} عزیز.** 🌱\n"
                f"شما در حال حاضر در **مرحله {stage}** هستید."
            ),
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        await _deliver_stage_content_unlocked(context.bot, user_id, stage=stage, next_stage=stage + 1, greeting_name=name)
    else:
        await reply_student_msg(
            update.message,
            user_id=user_id,
            text=(
                f"🌟 **سلام {safe_name} عزیز!**\n\n"
                "📌 **این صفحه را حتماً پین (Pin) داشته باشید تا همیشه در دسترستان باشد.**\n"
                "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب و ویس‌های جدید مشاوره‌ای در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** ✨"
            ),
            parse_mode="Markdown",
            reply_markup=reply_kb
        )

async def start_handler(update, context):
    if not update or not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or "دانش‌آموز"
    username = update.effective_user.username

    async with get_user_lock(user_id):
        # 1. Check for invite argument (/start inv_XXXXXX) - Checked FIRST so invites always register students
        args = context.args if context else []
        if args and len(args) > 0 and args[0].startswith("inv_"):
            code = args[0]
            pre_existing_student = database.get_student(config.DB_PATH, user_id)
            was_removed = database.is_student_removed(config.DB_PATH, user_id)

            success, assigned_name = database.validate_and_use_invite(
                config.DB_PATH, code, user_id, username, user_name
            )

            if not success:
                # If this student already used this invite link or is already an active student, open their panel instead of rejecting
                inv_info = database.get_invite(config.DB_PATH, code)
                curr_student = database.get_student(config.DB_PATH, user_id)
                if curr_student and not database.is_student_removed(config.DB_PATH, user_id):
                    if inv_info and inv_info.get("used_by") == user_id:
                        await _show_existing_student_panel(update, context, curr_student, user_name)
                        return
                await safe_reply_text(
                    update.message,
                    "❌ **لینک دعوت نامعتبر یا منقضی شده است.**\n"
                    "این لینک قبلاً استفاده شده است یا وجود ندارد. لطفاً جهت دریافت لینک جدید با دکتر هاشمی در ارتباط باشید 🩺",
                    parse_mode="Markdown"
                )
                return

            # If student was already active (not removed) and clicked a second invite link, avoid duplicate onboarding & duplicate admin alerts
            if pre_existing_student and not was_removed:
                updated_st = database.get_student(config.DB_PATH, user_id) or pre_existing_student
                await _show_existing_student_panel(update, context, updated_st, user_name)
                return

            display_name = assigned_name or user_name
            safe_display_name = escape_md(display_name)
            salutation = f"سلام {safe_display_name} عزیز 🌸"
            await reply_student_msg(
                update.message,
                user_id=user_id,
                text=(
                    f"✨ **{salutation}**\n\n"
                    "به ربات مشاوره تحصیلی دکتر هاشمی خوش آمدید 🌱🎓\n"
                    "مسیر آموزشی شما در ۳ مرحله اولیه و سپس فاز تکمیلی طراحی شده است."
                ),
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(1)
            )

            intro = database.get_intro_content(config.DB_PATH)

            # Step 1: Send Red Lines and Rules text FIRST (as instructed in the invite message)
            if intro and intro.get("text_content"):
                rules_text = intro["text_content"]
            else:
                rules_text = (
                    "📌 **توضیحات مهم و خط قرمزهای مسیر مشاوره:**\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "۱. **تعهد روزانه:** ویس‌ها و آموزش‌ها را با تمرکز کامل در همان روز گوش دهید 🌱\n"
                    "۲. **ارسال دقیق گزارش کار:** مباحث خوانده‌شده و گزارش مطالعه را منظم ارسال کنید 📝\n"
                    "۳. **استمرار و نظم:** این ربات همراه هوشمند شماست تا روز کنکور؛ با تعهد کامل قدم بردارید 🚀🎓"
                )

            # Step 2: Send Dr. Hashemi's Initial Voice/Media (if uploaded and not already delivered)
            has_intro_media = bool(intro and intro.get("message_id") and intro.get("from_chat_id"))
            if has_intro_media:
                await reply_student_msg(
                    update.message,
                    user_id=user_id,
                    text=f"{rules_text}\n\n━━━━━━━━━━━━━━━━━━━━\n🎙 **اکنون ویس اولیه را با دقت گوش دهید:**",
                    parse_mode="Markdown"
                )
                if not database.is_message_delivered(config.DB_PATH, user_id, 0, intro["from_chat_id"], intro["message_id"]):
                    try:
                        await copy_student_msg(
                            context.bot,
                            user_id=user_id,
                            from_chat_id=intro["from_chat_id"],
                            message_id=intro["message_id"],
                            content_type="voice"
                        )
                        database.record_message_delivery(
                            config.DB_PATH,
                            user_id,
                            0,
                            0,
                            intro["from_chat_id"],
                            intro["message_id"]
                        )
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        logger.warning(f"Could not copy intro media message: {e}")

                start_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🟢 ✅ خط قرمزها را خواندم و ویس اولیه را گوش دادم | ورود به مرحله اول 🚀", callback_data="student_start_stage_1", style="success")
                ]])
                await reply_student_msg(
                    update.message,
                    user_id=user_id,
                    text="👇 *پس از مطالعه خط قرمزها و گوش دادن به ویس اولیه، جهت ورود به مرحله اول روی دکمه زیر بزنید:*",
                    parse_mode="Markdown",
                    reply_markup=start_kb
                )
            else:
                start_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🟢 ✅ مطالعه کردم و متعهدم | ورود به مرحله اول 🚀", callback_data="student_start_stage_1", style="success")
                ]])
                await reply_student_msg(
                    update.message,
                    user_id=user_id,
                    text=(
                        f"{rules_text}\n\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "👇 *لطفاً موارد فوق را با دقت مطالعه نموده و سپس جهت ورود به مرحله اول روی دکمه زیر بزنید:*"
                    ),
                    parse_mode="Markdown",
                    reply_markup=start_kb
                )

            # Alert admin ONCE that a student just joined and is now in their students list
            if config.ADMIN_ID and user_id != config.ADMIN_ID:
                if database.claim_admin_registration_notification(config.DB_PATH, user_id):
                    try:
                        uname = f"@{escape_md(username)}" if username else "بدون یوزرنیم"
                        await safe_bot_send_message(
                            context.bot,
                            chat_id=config.ADMIN_ID,
                            text=f"🔔 **دانش‌آموز جدید ثبت‌نام شد:** 👤\n\n"
                                 f"• نام: **{safe_display_name}** ({uname})\n"
                                 "• کد دعوت فعال شد و پیام خط قرمزها برای او ارسال گردید 🎯",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.warning(f"Could not alert admin about new student: {e}")
            return

        # 2. Admin check (Hojjat_hshmi or ADMIN_ID)
        if is_admin(user_id, username):
            active_states[user_id] = None
            await safe_reply_text(
                update.message,
                "👑 **سلام آقای دکتر هاشمی گرامی**\n"
                "به پنل اختصاصی مدیریت ربات مشاوره تحصیلی خوش آمدید 🩺✨🌱\n\n"
                "👇 *دکمه‌های دسترسی سریع همیشه در پایین صفحه برای شما فعال هستند:*",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

        # 3. Check if student was explicitly removed
        if database.is_student_removed(config.DB_PATH, user_id):
            return

        # 4. Existing registered student check
        student = database.get_student(config.DB_PATH, user_id)
        if student:
            await _show_existing_student_panel(update, context, student, user_name)
            return

        # 5. Unauthorized visitor
        await safe_reply_text(
            update.message,
            "⛔️ **دسترسی تایید نشده است**\n\n"
            "ورود به ربات مشاوره فقط با «لینک دعوت اختصاصی» که دکتر هاشمی برایتان ارسال می‌کند امکان‌پذیر است 🩺",
            parse_mode="Markdown"
        )

# --- Admin Callback Handler ---

async def admin_callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    username = query.from_user.username
    if not is_admin(user_id, username):
        await query.edit_message_text("⛔️ شما به این بخش دسترسی ندارید.")
        return

    data = query.data

    if data == "admin_menu":
        active_states[user_id] = None
        active_package_ids.pop(user_id, None)
        await safe_reply_text(
            query.message,
            "👑 **پنل مدیریت ربات مشاوره دکتر هاشمی:**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data == "admin_ask_name":
        active_states[user_id] = "AWAITING_STUDENT_NAME"
        await safe_reply_text(
            query.message,
            "📝 **لطفاً نام دانش‌آموز جدید را تایپ کنید:**\n\n"
            "مثلاً بنویسید: `نگار` یا `علی` یا `ساچلی`\n"
            "(پیام دعوت اختصاصی با همین نام آماده می‌شود تا مستقیماً برای دانش‌آموز فوروارد کنید 🌸)",
            parse_mode="Markdown"
        )

    elif data == "admin_delete_student_menu":
        students = database.list_students(config.DB_PATH)
        if not students:
            active_states[user_id] = None
            await safe_reply_text(
                query.message,
                "ℹ️ در حال حاضر هیچ دانش‌آموزی در لیست شما ثبت نشده است.",
                reply_markup=get_admin_reply_keyboard()
            )
            return
        active_states[user_id] = "AWAITING_DELETE_STUDENT_NAME"
        await safe_reply_text(
            query.message,
            "🗑 **حذف کامل دانش‌آموز:**\n\n"
            "لطفاً نام (کامل یا ناقص) دانش‌آموز مورد نظر را تایپ کنید، یا از لیست زیر انتخاب نمایید:",
            parse_mode="Markdown",
            reply_markup=get_delete_students_keyboard(students)
        )

    elif data.startswith("admin_del_exec_"):
        target_uid = int(data.split("_")[-1])
        active_states[user_id] = None
        await perform_student_deletion(context.bot, target_uid, notify_chat_id=query.message.chat_id)

    elif data == "admin_manage_stages":
        active_states[user_id] = None
        active_package_ids.pop(user_id, None)
        await safe_reply_text(
            query.message,
            "🗂 **مدیریت و مشاهده محتوای مراحل:** 📁\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "جهت مشاهده فایل‌های ذخیره‌شده هر مرحله، افزودن فایل جدید یا پاکسازی، مرحله مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_stages_management_keyboard()
        )

    elif data.startswith("stage_detail_"):
        stage_num = int(data.split("_")[-1])
        st_title = get_stage_display_name(stage_num)
        msgs = database.get_stage_messages(config.DB_PATH, stage_num)
        count = len(msgs)
        # Viewing a stage must NOT secretly activate recording mode; recording starts only on admin_record_X
        active_states[user_id] = None
        active_package_ids.pop(user_id, None)

        if stage_num == 4:
            pkg_count = database.get_stage_4_package_count(config.DB_PATH)
            detail_txt = (
                f"🗂 **مدیریت و وضعیت محتوای «{st_title}»**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 تعداد کل نوبت‌ها و بسته‌ها: **{pkg_count} نوبت**\n"
                f"📎 مجموع فایل‌های ثبت‌شده: **{count} فایل** (ویدیو/عکس/ویس)\n\n"
                "در این بخش می‌توانید به دو روش ویس/فایل اضافه کنید، یا یک نوبت را دستی ارسال نمایید:\n"
                "• 🎙 **ویس تک و مجزا:** هر ویس یک نوبت ارسال جداگانه خواهد بود.\n"
                "• 📦 **پکیج چندرسانه‌ای:** ویدیو یا عکس همراه با ویس توضیحات در یک بسته متصل.\n"
                "• 🎯 **انتخاب و ارسال دستی:** ارسال فوری یک نوبت دلخواه به دانش‌آموزان واجد شرایط."
            )
            await safe_reply_text(
                query.message,
                detail_txt,
                parse_mode="Markdown",
                reply_markup=get_stage_action_keyboard(4, has_content=(count > 0))
            )
        else:
            detail_txt = (
                f"🗂 **مدیریت و وضعیت محتوای «{st_title}»**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📎 تعداد فایل‌های ثبت‌شده در این مرحله: **{count} پیام**\n\n"
                "👇 برای پیش‌نمایش فایل‌ها، افزودن فایل جدید یا پاکسازی، یکی از گزینه‌های زیر را انتخاب نمایید:"
            )
            await safe_reply_text(
                query.message,
                detail_txt,
                parse_mode="Markdown",
                reply_markup=get_stage_action_keyboard(stage_num, has_content=(count > 0))
            )

    elif data.startswith("stage_delfile_"):
        record_id = int(data.split("_")[-1])
        deleted_item = database.delete_stage_message_by_id(config.DB_PATH, record_id)
        if not deleted_item:
            await safe_reply_text(query.message, "ℹ️ این فایل قبلاً حذف شده است.")
            return
        st_num = deleted_item["stage"]
        st_title = get_stage_display_name(st_num)
        rem_count = len(database.get_stage_messages(config.DB_PATH, st_num))
        try:
            await query.message.delete()
        except Exception:
            pass
        await safe_bot_send_message(
            context.bot,
            chat_id=user_id,
            text=f"🗑 **فایل انتخابی از «{st_title}» حذف شد.** ✅\n"
                 f"📎 تعداد فایل‌های باقی‌مانده در این مرحله: **{rem_count} فایل**",
            parse_mode="Markdown",
            reply_markup=get_stage_action_keyboard(st_num, has_content=(rem_count > 0))
        )

    elif data.startswith("stage_preview_"):
        stage_num = int(data.split("_")[-1])
        st_title = get_stage_display_name(stage_num)
        active_states[user_id] = None
        msgs = database.get_stage_messages(config.DB_PATH, stage_num)
        if not msgs:
            await safe_reply_text(
                query.message,
                f"ℹ️ در حال حاضر هیچ فایلی برای **{st_title}** ذخیره نشده است.",
                parse_mode="Markdown",
                reply_markup=get_stage_action_keyboard(stage_num, has_content=False)
            )
            return

        await safe_reply_text(
            query.message,
            f"⏳ در حال استخراج و ارسال پیش‌نمایش فایل‌های **{st_title}** ({len(msgs)} فایل)...",
            parse_mode="Markdown"
        )

        if stage_num == 4:
            packages = database.get_stage_packages(config.DB_PATH, 4)
            for idx, pkg in enumerate(packages, 1):
                await safe_reply_text(
                    query.message,
                    f"📦 **بسته آموزشی شماره {idx}:** ({len(pkg)} فایل)",
                    parse_mode="Markdown"
                )
                for m in pkg:
                    del_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"🗑 حذف این فایل از {st_title}", callback_data=f"stage_delfile_{m['id']}", style="danger")
                    ]])
                    try:
                        await context.bot.copy_message(
                            chat_id=user_id,
                            from_chat_id=m["from_chat_id"],
                            message_id=m["message_id"],
                            reply_markup=del_kb
                        )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.warning(f"Could not copy stage message {m['id']}: {e}")
        else:
            for idx, m in enumerate(msgs, 1):
                del_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"🗑 حذف فایل شماره {idx} از {st_title}", callback_data=f"stage_delfile_{m['id']}", style="danger")
                ]])
                try:
                    await context.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=m["from_chat_id"],
                        message_id=m["message_id"],
                        reply_markup=del_kb
                    )
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Could not copy stage message {m['id']}: {e}")

        await safe_reply_text(
            query.message,
            f"✅ **پیش‌نمایش تمامی فایل‌های «{st_title}» با موفقیت ارسال شد.** 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💡 *(در صورت نیاز به حذف تکی هر فایل، می‌توانید روی دکمه حذف زیر همان فایل بزنید)*\n"
            "برای افزودن یا تغییر محتوا از دکمه‌های زیر استفاده کنید:",
            parse_mode="Markdown",
            reply_markup=get_stage_action_keyboard(stage_num, has_content=True)
        )

    elif data.startswith("stage_clear_ask_"):
        stage_num = int(data.split("_")[-1])
        st_title = get_stage_display_name(stage_num)
        await safe_reply_text(
            query.message,
            f"⚠️ **آیا مطمئن هستید که می‌خواهید تمام فایل‌های «{st_title}» را حذف و پاکسازی کنید؟**\n\n"
            "📌 *پس از حذف، این محتوا دیگر برای دانش‌آموزان جدید ارسال نخواهد شد و باید محتوای جدید برای این مرحله بارگذاری کنید.*",
            parse_mode="Markdown",
            reply_markup=get_stage_clear_confirm_keyboard(stage_num)
        )

    elif data.startswith("stage_clear_do_"):
        stage_num = int(data.split("_")[-1])
        st_title = get_stage_display_name(stage_num)
        active_states[user_id] = None
        deleted = database.clear_stage_messages(config.DB_PATH, stage_num)
        await safe_reply_text(
            query.message,
            f"🗑 **تمام محتوای «{st_title}» ({deleted} فایل) با موفقیت پاک شد.** 🟢\n\n"
            "اکنون می‌توانید با زدن دکمه زیر، فایل‌های جدید را برای این مرحله بارگذاری کنید:",
            parse_mode="Markdown",
            reply_markup=get_stage_action_keyboard(stage_num, has_content=False)
        )

    elif data == "admin_milestones_menu":
        active_states[user_id] = None
        c8 = len(database.get_stage_messages(config.DB_PATH, 8))
        c30 = len(database.get_stage_messages(config.DB_PATH, 30))
        msg = (
            "🎯 **مدیریت محتوای روز ۸ و ۳۰ مشاوره:** 🏆\n\n"
            f"• فایل‌های ذخیره‌شده برای روز ۸: **{c8} پیام 🎯**\n"
            f"• فایل‌های ذخیره‌شده برای روز ۳۰: **{c30} پیام 🏆**\n\n"
            "💡 *این محتواها رأس روز ۸ و روز ۳۰ پس از استارت بات، خودکار برای هر دانش‌آموز ارسال می‌گردد.*"
        )
        await safe_reply_text(query.message, msg, parse_mode="Markdown", reply_markup=get_milestones_keyboard())

    elif data == "admin_test_quote":
        await safe_reply_text(query.message, "⏳ در حال آماده‌سازی و ارسال نمونه پیام انگیزشی ساعت ۷ صبح...")
        await execute_morning_quotes_cycle(context.bot, target_user_id=user_id)
        return

    # --- Section 4 Menus, Solo / Package Recording & Manual Picker ---
    elif data in ("admin_record_4", "admin_record_4_menu"):
        active_states[user_id] = None
        active_package_ids.pop(user_id, None)
        c4 = len(database.get_stage_messages(config.DB_PATH, 4))
        pkg4 = database.get_stage_4_package_count(config.DB_PATH)
        await safe_reply_text(
            query.message,
            "🟣 💡 **مدیریت و بارگذاری محتوای «بخش ۴»:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 وضعیت فعلی: **{pkg4} نوبت/بسته** ({c4} فایل ذخیره)\n\n"
            "نحوه افزودن محتوا یا ارسال را انتخاب کنید:\n"
            "• 🎙 **ویس تک و مجزا:** مناسب ارسال ویس‌های تکی در هر نوبت\n"
            "• 📦 **پکیج چندرسانه‌ای:** مناسب ترکیب ویدیو یا عکس همراه با ویس توضیحات\n"
            "• 🎯 **انتخاب و ارسال دستی:** انتخاب و ارسال یک نوبت خاص به افراد واجد شرایط",
            parse_mode="Markdown",
            reply_markup=get_stage_4_options_keyboard(has_content=(c4 > 0))
        )

    elif data == "admin_record_4_solo":
        active_states[user_id] = "RECORD_STAGE_4_SOLO"
        await safe_reply_text(
            query.message,
            "🎙 **حالت ذخیره‌سازی ویس‌های تک و مجزا (بخش ۴) فعال شد.** 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 هم‌اکنون ویس‌های خود را یکی‌یکی به این چت فوروارد کنید.\n"
            "✨ *هر ویسی که بفرستید به عنوان یک «نوبت ارسال مجزا» ثبت می‌شود و به ترتیب دقیق و بدون تکرار ارسال خواهد شد.*\n\n"
            "👇 پس از اتمام فوروارد ویس‌ها، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "admin_record_4_package":
        active_states[user_id] = "RECORD_STAGE_4_PACKAGE"
        active_package_ids[user_id] = int(get_iran_now().timestamp())
        await safe_reply_text(
            query.message,
            "📦 **حالت ذخیره‌سازی پکیج چندرسانه‌ای (بخش ۴) فعال شد.** 🟢\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 اکنون می‌توانید فایل‌های این پکیج (ویدیو، عکس، ویس) را پشت سر هم بفرستید.\n"
            "✨ *ربات تمامی این فایل‌ها را در قالب یک بسته متصل ذخیره کرده و در روز نوبت با هم و به ترتیب به دانش‌آموز تحویل می‌دهد.*\n\n"
            "👇 پس از پایان ارسال فایل‌های این پکیج، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "stage4_split_solo":
        split_count = database.split_stage_4_messages_to_solo(config.DB_PATH)
        tot_pkg = database.get_stage_4_package_count(config.DB_PATH)
        await safe_reply_text(
            query.message,
            f"✅ **عملیات تفکیک با موفقیت انجام شد!** 🟢✨\n\n"
            f"تعداد {split_count} فایل در بخش ۴ به **{tot_pkg} نوبت ارسالی مستقل** تبدیل شدند.\n"
            "از این پس در هر بار ارسال فقط یک فایل (به ترتیب و بدون ارسال مجدد ویس‌های قبلی) به دانش‌آموز ارسال خواهد شد.",
            parse_mode="Markdown",
            reply_markup=get_stage_4_options_keyboard(has_content=True)
        )

    elif data == "stage4_manual_menu":
        active_states[user_id] = None
        packages = database.get_stage_4_packages_summary(config.DB_PATH)
        if not packages:
            await safe_reply_text(
                query.message,
                "ℹ️ هیچ فایل یا پکیجی در بخش ۴ ذخیره نشده است.",
                parse_mode="Markdown",
                reply_markup=get_stage_4_options_keyboard(has_content=False)
            )
            return

        kb_rows = []
        for pkg in packages:
            idx = pkg["index"]
            pid = pkg["package_id"]
            cnt = pkg["count"]
            cap = (pkg.get("caption") or "")[:20]
            cap_part = f" - {cap}" if cap else ""
            label = f"📦 نوبت {idx} ({cnt} فایل){cap_part}" if cnt > 1 else f"🎙 نوبت {idx} (ویس تک){cap_part}"
            kb_rows.append([InlineKeyboardButton(label, callback_data=f"stage4_pick_{pid}", style="primary")])

        kb_rows.append([InlineKeyboardButton("🔙 بازگشت به منوی بخش ۴", callback_data="admin_record_4_menu", style="danger")])
        await safe_reply_text(
            query.message,
            "🎯 **انتخاب نوبت جهت ارسال دستی یا مشاهده جزئیات:**\n"
            "نوبت مورد نظر خود را از لیست زیر انتخاب نمایید:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb_rows)
        )

    elif data.startswith("stage4_pick_"):
        pid = int(data.split("_")[-1])
        packages = database.get_stage_4_packages_summary(config.DB_PATH)
        target_pkg = next((p for p in packages if p["package_id"] == pid), None)
        if not target_pkg:
            await safe_reply_text(query.message, "❌ نوبت مورد نظر یافت نشد.")
            return

        missing_students = database.get_students_missing_stage_4_package(config.DB_PATH, pid)
        idx = target_pkg["index"]
        cnt = target_pkg["count"]
        type_str = f"پکیج چندرسانه‌ای ({cnt} فایل)" if cnt > 1 else "ویس تک و مجزا"

        if missing_students:
            st_list_str = "\n".join([f"• {escape_md(s.get('assigned_name') or s.get('full_name'))}" for s in missing_students[:10]])
            if len(missing_students) > 10:
                st_list_str += f"\n... و {len(missing_students) - 10} نفر دیگر"
        else:
            st_list_str = "تمام دانش‌آموزان فاز ۴ این نوبت را قبلاً دریافت کرده‌اند ✅"

        detail_text = (
            f"🎯 **مشخصات نوبت شماره {idx} (کد پکیج: `{pid}`):**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• نوع محتوا: **{type_str}**\n"
            f"• تعداد فایل‌ها: **{cnt} فایل**\n\n"
            f"👥 **دانش‌آموزان واجد شرایطی که هنوز این نوبت را نگرفته‌اند ({len(missing_students)} نفر):**\n"
            f"{st_list_str}\n\n"
            "👇 جهت ارسال اختصاصی همین نوبت به افراد فوق یا پیش‌نمایش، دکمه مورد نظر را لمس کنید:"
        )

        buttons = []
        if missing_students:
            buttons.append([InlineKeyboardButton(f"🚀 ارسال این نوبت برای {len(missing_students)} دانش‌آموز واجد شرایط", callback_data=f"stage4_send_{pid}", style="success")])
        buttons.append([InlineKeyboardButton("👁 پیش‌نمایش فایل‌های این نوبت در پی‌وی من", callback_data=f"stage4_prevpkg_{pid}", style="primary")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت به لیست نوبت‌ها", callback_data="stage4_manual_menu", style="danger")])

        await safe_reply_text(query.message, detail_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("stage4_prevpkg_"):
        pid = int(data.split("_")[-1])
        packages = database.get_stage_4_packages_summary(config.DB_PATH)
        target_pkg = next((p for p in packages if p["package_id"] == pid), None)
        if not target_pkg:
            await safe_reply_text(query.message, "❌ نوبت یافت نشد.")
            return

        await safe_reply_text(query.message, f"⏳ در حال ارسال پیش‌نمایش نوبت شماره {target_pkg['index']} ({target_pkg['count']} فایل)...")
        for m in target_pkg["messages"]:
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=m["from_chat_id"],
                    message_id=m["message_id"]
                )
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Could not copy message {m['id']} for preview: {e}")

    elif data.startswith("stage4_send_"):
        pid = int(data.split("_")[-1])
        packages = database.get_stage_4_packages_summary(config.DB_PATH)
        target_pkg = next((p for p in packages if p["package_id"] == pid), None)
        if not target_pkg:
            await safe_reply_text(query.message, "❌ نوبت یافت نشد.")
            return

        missing_students = database.get_students_missing_stage_4_package(config.DB_PATH, pid)
        if not missing_students:
            await safe_reply_text(query.message, "ℹ️ تمامی دانش‌آموزان واجد شرایط قبلاً این نوبت را دریافت کرده‌اند.")
            return

        pkg_msgs = target_pkg["messages"]
        await safe_reply_text(query.message, f"⏳ در حال ارسال نوبت شماره {target_pkg['index']} به {len(missing_students)} دانش‌آموز...")
        sent_count = 0

        for st in missing_students:
            st_uid = st["user_id"]
            async with get_user_lock(st_uid):
                fresh_st = database.get_student(config.DB_PATH, st_uid)
                if not fresh_st or fresh_st.get("current_stage", 1) < 4:
                    continue

                unsent_msgs = [
                    m for m in pkg_msgs
                    if not database.is_message_delivered(
                        config.DB_PATH,
                        st_uid,
                        m["id"],
                        m.get("from_chat_id"),
                        m.get("message_id")
                    )
                ]
                if not unsent_msgs:
                    continue

                raw_name = fresh_st.get("assigned_name") or fresh_st.get("full_name") or "دانش‌آموز"
                name = escape_md(raw_name)

                try:
                    if len(unsent_msgs) > 1:
                        intro_text = (
                            f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎙🎬 **مبحث تکمیلی جدید دکتر هاشمی ({len(unsent_msgs)} فایل پیوسته) برای شما ارسال می‌شود:**\n\n"
                            "📌 لطفاً تمام فایل‌های این بسته آموزشی را با دقت و به ترتیب مشاهده کرده و گوش دهید 🎓"
                        )
                    else:
                        caption_text = f"\n\n{escape_md(unsent_msgs[0].get('caption'))}" if unsent_msgs[0].get("caption") else ""
                        intro_text = (
                            f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎙 **مبحث تکمیلی جدید از طرف دکتر هاشمی برای شما ارسال می‌شود:**{caption_text}"
                        )

                    await send_student_msg(context.bot, user_id=st_uid, text=intro_text, parse_mode="Markdown")

                    for m in unsent_msgs:
                        if database.is_message_delivered(
                            config.DB_PATH,
                            st_uid,
                            m["id"],
                            m.get("from_chat_id"),
                            m.get("message_id")
                        ):
                            continue
                        try:
                            await copy_student_msg(
                                context.bot,
                                user_id=st_uid,
                                from_chat_id=m["from_chat_id"],
                                message_id=m["message_id"],
                                content_type="voice"
                            )
                        finally:
                            database.record_drip_package_delivery(config.DB_PATH, st_uid, pid, [m["id"]])
                            database.record_message_delivery(
                                config.DB_PATH,
                                st_uid,
                                4,
                                m["id"],
                                m.get("from_chat_id"),
                                m.get("message_id")
                            )
                        await asyncio.sleep(0.3)

                    sent_count += 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"Error sending manual stage 4 package {pid} to {st_uid}: {e}")

        await safe_reply_text(
            query.message,
            f"🎉 **نوبت شماره {target_pkg['index']} با موفقیت برای {sent_count} دانش‌آموز ارسال شد!** 🟢✨\n"
            "(ارسال تکراری به صفر رسیده و هیچ پیامی مجدد فرستاده نشد)",
            parse_mode="Markdown",
            reply_markup=get_stage_4_options_keyboard(has_content=True)
        )

    # --- Intro / Red Lines Management ---
    elif data == "admin_intro_menu":
        active_states[user_id] = None
        intro = database.get_intro_content(config.DB_PATH)
        has_media = bool(intro and intro.get("message_id"))
        has_rules = bool(intro and intro.get("text_content"))
        media_st = "تنظیم شده (فایل اختصاصی) ✅" if has_media else "ثبت نشده ❌"
        rules_st = "متن اختصاصی شما ✅" if has_rules else "متن پیش‌فرض سیستم 📋"

        await safe_reply_text(
            query.message,
            "📜 **تنظیمات پیام خوش‌آمد و خط قرمزهای مشاوره:** 📌\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "این فرایند به صورت چندمرحله‌ای برای دانش‌آموز جدید ارسال می‌شود:\n"
            "۱. **گام اول:** متن خط قرمزها\n"
            "۲. **گام دوم:** ویس یا فایل اولیه دکتر هاشمی همراه با دکمه ورود به مرحله اول\n\n"
            f"• **وضعیت فایل/ویس معرفی:** {media_st}\n"
            f"• **وضعیت متن خط قرمزها:** {rules_st}\n\n"
            "جهت تنظیم، ویرایش یا پیش‌نمایش از دکمه‌های زیر استفاده کنید:",
            parse_mode="Markdown",
            reply_markup=get_intro_management_keyboard(has_media=has_media, has_rules=has_rules)
        )

    elif data == "admin_preview_intro":
        intro = database.get_intro_content(config.DB_PATH)
        has_media = bool(intro and intro.get("message_id"))
        rules_text = intro.get("text_content") if (intro and intro.get("text_content")) else (
            "📌 **توضیحات مهم و خط قرمزهای مسیر مشاوره (پیش‌فرض):**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "۱. **تعهد روزانه:** ویس‌ها و آموزش‌ها را با تمرکز کامل در همان روز گوش دهید 🌱\n"
            "۲. **ارسال دقیق گزارش کار:** مباحث خوانده‌شده و گزارش مطالعه را منظم ارسال کنید 📝\n"
            "۳. **استمرار و نظم:** این ربات همراه هوشمند شماست تا روز کنکور؛ با تعهد کامل قدم بردارید 🚀🎓"
        )

        await safe_reply_text(query.message, "👁 **پیش‌نمایش مرحله به مرحله پیام خوش‌آمدگویی:**", parse_mode="Markdown")
        if has_media:
            try:
                await context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=intro["from_chat_id"],
                    message_id=intro["message_id"]
                )
            except Exception as e:
                await safe_reply_text(query.message, f"خطا در ارسال فایل معرفی: {e}")
        else:
            await safe_reply_text(query.message, "ℹ️ *هنوز فایل یا ویس معرفی بارگذاری نشده است (اختیاری).*", parse_mode="Markdown")

        preview_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 ✅ مطالعه کردم و متعهدم | ورود به مرحله اول 🚀", callback_data="admin_menu", style="success")
        ]])
        await safe_reply_text(
            query.message,
            f"{rules_text}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 *دکمه شروع مرحله اول که دانش‌آموز می‌بیند:*",
            parse_mode="Markdown",
            reply_markup=preview_kb
        )

    elif data == "admin_edit_intro_media":
        active_states[user_id] = "AWAITING_INTRO_MEDIA"
        await safe_reply_text(
            query.message,
            "🎙 **بارگذاری ویس یا فایل چندرسانه‌ای معرفی:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "لطفاً ویس، ویدیو، عکس یا فایل صوتی مورد نظرتان را در همین چت ارسال یا فوروارد کنید.\n"
            "این فایل به محض ورود دانش‌آموز جدید برای او فرستاده می‌شود.\n\n"
            "👇 جهت انصراف دکمه زیر را لمس نمایید:",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "admin_edit_intro_rules":
        active_states[user_id] = "AWAITING_INTRO_RULES"
        await safe_reply_text(
            query.message,
            "📝 **تنظیم متن خط قرمزها و قوانین مشاوره:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "لطفاً متن قوانین را به صورت یک پیام متنی ارسال کنید (می‌توانید همینجا تایپ کنید یا فوروارد نمایید).\n\n"
            "👇 جهت انصراف دکمه زیر را لمس نمایید:",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "admin_clear_intro_media":
        database.clear_intro_media(config.DB_PATH)
        await safe_reply_text(
            query.message,
            "🗑 **ویس/فایل معرفی حذف شد.**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data == "admin_reset_intro_rules":
        database.clear_intro_rules(config.DB_PATH)
        await safe_reply_text(
            query.message,
            "🔄 **متن خط قرمزها به حالت پیش‌فرض بازنشانی شد.**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    # --- Broadcast Management ---
    elif data == "admin_broadcast_menu":
        active_states[user_id] = "AWAITING_BROADCAST_CONTENT"
        await safe_reply_text(
            query.message,
            "📢 **ارسال پیام همگانی برای تمامی دانش‌آموزان:** 📌\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 لطفاً پیام، ویس، ویدیو، عکس یا متن مورد نظر را در همین چت ارسال یا فوروارد نمایید.\n\n"
            "*(در مرحله بعد می‌توانید انتخاب کنید که پیام در پی‌وی دانش‌آموزان پین شود یا خیر)*\n"
            "👇 جهت انصراف دکمه زیر را لمس کنید:",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data in ("broadcast_confirm_pin", "broadcast_confirm_nopin"):
        should_pin = (data == "broadcast_confirm_pin")
        item = pending_broadcast.pop(user_id, None)
        if not item:
            await safe_reply_text(query.message, "❌ پیامی برای ارسال یافت نشد یا قبلاً ارسال شده است.")
            return

        students = database.list_students(config.DB_PATH)
        if not students:
            await safe_reply_text(query.message, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
            return

        await safe_reply_text(query.message, f"⏳ در حال ارسال پیام همگانی به {len(students)} دانش‌آموز... لطفاً شکیبا باشید.")
        sent_count = 0
        pin_count = 0

        for st in students:
            st_uid = st["user_id"]
            try:
                sent_msg = await copy_student_msg(
                    context.bot,
                    user_id=st_uid,
                    from_chat_id=item["from_chat_id"],
                    message_id=item["message_id"],
                    content_type="broadcast"
                )
                sent_count += 1
                if should_pin and sent_msg and getattr(sent_msg, "message_id", None):
                    try:
                        await context.bot.pin_chat_message(chat_id=st_uid, message_id=sent_msg.message_id)
                        pin_count += 1
                    except Exception as pe:
                        logger.warning(f"Could not pin message for user {st_uid}: {pe}")
                await asyncio.sleep(0.15)
            except Exception as e:
                logger.warning(f"Could not broadcast message to user {st_uid}: {e}")

        pin_status_str = f"📌 پین در پی‌وی فعال شد ({pin_count} پین موفق)" if should_pin else "بدون پین"
        await safe_reply_text(
            query.message,
            f"🎉 **پیام همگانی با موفقیت ارسال شد!** 🟢✨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• تعداد دریافت‌کنندگان موفق: **{sent_count} نفر** از {len(students)}\n"
            f"• وضعیت پین: **{pin_status_str}**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data == "broadcast_cancel":
        pending_broadcast.pop(user_id, None)
        active_states[user_id] = None
        await safe_reply_text(
            query.message,
            "❌ **ارسال پیام همگانی لغو شد.**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data.startswith("admin_record_"):
        stage_num = int(data.split("_")[-1])
        active_states[user_id] = f"RECORD_STAGE_{stage_num}"
        active_package_ids[user_id] = int(get_iran_now().timestamp())
        stage_title = get_stage_display_name(stage_num)

        await safe_reply_text(
            query.message,
            f"🎙 **حالت ذخیره‌سازی برای «{stage_title}» فعال شد.**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 هم‌اکنون پیام‌ها، ویس‌ها و فایل‌ها را به این چت فوروارد کنید.\n\n"
            "👇 پس از پایان ارسال فایل‌ها، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** در پایین صفحه را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "admin_force_milestones":
        await safe_reply_text(query.message, "⏳ در حال بررسی نوبت‌های ارسالی روز ۸ و ۳۰ دانش‌آموزان...")
        sent = await execute_milestones_check(context.bot)
        if sent > 0:
            await safe_reply_text(query.message, f"🎯 **موفق:** تعداد {sent} پکیج روز ۸ یا ۳۰ به دانش‌آموزان مربوطه ارسال شد.", parse_mode="Markdown")
        else:
            await safe_reply_text(query.message, "ℹ️ در حال حاضر دانش‌آموزی که موعد روز ۸ یا ۳۰ او رسیده باشد و محتوا را نگرفته باشد یافت نشد.")

    elif data == "admin_drip_settings":
        active_states[user_id] = None
        interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
        hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))

        msg = (
            "⚙️ **تنظیمات زمان‌بندی ارسال خودکار (بخش ۴):** ⚙️\n\n"
            f"• **فاصله زمانی فعلی:** هر {interval} روز یکبار 🗓\n"
            f"• **ساعت ارسال در روز نوبت:** ساعت {hour}:۰۰ شب ⏰\n\n"
            "💡 *دانش‌آموزانی که ۳ مرحله را تمام کرده‌اند، طبق این برنامه ویس‌های بخش ۴ را یکی پس از دیگری، به ترتیب و بدون تکرار دریافت می‌کنند.*"
        )
        await safe_reply_text(query.message, msg, parse_mode="Markdown", reply_markup=get_drip_settings_keyboard(interval, hour))

    elif data.startswith("drip_set_int_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_interval_days", val)
        await safe_reply_text(query.message, f"✅ فاصله ارسال به **هر {val} روز یکبار** تغییر یافت.", parse_mode="Markdown")

    elif data.startswith("drip_set_hour_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_send_hour", val)
        await safe_reply_text(query.message, f"✅ ساعت ارسال روزانه به **ساعت {val}:۰۰ شب** تغییر یافت.", parse_mode="Markdown")

    elif data == "admin_force_drip":
        await safe_reply_text(query.message, "⏳ در حال بررسی و ارسال نوبت ویس جدید به دانش‌آموزان واجد شرایط فاز ۴...")
        sent = await execute_drip_cycle(context.bot, force_ignore_interval=True)
        if sent > 0:
            await safe_reply_text(query.message, f"🚀 **موفق:** تعداد {sent} نوبت جدید (به ترتیب و بدون تکرار) برای دانش‌آموزان مرحله ۴ ارسال شد.", parse_mode="Markdown")
        else:
            await safe_reply_text(query.message, "ℹ️ دانش‌آموز واجد شرایط جدیدی یافت نشد (یا تمامی نوبت‌های موجود قبلاً ارسال شده‌اند).")

    # --- Study Plans Management ---
    elif data == "admin_plans_menu":
        active_states[user_id] = None
        students = database.list_students(config.DB_PATH)
        if not students:
            await safe_reply_text(query.message, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
            return

        await safe_reply_text(
            query.message,
            "🟡 🗓 **مدیریت برنامه‌های مطالعاتی دوره‌ای:**\n\n"
            "دانش‌آموز مورد نظر را انتخاب کنید تا برنامه جدید (۷، ۸، ۹ یا دلخواه) برایش فعال شود:",
            parse_mode="Markdown",
            reply_markup=get_student_list_for_plans_keyboard(students)
        )

    elif data.startswith("plan_user_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        if not st:
            await safe_reply_text(query.message, "❌ این دانش‌آموز حذف شده یا یافت نشد.")
            return
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(raw_name)
        active_plan = database.get_active_study_plan(config.DB_PATH, target_uid)

        plan_info = "در حال حاضر برنامه فعالی ندارد."
        if active_plan:
            dur = active_plan["duration_days"]
            status = escape_md(str(active_plan["status"]))
            plan_info = f"دارای برنامه **{dur} روزه** فعال (وضعیت: {status})"

        dossier = database.get_student_dossier(config.DB_PATH, target_uid)
        dossier_summary = ""
        if dossier:
            dossier_summary = f"\n📁 **خلاصه آخرین موارد پرونده دانش‌آموز ({len(dossier)} مورد ثبت‌شده):**\n"
            for n in dossier[-4:]:
                mtype = escape_md(n.get("media_type") or "متن")
                t_str = n.get("text_content") or n.get("caption") or f"فایل {mtype}"
                short_t = (t_str[:50] + "...") if len(t_str) > 50 else t_str
                dossier_summary += f"• [{mtype}] {escape_md(short_t)}\n"
            if len(dossier) > 4:
                dossier_summary += f"*(و {len(dossier) - 4} مورد قدیمی‌تر)*\n"

        await safe_reply_text(
            query.message,
            f"👤 **دانش‌آموز:** {safe_name}\n"
            f"📊 **وضعیت کنونی:** {plan_info}\n"
            f"{dossier_summary}\n"
            f"مدت برنامه جدید را برای «{safe_name}» انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_plan_duration_keyboard(target_uid)
        )

    elif data.startswith("plan_set_"):
        parts = data.split("_")
        target_uid = int(parts[2])
        days = int(parts[3])
        st = database.get_student(config.DB_PATH, target_uid)
        if not st:
            await safe_reply_text(query.message, "❌ این دانش‌آموز حذف شده یا یافت نشد.")
            return

        database.create_study_plan(config.DB_PATH, target_uid, days)
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(raw_name)

        await safe_reply_text(
            query.message,
            f"✅ **برنامه مطالعاتی {days} روزه با موفقیت برای «{safe_name}» فعال شد!** 🎯\n\n"
            f"• ۱ روز مانده به اتمام برنامه ({max(0, days - 1)} روز دیگر)، پیام هشدار جهت ارسال مباحث خوانده‌شده خودکار برای دانش‌آموز ارسال خواهد شد.",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

        try:
            await send_student_msg(
                context.bot,
                user_id=target_uid,
                text=f"🗓 **{safe_name} عزیز، برنامه درسی {days} روزه جدید شما از امروز فعال شد!** 🎯🌱\n\n"
                     "با تمرکز و دقت کامل شروع کنید 🚀\n"
                     f"🔔 یک روز مانده به پایان برنامه، پیام یادآوری جهت ارسال لیست مباحث خوانده‌شده برایتان ارسال خواهد شد.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not notify student of new plan: {e}")

    elif data.startswith("plan_custom_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        if not st:
            await safe_reply_text(query.message, "❌ این دانش‌آموز حذف شده یا یافت نشد.")
            return
        active_states[user_id] = f"AWAITING_CUSTOM_DAYS_{target_uid}"
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(raw_name)

        await safe_reply_text(
            query.message,
            f"✏️ **مدت برنامه برای «{safe_name}» را به عدد وارد کنید:**\n\n"
            "مثلاً بنویسید: `7` یا `8` یا `10`",
            parse_mode="Markdown"
        )

    # --- Student Dossier & Memory Management ---
    elif data == "admin_dossier_menu":
        active_states[user_id] = None
        students = database.list_students(config.DB_PATH)
        if not students:
            await safe_reply_text(query.message, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
            return

        await safe_reply_text(
            query.message,
            "🗂 👤 **پرونده و اطلاعات ذخیره‌شده دانش‌آموزان:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "هر پیامی (ویس، ویدیو، عکس، متن) که از چت دانش‌آموز به ربات فوروارد کنید، خودکار در پرونده او ذخیره می‌شود.\n\n"
            "جهت مشاهده یا ثبت یادداشت دستی، دانش‌آموز مورد نظر را انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_students_for_dossier_keyboard(students)
        )

    elif data.startswith("dossier_select_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        if not st:
            await safe_reply_text(query.message, "❌ دانش‌آموز یافت نشد.")
            return
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(name)
        cnt = database.get_student_dossier_count(config.DB_PATH, target_uid)
        st_info = (
            f"👤 **پرونده تحصیلی «{safe_name}»**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• وضعیت مرحله: **مرحله {st['current_stage']}**\n"
            f"• تعداد کل یادداشت‌ها و فایل‌های ثبت‌شده: **{cnt} مورد**\n\n"
            "عملیات مورد نظر را انتخاب کنید:"
        )
        await safe_reply_text(
            query.message,
            st_info,
            parse_mode="Markdown",
            reply_markup=get_dossier_actions_keyboard(target_uid, has_notes=(cnt > 0))
        )

    elif data.startswith("dossier_view_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        name = (st.get("assigned_name") or st.get("full_name") or "دانش‌آموز") if st else "دانش‌آموز"
        safe_name = escape_md(name)
        notes = database.get_student_dossier(config.DB_PATH, target_uid)

        if not notes:
            await safe_reply_text(query.message, f"ℹ️ پرونده «{safe_name}» در حال حاضر خالی است.")
            return

        await safe_reply_text(
            query.message,
            f"⏳ در حال ارسال محتوای پرونده «{safe_name}» ({len(notes)} مورد)...",
            parse_mode="Markdown"
        )

        for idx, n in enumerate(notes, 1):
            mtype = escape_md(n.get("media_type") or "text")
            date_str = str(n.get("created_at") or "")[:16]
            if n.get("from_chat_id") and n.get("message_id"):
                try:
                    await safe_bot_send_message(
                        context.bot,
                        chat_id=user_id,
                        text=f"📌 **مورد شماره {idx} ({mtype}) - ثبت: `{date_str}`:**",
                        parse_mode="Markdown"
                    )
                    await context.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=n["from_chat_id"],
                        message_id=n["message_id"]
                    )
                    await asyncio.sleep(0.3)
                except Exception as e:
                    await safe_reply_text(query.message, f"خطا در ارسال فایل شماره {idx}: {e}")
            elif n.get("text_content"):
                t_content = escape_md(n.get("text_content"))
                await safe_bot_send_message(
                    context.bot,
                    chat_id=user_id,
                    text=f"📝 **یادداشت شماره {idx} - ثبت: `{date_str}`:**\n\n{t_content}",
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.2)

        await safe_reply_text(
            query.message,
            f"✅ **پایان محتوای پرونده «{safe_name}»**",
            parse_mode="Markdown",
            reply_markup=get_dossier_actions_keyboard(target_uid, has_notes=True)
        )

    elif data.startswith("dossier_addmanual_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        if not st:
            await safe_reply_text(query.message, "❌ دانش‌آموز یافت نشد.")
            return
        active_states[user_id] = f"AWAITING_MANUAL_DOSSIER_{target_uid}"
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(name)
        await safe_reply_text(
            query.message,
            f"📝 **ثبت یادداشت دستی برای «{safe_name}»:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "لطفاً متن یادداشت خود را (مثلاً وضعیت درسی، ضعف‌ها یا نقاط قوت) در این چت ارسال نمایید.\n\n"
            "👇 جهت انصراف دکمه زیر را لمس کنید:",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data.startswith("dossier_clear_"):
        target_uid = int(data.split("_")[-1])
        database.clear_student_dossier(config.DB_PATH, target_uid)
        await safe_reply_text(
            query.message,
            "🗑 **پرونده این دانش‌آموز با موفقیت کامل پاکسازی شد.** 🟢",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data.startswith("dossier_del_"):
        note_id = int(data.split("_")[-1])
        database.delete_dossier_note(config.DB_PATH, note_id)
        await safe_reply_text(query.message, "🗑 **این مورد از پرونده دانش‌آموز حذف شد.** 🟢", parse_mode="Markdown")

    elif data.startswith("dossier_assign_"):
        if data == "dossier_assign_cancel":
            pending_dossier_notes.pop(user_id, None)
            await safe_reply_text(query.message, "❌ عملیات ذخیره لغو شد.", reply_markup=get_admin_reply_keyboard())
        else:
            target_uid = int(data.split("_")[-1])
            item = pending_dossier_notes.pop(user_id, None)
            if not item:
                await safe_reply_text(query.message, "❌ داده مورد نظر منقضی شده یا یافت نشد.")
            else:
                note_id = database.add_student_dossier_note(
                    config.DB_PATH,
                    user_id=target_uid,
                    from_chat_id=item.get("from_chat_id"),
                    message_id=item.get("message_id"),
                    media_type=item.get("media_type", "text"),
                    caption=item.get("caption"),
                    text_content=item.get("text_content")
                )
                st = database.get_student(config.DB_PATH, target_uid)
                name = (st.get("assigned_name") or st.get("full_name") or "دانش‌آموز") if st else "دانش‌آموز"
                safe_name = escape_md(name)
                tot = database.get_student_dossier_count(config.DB_PATH, target_uid)

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👁 مشاهده کامل پرونده", callback_data=f"dossier_view_{target_uid}", style="success")],
                    [InlineKeyboardButton("↩️ حذف این پیام از پرونده", callback_data=f"dossier_del_{note_id}", style="danger")],
                    [InlineKeyboardButton("👑 منوی اصلی مدیریت", callback_data="admin_menu", style="primary")]
                ])
                await safe_reply_text(
                    query.message,
                    f"📁 **پیام با موفقیت در پرونده «{safe_name}» ذخیره شد!** ✅\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"• نوع محتوا: **{escape_md(item.get('media_type', 'text'))}**\n"
                    f"• مجموع موارد پرونده: **{tot} مورد**",
                    parse_mode="Markdown",
                    reply_markup=kb
                )

    elif data == "admin_stats":
        active_states[user_id] = None
        report = generate_students_stats_report()
        await safe_reply_text(query.message, report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())

    elif data == "admin_download_db":
        active_states[user_id] = None
        await safe_reply_text(query.message, "⏳ در حال تهیه نسخه پشتیبان کامل از دیتابیس...")
        await send_database_backup_file(context.bot, user_id)

# --- Student Callback Handler ---

async def student_callback_handler(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if query.message and getattr(query.message, "message_id", None):
        database.record_student_sent_message(config.DB_PATH, user_id, query.message.message_id, content_type="callback_msg")

    if database.is_student_removed(config.DB_PATH, user_id):
        await query.answer(REMOVAL_NOTICE_TEXT, show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            try:
                await query.edit_message_text(REMOVAL_NOTICE_TEXT, reply_markup=None)
            except Exception:
                try:
                    await query.edit_message_caption(REMOVAL_NOTICE_TEXT, reply_markup=None)
                except Exception:
                    pass
        return

    async with get_user_lock(user_id):
        student = database.get_student(config.DB_PATH, user_id)
        if not student:
            await query.answer("دسترسی شما یافت نشد.", show_alert=True)
            return

        await query.answer()
        data = query.data
        raw_name = student.get("assigned_name") or student.get("full_name") or "دانش‌آموز"
        safe_name = escape_md(raw_name)
        curr_stage = student.get("current_stage", 1)

        if data == "student_start_stage_1":
            # Prevent resetting stage or re-sending if already past stage 1
            if curr_stage > 1:
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=f"ℹ️ **{safe_name} عزیز، شما قبلاً مرحله اول را گذرانده‌اید و اکنون در مرحله {curr_stage} هستید.**",
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(curr_stage)
                )
                return

            # If Stage 1 messages were already delivered (e.g. student double-clicked the start stage 1 button), don't duplicate the header
            st1_msgs = database.get_stage_messages(config.DB_PATH, 1)
            already_started_st1 = bool(st1_msgs) and all(
                database.is_message_delivered(config.DB_PATH, user_id, m["id"], m.get("from_chat_id"), m.get("message_id"))
                for m in st1_msgs
            )

            database.update_student_stage(config.DB_PATH, user_id, 1)
            if not already_started_st1:
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=(
                        f"👏 **آفرین {safe_name} عزیز! ورود به مرحله اول با موفقیت ثبت شد.** 🌱✨\n"
                        "فایل‌ها و ویس‌های این مرحله برای شما ارسال می‌شوند:"
                    ),
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(1)
                )
            await _deliver_stage_content_unlocked(context.bot, user_id, stage=1, next_stage=2, greeting_name=raw_name)
            return

        elif data.startswith("student_goto_"):
            next_stage = int(data.split("_")[-1])
            # Enforce strict sequential stage progression (no duplicate stage entries, no jumping backwards)
            if next_stage <= curr_stage:
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=f"ℹ️ **{safe_name} عزیز، شما قبلاً وارد این مرحله شده‌اید.**",
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(curr_stage)
                )
                return
            if next_stage > curr_stage + 1:
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=f"⚠️ لطفاً ابتدا **مرحله {curr_stage}** را به پایان برسانید.",
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(curr_stage)
                )
                return

            database.update_student_stage(config.DB_PATH, user_id, next_stage)

            if next_stage <= 3:
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=(
                        f"👏 **آفرین {safe_name} عزیز! مرحله {next_stage} برات باز شد.** 🌱\n"
                        "فایل‌های این مرحله ارسال می‌شوند:"
                    ),
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(next_stage)
                )
                await _deliver_stage_content_unlocked(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1, greeting_name=raw_name)
            else:
                completion_msg = (
                    "🎉 **تبریک ویژه! شما هر ۳ مرحله اولیه آموزش را با موفقیت به پایان رساندید.** 🏆✨\n\n"
                    "📌 **حتماً این صفحه و چت را پین (Pin) کنید تا همیشه در دسترستان باشد.**\n\n"
                    "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب، ویس‌ها و نکات آموزشی جدید در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** 🌱"
                )
                await reply_student_msg(
                    query.message,
                    user_id=user_id,
                    text=completion_msg,
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(4)
                )

        elif data.startswith("student_resend_stage_"):
            st_num = int(data.split("_")[-1])
            await _deliver_stage_content_unlocked(context.bot, user_id, stage=st_num, next_stage=st_num + 1, greeting_name=raw_name)

        elif data == "student_start_report":
            active_states[user_id] = "AWAITING_STUDY_REPORT"
            await reply_student_msg(
                query.message,
                user_id=user_id,
                text=(
                    f"📝 **{safe_name} عزیز، لطفاً لیست مباحثی که تا امروز مطالعه کرده‌اید را تایپ کنید و بفرستید:**\n\n"
                    "💡 *مثال:* زیست: گفتار ۱ فصل ۳ تمام شد و ۵۰ تست زدم. شیمی: تا صفحه ۴۰ خوانده شد و ویدیو تحلیل رو دیدم."
                ),
                parse_mode="Markdown"
            )

# --- Admin & Student Message & Button Collector ---

async def message_collector(update, context):
    if not update or not update.message:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    msg = update.message
    text = (msg.text or "").strip()

    # ==========================
    # 1. ADMIN ACTIONS & BUTTONS
    # ==========================
    if is_admin(user_id, username):
        curr_admin_state = active_states.get(user_id) or ""
        is_recording_media = (
            curr_admin_state.startswith("RECORD_STAGE_")
            or curr_admin_state in ("AWAITING_INTRO_MEDIA", "AWAITING_BROADCAST_CONTENT")
        )
        if msg.document:
            file_name = (msg.document.file_name or "").lower()
            is_db_ext = file_name.endswith((".db", ".sqlite", ".sqlite3", ".bak", ".sql"))
            if is_db_ext and not is_recording_media:
                await safe_reply_text(msg, "⏳ در حال دریافت و بررسی فایل پشتیبان دیتابیس برای بازیابی...")
                try:
                    doc_file = await msg.document.get_file()
                    dir_name = os.path.dirname(config.DB_PATH) or "."
                    temp_restore_path = os.path.join(dir_name, "temp_restore.db")
                    await doc_file.download_to_drive(custom_path=temp_restore_path)

                    success = database.restore_database_backup(temp_restore_path, config.DB_PATH)
                    if os.path.exists(temp_restore_path):
                        os.remove(temp_restore_path)

                    if success:
                        database.init_db(config.DB_PATH)
                        st_count = len(database.list_students(config.DB_PATH))
                        c1 = len(database.get_stage_messages(config.DB_PATH, 1))
                        c2 = len(database.get_stage_messages(config.DB_PATH, 2))
                        c3 = len(database.get_stage_messages(config.DB_PATH, 3))
                        pkg4 = database.get_stage_4_package_count(config.DB_PATH)
                        await safe_reply_text(
                            msg,
                            "🎉 **فایل پشتیبان دیتابیس با موفقیت کامل بازیابی و جایگزین شد!** 🟢✨\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"👥 دانش‌آموزان بازیابی‌شده: **{st_count} نفر**\n"
                            f"🎓 مرحله ۱: **{c1} فایل** | مرحله ۲: **{c2} فایل** | مرحله ۳: **{c3} فایل**\n"
                            f"💡 بخش ۴: **{pkg4} نوبت/بسته آموزشی**\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "✅ تمام اطلاعات، پرونده‌ها، برنامه‌های درسی و تنظیمات بدون هیچ نقصی فعال شدند 🚀",
                            parse_mode="Markdown",
                            reply_markup=get_admin_reply_keyboard()
                        )
                    else:
                        await safe_reply_text(msg, "❌ خطا در بازیابی دیتابیس. فایل ارسالی یک دیتابیس معتبر SQLite نیست.")
                except Exception as e:
                    logger.error(f"Error restoring db from document: {e}")
                    await safe_reply_text(msg, f"❌ خطا در پردازش فایل پشتیبان دیتابیس: {e}")
                return

        # A) Stop recording button (checked first)
        if "اتمام ذخیره پیام‌ها" in text or "انصراف و بازگشت" in text:
            curr_state = active_states.get(user_id) or ""
            active_states[user_id] = None
            pkg_id = active_package_ids.pop(user_id, None)

            if curr_state in ("AWAITING_INTRO_CONTENT", "AWAITING_INTRO_MEDIA", "AWAITING_INTRO_RULES", "AWAITING_BROADCAST_CONTENT", "AWAITING_DELETE_STUDENT_NAME") or curr_state.startswith("AWAITING_MANUAL_DOSSIER_"):
                pending_broadcast.pop(user_id, None)
                pending_dossier_notes.pop(user_id, None)
                await safe_reply_text(
                    msg,
                    "❌ **عملیات لغو شد و به منوی اصلی مدیریت بازگشتید.**",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
                return

            if curr_state == "RECORD_STAGE_4_SOLO":
                tot_pkg = database.get_stage_4_package_count(config.DB_PATH)
                c4 = len(database.get_stage_messages(config.DB_PATH, 4))
                await safe_reply_text(
                    msg,
                    "🎉 **ویس‌های تک و مجزای بخش ۴ با موفقیت ذخیره شدند!** 🟢✨\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 مجموع کل نوبت‌های آماده بخش ۴: **{tot_pkg} نوبت** ({c4} فایل)\n\n"
                    "💡 *هر یک از این ویس‌ها به عنوان یک نوبت مستقل ذخیره شدند و در هر نوبت ارسال، به ترتیب و بدون تکرار به دانش‌آموز تحویل داده می‌شوند.* 🚀",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
                return
            elif curr_state in ("RECORD_STAGE_4_PACKAGE", "RECORD_STAGE_4"):
                tot_pkg = database.get_stage_4_package_count(config.DB_PATH)
                conn = database.get_connection(config.DB_PATH)
                try:
                    cur = conn.execute("SELECT COUNT(*) as cnt FROM stage_messages WHERE package_id = ?", (pkg_id,))
                    pkg_cnt = cur.fetchone()["cnt"] if pkg_id else 0
                finally:
                    conn.close()

                await safe_reply_text(
                    msg,
                    "🎉 **بسته چندرسانه‌ای بخش ۴ با موفقیت ذخیره شد!** 🟢✨\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 تعداد فایل‌های این بسته: **{pkg_cnt} فایل** (ویدیو/عکس + ویس)\n"
                    f"📊 مجموع کل بسته‌های آماده بخش ۴: **{tot_pkg} بسته**\n\n"
                    "💡 *در روز نوبت ارسال، تمامی فایل‌های این بسته به صورت خودکار، به ترتیب و بدون تکرار برای دانش‌آموز ارسال خواهد شد.* 🚀",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
                return
            else:
                stage_num = int(curr_state.split("_")[-1]) if ("RECORD_STAGE_" in curr_state and curr_state.split("_")[-1].isdigit()) else 1
                st_title = get_stage_display_name(stage_num)
                count = len(database.get_stage_messages(config.DB_PATH, stage_num))
                await safe_reply_text(
                    msg,
                    f"✅ **عملیات ثبت محتوا برای «{st_title}» به پایان رسید.** 🟢\n"
                    f"مجموع پیام‌های ذخیره شده در این مرحله: {count} پیام.\n\n"
                    "به منوی اصلی مدیریت بازگشتید:",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
                return

        # B) Bottom Keyboard navigation for Admin (checked BEFORE state inputs so menu buttons always work)
        if "ساخت لینک جدید" in text:
            active_states[user_id] = "AWAITING_STUDENT_NAME"
            await safe_reply_text(
                msg,
                "📝 **لطفاً نام دانش‌آموز جدید را تایپ کنید:**\n\n"
                "مثلاً بنویسید: `نگار` یا `علی` یا `ساچلی`\n"
                "(پیام دعوت آماده فوروارد برای دانش‌آموز ساخته خواهد شد 🌸)",
                parse_mode="Markdown"
            )
            return

        elif "حذف دانش‌آموز ❌" in text:
            students = database.list_students(config.DB_PATH)
            if not students:
                active_states[user_id] = None
                await safe_reply_text(
                    msg,
                    "ℹ️ در حال حاضر هیچ دانش‌آموزی در لیست شما وجود ندارد.",
                    reply_markup=get_admin_reply_keyboard()
                )
                return
            active_states[user_id] = "AWAITING_DELETE_STUDENT_NAME"
            await safe_reply_text(
                msg,
                "🗑 **حذف کامل دانش‌آموز:**\n\n"
                "لطفاً نام (کامل یا ناقص) دانش‌آموز مورد نظر را تایپ کنید، یا از لیست زیر انتخاب نمایید:",
                parse_mode="Markdown",
                reply_markup=get_delete_students_keyboard(students)
            )
            return

        elif any(k in text for k in ("مدیریت و مشاهده", "بارگذاری محتوا", "محتوای مراحل", "مدیریت مراحل")):
            active_states[user_id] = None
            active_package_ids.pop(user_id, None)
            await safe_reply_text(
                msg,
                "🗂 **مدیریت و مشاهده محتوای مراحل:** 📁\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "جهت مشاهده فایل‌های ذخیره‌شده هر مرحله، افزودن فایل جدید یا پاکسازی، مرحله مورد نظر را انتخاب کنید:",
                parse_mode="Markdown",
                reply_markup=get_stages_management_keyboard()
            )
            return

        elif "افزودن فایل به بخش ۴" in text:
            active_states[user_id] = None
            active_package_ids.pop(user_id, None)
            c4 = len(database.get_stage_messages(config.DB_PATH, 4))
            pkg4 = database.get_stage_4_package_count(config.DB_PATH)
            await safe_reply_text(
                msg,
                "🟣 💡 **مدیریت و بارگذاری محتوای «بخش ۴»:**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 وضعیت فعلی: **{pkg4} نوبت/بسته** ({c4} فایل ذخیره)\n\n"
                "لطفاً نحوه ذخیره‌سازی محتوا را انتخاب کنید:\n"
                "• 🎙 **ویس تک و مجزا:** مناسب زمانی که ویس‌های تکی دارید و می‌خواهید هر کدام در یک روز/نوبت مجزا فرستاده شوند\n"
                "• 📦 **پکیج چندرسانه‌ای:** مناسب زمانی که می‌خواهید یک ویدیو یا عکس همراه با ویس توضیحات آن در یک روز با هم فرستاده شوند\n"
                "• 🎯 **انتخاب و ارسال دستی:** انتخاب و ارسال یک نوبت خاص به افراد واجد شرایط",
                parse_mode="Markdown",
                reply_markup=get_stage_4_options_keyboard(has_content=(c4 > 0))
            )
            return

        elif "ارسال پیام همگانی" in text:
            active_states[user_id] = "AWAITING_BROADCAST_CONTENT"
            await safe_reply_text(
                msg,
                "📢 **ارسال پیام همگانی برای تمامی دانش‌آموزان:** 📌\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📥 لطفاً پیام، ویس، ویدیو، عکس یا متن مورد نظر را در همین چت ارسال یا فوروارد نمایید.\n\n"
                "*(در مرحله بعد می‌توانید انتخاب کنید که پیام در پی‌وی دانش‌آموزان پین شود یا خیر)*\n"
                "👇 جهت انصراف دکمه زیر را لمس کنید:",
                parse_mode="Markdown",
                reply_markup=get_recording_reply_keyboard()
            )
            return

        elif "پیام خوش‌آمد و خط قرمزها" in text:
            active_states[user_id] = None
            intro = database.get_intro_content(config.DB_PATH)
            has_media = bool(intro and intro.get("message_id"))
            has_rules = bool(intro and intro.get("text_content"))
            media_st = "تنظیم شده (فایل اختصاصی) ✅" if has_media else "ثبت نشده ❌"
            rules_st = "متن اختصاصی شما ✅" if has_rules else "متن پیش‌فرض سیستم 📋"

            await safe_reply_text(
                msg,
                "📜 **تنظیمات پیام خوش‌آمد و خط قرمزهای مشاوره:** 📌\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "این فرایند به صورت چندمرحله‌ای برای دانش‌آموز جدید ارسال می‌شود:\n"
                "۱. **گام اول:** متن خط قرمزها\n"
                "۲. **گام دوم:** ویس یا فایل اولیه دکتر هاشمی همراه با دکمه ورود به مرحله اول\n\n"
                f"• **وضعیت فایل/ویس معرفی:** {media_st}\n"
                f"• **وضعیت متن خط قرمزها:** {rules_st}\n\n"
                "جهت تنظیم، ویرایش یا پیش‌نمایش از دکمه‌های زیر استفاده کنید:",
                parse_mode="Markdown",
                reply_markup=get_intro_management_keyboard(has_media=has_media, has_rules=has_rules)
            )
            return

        elif "پرونده و یادداشت‌های دانش‌آموزان" in text:
            active_states[user_id] = None
            students = database.list_students(config.DB_PATH)
            if not students:
                await safe_reply_text(msg, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
                return

            await safe_reply_text(
                msg,
                "🗂 👤 **پرونده و اطلاعات ذخیره‌شده دانش‌آموزان:**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "جهت مشاهده محتویات پرونده یا ثبت یادداشت دستی، دانش‌آموز مورد نظر را انتخاب کنید:\n"
                "*(همچنین می‌توانید مستقیماً هر پیامی از دانش‌آموز را به این چت فوروارد کنید تا ذخیره شود)*",
                parse_mode="Markdown",
                reply_markup=get_students_for_dossier_keyboard(students)
            )
            return

        elif "محتوای روز ۸ و ۳۰" in text:
            active_states[user_id] = None
            c8 = len(database.get_stage_messages(config.DB_PATH, 8))
            c30 = len(database.get_stage_messages(config.DB_PATH, 30))
            msg_text = (
                "🟠 🎯 **مدیریت محتوای روز ۸ و ۳۰ مشاوره:** 🏆\n\n"
                f"• فایل‌های ذخیره‌شده برای روز ۸: **{c8} پیام 🎯**\n"
                f"• فایل‌های ذخیره‌شده برای روز ۳۰: **{c30} پیام 🏆**\n\n"
                "برای بارگذاری یا ارسال دستی، دکمه مورد نظر را انتخاب کنید:"
            )
            await safe_reply_text(msg, msg_text, parse_mode="Markdown", reply_markup=get_milestones_keyboard())
            return

        elif "برنامه درسی دوره‌ای" in text:
            active_states[user_id] = None
            students = database.list_students(config.DB_PATH)
            if not students:
                await safe_reply_text(msg, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
                return

            await safe_reply_text(
                msg,
                "🟡 🗓 **مدیریت برنامه‌های مطالعاتی دوره‌ای:**\n\n"
                "دانش‌آموز مورد نظر را انتخاب کنید تا برنامه جدید (۷، ۸، ۹ یا دلخواه) برایش فعال شود:",
                parse_mode="Markdown",
                reply_markup=get_student_list_for_plans_keyboard(students)
            )
            return

        elif "تنظیمات ارسال خودکار" in text:
            active_states[user_id] = None
            interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
            hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))
            msg_text = (
                "⚙️ ⏱ **تنظیمات زمان‌بندی ارسال خودکار (بخش ۴):** ⚙️\n\n"
                f"• **فاصله زمانی فعلی:** هر {interval} روز یکبار 🗓\n"
                f"• **ساعت ارسال در روز نوبت:** ساعت {hour}:۰۰ شب ⏰\n\n"
                "برای تغییر فاصله یا ساعت، از دکمه‌های زیر استفاده کنید:"
            )
            await safe_reply_text(msg, msg_text, parse_mode="Markdown", reply_markup=get_drip_settings_keyboard(interval, hour))
            return

        elif "آمار و وضعیت دانش‌آموزان" in text:
            active_states[user_id] = None
            report = generate_students_stats_report()
            await safe_reply_text(msg, report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
            return

        elif "تست جمله انگیزشی" in text:
            active_states[user_id] = None
            await safe_reply_text(msg, "⏳ در حال آماده‌سازی و ارسال نمونه پیام انگیزشی ساعت ۷ صبح...")
            await execute_morning_quotes_cycle(context.bot, target_user_id=user_id)
            return

        elif "دریافت بکاپ دیتابیس" in text:
            active_states[user_id] = None
            await safe_reply_text(msg, "⏳ در حال آماده‌سازی و ارسال نسخه پشتیبان دیتابیس...")
            await send_database_backup_file(context.bot, user_id)
            return

        elif "منوی اصلی مدیریت" in text:
            active_states[user_id] = None
            active_package_ids.pop(user_id, None)
            await safe_reply_text(
                msg,
                "👑 **منوی اصلی مدیریت ربات مشاوره دکتر هاشمی:**",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

        state = active_states.get(user_id)

        # C1) Natural language chat command for deleting a student (when not recording content)
        if not is_recording_media and state not in ("AWAITING_STUDENT_NAME", "AWAITING_INTRO_RULES") and not (state and state.startswith("AWAITING_MANUAL_DOSSIER_")):
            is_del, del_name = parse_admin_delete_intent(text)
            if is_del:
                if del_name:
                    await handle_admin_delete_student_query(msg, context.bot, user_id, del_name)
                else:
                    students = database.list_students(config.DB_PATH)
                    if not students:
                        active_states[user_id] = None
                        await safe_reply_text(
                            msg,
                            "ℹ️ در حال حاضر هیچ دانش‌آموزی در لیست شما وجود ندارد.",
                            reply_markup=get_admin_reply_keyboard()
                        )
                    else:
                        active_states[user_id] = "AWAITING_DELETE_STUDENT_NAME"
                        await safe_reply_text(
                            msg,
                            "🗑 **حذف کامل دانش‌آموز:**\n\n"
                            "لطفاً نام (کامل یا ناقص) دانش‌آموز مورد نظر را تایپ کنید، یا از لیست زیر انتخاب نمایید:",
                            parse_mode="Markdown",
                            reply_markup=get_delete_students_keyboard(students)
                        )
                return

        # C2) State: Waiting for student name to DELETE
        if state == "AWAITING_DELETE_STUDENT_NAME" and text:
            _, extracted = parse_admin_delete_intent(text)
            target_query = extracted if extracted else text
            await handle_admin_delete_student_query(msg, context.bot, user_id, target_query)
            return

        # C3) State: Waiting for student name to CREATE INVITE LINK
        if state == "AWAITING_STUDENT_NAME":
            await create_and_send_invite_link(msg, context.bot, user_id, text)
            return

        # D1) State: Waiting for intro media
        if state == "AWAITING_INTRO_MEDIA":
            active_states[user_id] = None
            caption = msg.caption or (text if text else None)
            database.set_intro_media(
                config.DB_PATH,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=caption
            )
            await safe_reply_text(
                msg,
                "🎉 **ویس/فایل معرفی دکتر هاشمی با موفقیت ذخیره شد!** 🟢✨\n"
                "از این پس هنگام ورود دانش‌آموز جدید با لینک اختصاصی، این فایل برای او ارسال خواهد شد.",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

        # D2) State: Waiting for intro rules text
        if state == "AWAITING_INTRO_RULES":
            active_states[user_id] = None
            rules_text = text if text else (msg.caption or "")
            database.set_intro_rules(config.DB_PATH, rules_text)
            await safe_reply_text(
                msg,
                "🎉 **متن خط قرمزها و قوانین مشاوره با موفقیت ذخیره شد!** 🟢✨\n"
                "این متن در بدو ورود دانش‌آموز به ربات به او نمایش داده می‌شود.",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

        # D3) State: Waiting for manual dossier text note
        if state and state.startswith("AWAITING_MANUAL_DOSSIER_"):
            target_uid = int(state.split("_")[-1])
            active_states[user_id] = None
            st = database.get_student(config.DB_PATH, target_uid)
            raw_name = (st.get("assigned_name") or st.get("full_name") or "دانش‌آموز") if st else "دانش‌آموز"
            safe_name = escape_md(raw_name)

            database.add_student_dossier_note(
                config.DB_PATH,
                user_id=target_uid,
                from_chat_id=None,
                message_id=None,
                media_type="text",
                caption=None,
                text_content=text
            )
            tot = database.get_student_dossier_count(config.DB_PATH, target_uid)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👁 مشاهده کامل پرونده", callback_data=f"dossier_view_{target_uid}", style="success")],
                [InlineKeyboardButton("👑 منوی اصلی مدیریت", callback_data="admin_menu", style="primary")]
            ])
            await safe_reply_text(
                msg,
                f"📝 **یادداشت دستی در پرونده «{safe_name}» ثبت شد!** 🟢✨\n"
                f"📊 مجموع موارد پرونده: **{tot} مورد**",
                parse_mode="Markdown",
                reply_markup=kb
            )
            return

        # E) State: Waiting for broadcast content
        if state == "AWAITING_BROADCAST_CONTENT":
            pending_broadcast[user_id] = {
                "from_chat_id": msg.chat_id,
                "message_id": msg.message_id,
                "text": text,
                "caption": msg.caption
            }
            active_states[user_id] = None
            await safe_reply_text(
                msg,
                "📥 **پیام همگانی با موفقیت دریافت شد!**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "آیا تمایل دارید این پیام در صفحه گفتگوی (پی‌وی) تمامی دانش‌آموزان به صورت **سنجاق‌شده (Pin)** نمایش داده شود؟\n\n"
                "👇 لطفاً یکی از گزینه‌های زیر را انتخاب نمایید:",
                parse_mode="Markdown",
                reply_markup=get_broadcast_confirm_keyboard()
            )
            return

        # F) State: Custom days input for study plan
        if state and state.startswith("AWAITING_CUSTOM_DAYS_"):
            target_uid = int(state.split("_")[-1])
            clean_num_str = database.convert_digits_to_english(text).strip()
            m_num = re.search(r"\d+", clean_num_str)
            if not m_num:
                await safe_reply_text(
                    msg,
                    "⚠️ لطفاً تعداد روز را به صورت یک عدد (مثلاً `7` یا `10`) وارد کنید:",
                    parse_mode="Markdown"
                )
                return
            days = max(1, min(365, int(m_num.group(0))))
            active_states[user_id] = None

            st = database.get_student(config.DB_PATH, target_uid)
            if not st:
                await safe_reply_text(msg, "❌ این دانش‌آموز حذف شده یا یافت نشد.", reply_markup=get_admin_reply_keyboard())
                return

            database.create_study_plan(config.DB_PATH, target_uid, days)
            raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
            name = escape_md(raw_name)

            await safe_reply_text(
                msg,
                f"✅ **برنامه {days} روزه با موفقیت برای «{name}» فعال شد!** 🎯\n"
                f"• ۱ روز مانده به پایان برنامه ({max(0, days - 1)} روز بعد)، پیام یادآوری مباحث به دانش‌آموز ارسال خواهد شد.",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )

            try:
                await send_student_msg(
                    context.bot,
                    user_id=target_uid,
                    text=f"🗓 **{name} عزیز، برنامه درسی {days} روزه جدید شما فعال شد!** 🎯🌱\n"
                         "با قدرت شروع کنید 🚀",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            return

        # G) State: Recording forwarded messages for a stage
        if state and state.startswith("RECORD_STAGE_"):
            file_uid = extract_media_unique_id(msg)
            if state == "RECORD_STAGE_4_SOLO":
                caption = msg.caption or (text[:50] if text else None)
                database.add_stage_message(
                    config.DB_PATH,
                    stage=4,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption,
                    package_id=None,
                    file_unique_id=file_uid
                )
                tot_pkg = database.get_stage_4_package_count(config.DB_PATH)
                await safe_reply_text(
                    msg,
                    f"✅ **ویس به عنوان نوبت مستقل جدید در بخش ۴ ذخیره شد!** 🟢\n"
                    f"📊 کل نوبت‌های بخش ۴ تاکنون: **{tot_pkg} نوبت**\n\n"
                    "*(می‌توانید ویس‌های بعدی را هم فوروارد کنید یا دکمه اتمام ذخیره را بزنید)*",
                    parse_mode="Markdown",
                    reply_markup=get_recording_reply_keyboard()
                )
                return

            elif state in ("RECORD_STAGE_4_PACKAGE", "RECORD_STAGE_4"):
                pkg_id = active_package_ids.get(user_id)
                if not pkg_id:
                    pkg_id = int(get_iran_now().timestamp())
                    active_package_ids[user_id] = pkg_id

                caption = msg.caption or (text[:50] if text else None)
                database.add_stage_message(
                    config.DB_PATH,
                    stage=4,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption,
                    package_id=pkg_id,
                    file_unique_id=file_uid
                )

                conn = database.get_connection(config.DB_PATH)
                try:
                    cur = conn.execute("SELECT COUNT(*) as cnt FROM stage_messages WHERE package_id = ?", (pkg_id,))
                    pkg_cnt = cur.fetchone()["cnt"]
                finally:
                    conn.close()

                await safe_reply_text(
                    msg,
                    f"✅ فایل شماره {pkg_cnt} در این بسته آموزشی ذخیره شد 🟢\n"
                    "*(می‌توانید فایل‌های مکمل مثل ویدیو، عکس و ویس را در ادامه فوروارد کنید یا دکمه اتمام را بزنید)*",
                    parse_mode="Markdown",
                    reply_markup=get_recording_reply_keyboard()
                )
                return

            else:
                stage_num = int(state.split("_")[-1]) if state.split("_")[-1].isdigit() else 1
                st_title = get_stage_display_name(stage_num)
                caption = msg.caption or (text[:50] if text else None)
                before_cnt = len(database.get_stage_messages(config.DB_PATH, stage_num))
                database.add_stage_message(
                    config.DB_PATH,
                    stage=stage_num,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption,
                    file_unique_id=file_uid
                )
                count = len(database.get_stage_messages(config.DB_PATH, stage_num))
                if count == before_cnt:
                    await safe_reply_text(
                        msg,
                        f"ℹ️ این فایل قبلاً در «{st_title}» ذخیره شده بود و از ثبت تکراری آن جلوگیری شد ✅\n"
                        f"📊 مجموع پیام‌های این مرحله: **{count} پیام**",
                        parse_mode="Markdown",
                        reply_markup=get_recording_reply_keyboard()
                    )
                else:
                    await safe_reply_text(
                        msg,
                        f"✅ فایل با موفقیت برای «{st_title}» ذخیره شد 🟢\n"
                        f"📊 مجموع پیام‌های این مرحله: **{count} پیام**\n"
                        "*(می‌توانید پیام‌های دیگر را فوروارد کنید یا دکمه اتمام ذخیره را بزنید)*",
                        parse_mode="Markdown",
                        reply_markup=get_recording_reply_keyboard()
                    )
                return

        # H) Student Dossier Auto-Save from Forwarded Messages
        is_forwarded = bool(msg.forward_date or msg.forward_from or msg.forward_sender_name or getattr(msg, "forward_origin", None))
        if is_forwarded:
            if msg.voice:
                media_type = "voice"
            elif msg.audio:
                media_type = "audio"
            elif msg.video or msg.video_note:
                media_type = "video"
            elif msg.photo:
                media_type = "photo"
            elif msg.document:
                media_type = "document"
            else:
                media_type = "text"
            caption = msg.caption
            text_content = msg.text

            target_student_id = None
            sender_name = None

            if msg.forward_from:
                target_student_id = msg.forward_from.id
                sender_name = msg.forward_from.full_name
            elif hasattr(msg, "forward_origin") and msg.forward_origin:
                orig = msg.forward_origin
                orig_type = getattr(orig, "type", "")
                if orig_type == "user" and getattr(orig, "sender_user", None):
                    target_student_id = orig.sender_user.id
                    sender_name = orig.sender_user.full_name
                elif orig_type == "hidden_user":
                    sender_name = getattr(orig, "sender_user_name", None)
            elif msg.forward_sender_name:
                sender_name = msg.forward_sender_name

            # Check 1: Direct match by Telegram User ID
            if target_student_id:
                st = database.get_student(config.DB_PATH, target_student_id)
                if st:
                    note_id = database.add_student_dossier_note(
                        config.DB_PATH,
                        user_id=target_student_id,
                        from_chat_id=msg.chat_id,
                        message_id=msg.message_id,
                        media_type=media_type,
                        caption=caption,
                        text_content=text_content
                    )
                    st_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
                    safe_name = escape_md(st_name)
                    tot = database.get_student_dossier_count(config.DB_PATH, target_student_id)
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("👁 مشاهده کامل پرونده", callback_data=f"dossier_view_{target_student_id}", style="success")],
                        [InlineKeyboardButton("↩️ حذف این پیام از پرونده", callback_data=f"dossier_del_{note_id}", style="danger")],
                        [InlineKeyboardButton("👑 منوی اصلی مدیریت", callback_data="admin_menu", style="primary")]
                    ])
                    await safe_reply_text(
                        msg,
                        f"📁 **پیام فوروارد شده در پرونده «{safe_name}» ذخیره شد!** ✅\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"• نوع محتوا: **{media_type}**\n"
                        f"• مجموع موارد پرونده این دانش‌آموز: **{tot} مورد**",
                        parse_mode="Markdown",
                        reply_markup=kb
                    )
                    return

            # Check 2: Match by Sender Name (e.g. forward with privacy)
            matches = database.find_students_by_name(config.DB_PATH, sender_name) if sender_name else []
            if len(matches) == 1:
                matched_st = matches[0]
                m_uid = matched_st["user_id"]
                note_id = database.add_student_dossier_note(
                    config.DB_PATH,
                    user_id=m_uid,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    media_type=media_type,
                    caption=caption,
                    text_content=text_content
                )
                st_name = matched_st.get("assigned_name") or matched_st.get("full_name") or "دانش‌آموز"
                safe_name = escape_md(st_name)
                tot = database.get_student_dossier_count(config.DB_PATH, m_uid)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👁 مشاهده کامل پرونده", callback_data=f"dossier_view_{m_uid}", style="success")],
                    [InlineKeyboardButton("↩️ حذف این پیام از پرونده", callback_data=f"dossier_del_{note_id}", style="danger")],
                    [InlineKeyboardButton("👑 منوی اصلی مدیریت", callback_data="admin_menu", style="primary")]
                ])
                await safe_reply_text(
                    msg,
                    f"📁 **پیام بر اساس نام فرستنده ({escape_md(sender_name)}) در پرونده «{safe_name}» ذخیره شد!** ✅\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"• نوع محتوا: **{media_type}**\n"
                    f"• مجموع موارد پرونده: **{tot} مورد**",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
                return

            # Check 3: Ambiguous or unlinked forward -> Ask admin to assign to a student
            pending_dossier_notes[user_id] = {
                "from_chat_id": msg.chat_id,
                "message_id": msg.message_id,
                "media_type": media_type,
                "caption": caption,
                "text_content": text_content,
                "sender_name": sender_name
            }
            all_students = database.list_students(config.DB_PATH)
            if not all_students:
                await safe_reply_text(msg, "ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
                return

            candidates = matches if matches else all_students
            prompt_name = f" از «{escape_md(sender_name)}»" if sender_name else ""
            await safe_reply_text(
                msg,
                f"📥 **پیام فوروارد شده{prompt_name} دریافت شد.**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "جهت ثبت این پیام در پرونده، لطفاً دانش‌آموز مربوطه را از لیست زیر انتخاب فرمایید:",
                parse_mode="Markdown",
                reply_markup=get_dossier_assign_keyboard(candidates)
            )
            return

        # I) Stateless Fallback for Admin Plain Text:
        # If the admin types a student name (e.g. "خودم", "نگار", "لینک علی") even when state was None/reset,
        # immediately generate and send the invite link so the bot never ignores the admin!
        if text and not text.startswith("/") and len(text) <= 60:
            await create_and_send_invite_link(msg, context.bot, user_id, text)
            return

    # ==========================
    # 2. STUDENT ACTIONS & BUTTONS
    # ==========================
    if database.is_student_removed(config.DB_PATH, user_id):
        return

    async with get_user_lock(user_id):
        student = database.get_student(config.DB_PATH, user_id)
        if not student:
            return

        if getattr(msg, "message_id", None):
            database.record_student_sent_message(config.DB_PATH, user_id, msg.message_id, content_type="student_msg")
        curr_stage = student["current_stage"]
        raw_name = student.get("assigned_name") or student.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)

        # Check student bottom keyboard buttons FIRST so tapping a menu button while in AWAITING_STUDY_REPORT never submits the button text as a report
        if "ارسال لیست مباحث" in text:
            active_states[user_id] = "AWAITING_STUDY_REPORT"
            await reply_student_msg(
                msg,
                user_id=user_id,
                text=(
                    f"📝 **{name} عزیز، لطفاً لیست مباحثی که مطالعه کرده‌اید را تایپ کنید و بفرستید:**\n\n"
                    "💡 *مثال:* زیست: گفتار ۱ فصل ۳ تمام شد و ۵۰ تست زدم. شیمی: تا صفحه ۴۰ خوانده شد و ویدیو تحلیل رو دیدم."
                ),
                parse_mode="Markdown"
            )
            return

        if "گوش دادم" in text or "اتمام دوره اصلی" in text or "ورود به فاز ۴" in text:
            active_states[user_id] = None
            if curr_stage >= 4:
                await reply_student_msg(
                    msg,
                    user_id=user_id,
                    text=(
                        f"📌 **{name} عزیز، شما هر ۳ مرحله اولیه را به پایان رسانده‌اید و در فاز ۴ هستید.** 🌱\n"
                        "مهم‌ترین مطالب و ویس‌های جدید به ترتیب در همین صفحه برای شما ارسال می‌شود."
                    ),
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(4)
                )
                return

            # Prevent accidental double-tap on the same bottom keyboard button from skipping a stage
            norm_btn = database.convert_digits_to_english(text)
            m_target = re.search(r"رفتن به مرحله\s*(\d+)", norm_btn)
            if m_target:
                requested_next = int(m_target.group(1))
                if requested_next <= curr_stage:
                    await reply_student_msg(
                        msg,
                        user_id=user_id,
                        text=f"ℹ️ **{name} عزیز، شما هم‌اکنون در مرحله {curr_stage} هستید.**",
                        parse_mode="Markdown",
                        reply_markup=get_student_reply_keyboard(curr_stage)
                    )
                    return

            next_stage = curr_stage + 1
            database.update_student_stage(config.DB_PATH, user_id, next_stage)

            if next_stage <= 3:
                await reply_student_msg(
                    msg,
                    user_id=user_id,
                    text=(
                        f"👏 **آفرین {name} عزیز! مرحله {next_stage} برات باز شد.** 🌱\n"
                        "فایل‌های این مرحله ارسال می‌شوند:"
                    ),
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(next_stage)
                )
                await _deliver_stage_content_unlocked(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1, greeting_name=raw_name)
            else:
                completion_msg = (
                    "🎉 **تبریک ویژه! شما هر ۳ مرحله اولیه آموزش را با موفقیت به پایان رساندید.** 🏆✨\n\n"
                    "📌 **حتماً این صفحه و چت را پین (Pin) کنید تا همیشه در دسترستان باشد.**\n\n"
                    "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب، ویس‌ها و نکات آموزشی جدید در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** 🌱"
                )
                await reply_student_msg(
                    msg,
                    user_id=user_id,
                    text=completion_msg,
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(4)
                )
            return

        if "پیام به دکتر هاشمی" in text:
            active_states[user_id] = None
            await reply_student_msg(
                msg,
                user_id=user_id,
                text=(
                    "💬 **ارتباط مستقیم با دکتر هاشمی:**\n\n"
                    "جهت ارسال گزارش کار، سوالات درسی یا ارتباط با مشاور، وارد پی‌وی زیر شوید:\n"
                    "👉 @Hojjat_hshmi 🩺"
                ),
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(curr_stage)
            )
            return

        # A) State: Student typing or sending their study report
        st_state = active_states.get(user_id)
        if st_state == "AWAITING_STUDY_REPORT":
            report_content = text or (msg.caption or "").strip()
            has_media_report = bool(msg.voice or msg.photo or msg.document or msg.video or msg.audio)
            if not report_content and not has_media_report:
                return

            active_states[user_id] = None
            saved_text = report_content if report_content else "[گزارش چندرسانه‌ای / فایل]"
            report_result = database.submit_study_plan_report(config.DB_PATH, user_id, saved_text)
            dur = report_result["duration_days"] if report_result else 7

            # Also save student report in their dossier automatically
            database.add_student_dossier_note(
                config.DB_PATH,
                user_id=user_id,
                from_chat_id=msg.chat_id if has_media_report else None,
                message_id=msg.message_id if has_media_report else None,
                media_type="report",
                caption=msg.caption,
                text_content=saved_text
            )

            safe_text = escape_md(saved_text)

            await reply_student_msg(
                msg,
                user_id=user_id,
                text=(
                    f"✅ **{name} عزیز، گزارش مباحث با موفقیت برای دکتر هاشمی ارسال شد!** 🌱✨\n\n"
                    "پس از بررسی، برنامه دوره بعدی برایتان تنظیم خواهد شد 🎯"
                ),
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(curr_stage)
            )

            # Alert admin
            admin_target = config.ADMIN_ID
            if admin_target:
                report_alert = (
                    f"📩🔔 **گزارش مباحث خوانده‌شده از طرف «{name}»:**\n\n"
                    f"📝 *متن گزارش:*\n{safe_text}\n\n"
                    f"🎯 *برنامه دوره قبلی:* {dur} روزه (تکمیل شد)\n"
                    "👇 جهت تایید و فعال‌سازی برنامه دوره جدید، دکمه زیر را لمس کنید:"
                )
                admin_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"➕ تنظیم برنامه دوره جدید برای {raw_name}", callback_data=f"plan_user_{user_id}", style="success")
                ]])
                try:
                    if has_media_report:
                        await context.bot.copy_message(
                            chat_id=admin_target,
                            from_chat_id=msg.chat_id,
                            message_id=msg.message_id
                        )
                    await safe_bot_send_message(
                        context.bot,
                        chat_id=admin_target,
                        text=report_alert,
                        parse_mode="Markdown",
                        reply_markup=admin_kb
                    )
                except Exception as e:
                    logger.error(f"Could not alert admin about report: {e}")
            return

# --- Background Automated Scheduler Loop ---

async def drip_scheduler_loop(app):
    logger.info("Background automated scheduler loop started.")
    last_run_day = None

    while True:
        try:
            async with scheduler_cycle_lock:
                now_iran = get_iran_now()
                today_str = now_iran.strftime("%Y-%m-%d")

                # 1. Daily 7:00 AM Morning Motivational Quotes Check
                if now_iran.hour == 7:
                    quotes_sent = await execute_morning_quotes_cycle(app.bot)
                    if quotes_sent > 0:
                        logger.info(f"Daily 7:00 AM motivational quotes delivered to {quotes_sent} students.")

                # 2. Daily Nightly Drip & Milestones Check (Default 23:00)
                target_hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))
                last_nightly_date = database.get_setting(config.DB_PATH, "last_nightly_cycle_date", "")
                if now_iran.hour == target_hour and last_run_day != today_str and last_nightly_date != today_str:
                    database.set_setting(config.DB_PATH, "last_nightly_cycle_date", today_str)
                    last_run_day = today_str
                    logger.info(f"Target hour {target_hour}:00 reached in Iran. Running daily scheduled deliveries...")
                    milestones_sent = await execute_milestones_check(app.bot)
                    drip_sent = await execute_drip_cycle(app.bot, force_ignore_interval=False)
                    logger.info(f"Daily cycle finished. Milestones: {milestones_sent}, Drip: {drip_sent}")

                    # Auto-backup database daily to admin
                    if config.ADMIN_ID:
                        try:
                            await send_database_backup_file(app.bot, config.ADMIN_ID)
                            logger.info("Sent daily auto-backup database file to admin.")
                        except Exception as e:
                            logger.warning(f"Could not send daily backup to admin: {e}")

                # 3. Hourly Study Plans Reminders
                plan_reminders = await execute_study_plans_reminders(app.bot)
                if plan_reminders > 0:
                    logger.info(f"Sent {plan_reminders} study plan 1-day reminders.")

                # 4. Inactivity Warnings (every loop check)
                inactivity_warns = await execute_inactivity_warnings(app.bot)
                if inactivity_warns > 0:
                    logger.info(f"Sent {inactivity_warns} inactivity warnings to students.")

        except Exception as e:
            logger.error(f"Error in drip_scheduler_loop: {e}")

        await asyncio.sleep(60)

# --- Global Error Handler (Prevents Unhandled Exceptions & Duplicate Polling Conflicts) ---

async def global_error_handler(update: object, context: Any) -> None:
    global _CONFLICT_COUNT
    err = getattr(context, "error", None)
    err_str = str(err or "")
    if "terminated by other getupdates request" in err_str.lower() or "conflict" in err_str.lower():
        _CONFLICT_COUNT += 1
        logger.warning(f"Telegram getUpdates Conflict detected (#{_CONFLICT_COUNT}): {err_str}")
        # If another newer instance is persistently polling the same BOT_TOKEN, exit cleanly to stop duplicate messages
        if _CONFLICT_COUNT >= 6:
            logger.error("Persistent getUpdates Conflict detected (>=6 times). Shutting down this duplicate instance to prevent duplicate messages.")
            os._exit(0)
        return
    _CONFLICT_COUNT = 0
    logger.warning(f"Handled bot exception: {err}")

# --- Application Setup ---

def create_application():
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in config or environment variables!")
        return None

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^(admin_|stage|drip_|plan_|broadcast_|dossier_)"))
    app.add_handler(CallbackQueryHandler(student_callback_handler, pattern="^student_"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_collector))
    app.add_error_handler(global_error_handler)

    return app

_INSTANCE_LOCK_FD = None

def acquire_single_instance_lock() -> bool:
    """Acquires an OS-level exclusive file lock so two local bot.py processes can never run simultaneously."""
    global _INSTANCE_LOCK_FD
    try:
        import fcntl
        lock_path = "/tmp/consulting_bot_single_instance.lock"
        fd = open(lock_path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _INSTANCE_LOCK_FD = fd
        return True
    except ImportError:
        return True
    except OSError:
        logger.error("Another local instance of bot.py is already holding the lock! Exiting to prevent duplicate messages.")
        return False

def main():
    if not acquire_single_instance_lock():
        sys.exit(0)

    database.init_db(config.DB_PATH)
    logger.info("Database initialized successfully.")

    try:
        from keep_alive import start_keep_alive
        start_keep_alive(port=config.PORT, ping_url=config.RENDER_EXTERNAL_URL)
    except Exception as e:
        logger.warning(f"Keep-alive server warning: {e}")

    app = create_application()
    if not app:
        print("Please configure BOT_TOKEN in .env or environment variables.")
        sys.exit(1)

    async def post_init(application):
        try:
            await resolve_bot_username(application.bot)
        except Exception:
            pass
        asyncio.create_task(drip_scheduler_loop(application))

    app.post_init = post_init

    logger.info("Bot starting polling (drop_pending_updates=False)...")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
