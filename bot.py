import os
import sys
import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

import config
import database
import motivational_quotes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Active states: "AWAITING_STUDENT_NAME", "RECORD_STAGE_...", "AWAITING_CUSTOM_DAYS_<user_id>", "AWAITING_STUDY_REPORT"
active_states: Dict[int, Optional[str]] = {}
active_package_ids: Dict[int, int] = {}

try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ReplyKeyboardMarkup,
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
except ImportError:
    Update = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    ReplyKeyboardMarkup = None
    KeyboardButton = None
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

def is_admin(user_id: int, username: Optional[str]) -> bool:
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        return True
    if username and config.ADMIN_USERNAME:
        if username.lower().lstrip("@") == config.ADMIN_USERNAME.lower().lstrip("@"):
            return True
    return False

# --- Persistent Bottom Reply Keyboards (With Vivid Color Badges) ---

def get_admin_reply_keyboard():
    keyboard = [
        [KeyboardButton("🟢 🔗 ساخت لینک جدید (با نام) 👤"), KeyboardButton("🔵 📁 بارگذاری محتوای مراحل 📥")],
        [KeyboardButton("🟣 🎙 افزودن فایل به بخش ۴ 💡"), KeyboardButton("🟠 🎯 محتوای روز ۸ و ۳۰ 🏆")],
        [KeyboardButton("🟡 🗓 برنامه درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯"), KeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار ⚙️")],
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
        [KeyboardButton("🔵 🔄 ارسال مجدد ویس‌های این مرحله 🔁"), KeyboardButton("💬 🩺 پیام به دکتر هاشمی 💬")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- Inline Keyboards (With Vivid Color Badges) ---

def get_admin_main_inline_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟢 🔗 ساخت لینک اختصاصی (با نام) 👤", callback_data="admin_ask_name", style="success")],
        [InlineKeyboardButton("🔵 📁 مدیریت و بارگذاری محتوای مراحل 📥", callback_data="admin_manage_stages", style="primary")],
        [InlineKeyboardButton("🟣 🎙 افزودن بسته آموزشی به بخش ۴ 💡", callback_data="admin_record_4", style="primary")],
        [InlineKeyboardButton("🟠 🎯 تنظیم محتوای روز ۸ و ۳۰ 🏆", callback_data="admin_milestones_menu", style="primary")],
        [InlineKeyboardButton("🟡 🗓 برنامه‌ریزی درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯", callback_data="admin_plans_menu", style="primary")],
        [InlineKeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار بخش ۴ ⚙️", callback_data="admin_drip_settings", style="primary")],
        [InlineKeyboardButton("☀️ 💬 تست و مشاهده پیام انگیزشی ۷ صبح 🚀", callback_data="admin_test_quote", style="primary")],
        [InlineKeyboardButton("📊 📈 لیست و وضعیت دقیق دانش‌آموزان 📈", callback_data="admin_stats", style="success")],
        [InlineKeyboardButton("💾 📦 دریافت نسخه پشتیبان دیتابیس (Backup)", callback_data="admin_download_db", style="primary")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stages_selection_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟢 📥 بارگذاری مرحله ۱ 🎓", callback_data="admin_record_1", style="success")],
        [InlineKeyboardButton("🔵 📥 بارگذاری مرحله ۲ 📚", callback_data="admin_record_2", style="primary")],
        [InlineKeyboardButton("🟣 📥 بارگذاری مرحله ۳ 🧠", callback_data="admin_record_3", style="primary")],
        [InlineKeyboardButton("🟠 🎙 بارگذاری فایل‌های بخش ۴ 💡", callback_data="admin_record_4", style="primary")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu", style="danger")],
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

# --- Content Delivery Helpers ---

async def deliver_stage_content(bot, chat_id: int, stage: int, next_stage: int, greeting_name: str = ""):
    msgs = database.get_stage_messages(config.DB_PATH, stage)
    student = database.get_student(config.DB_PATH, chat_id)
    curr_st = student["current_stage"] if student else stage
    reply_kb = get_student_reply_keyboard(curr_st)

    if not msgs:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📌 محتوای **مرحله {stage}** به زودی توسط دکتر هاشمی بارگذاری می‌شود ⏳",
            parse_mode="Markdown",
            reply_markup=reply_kb
        )
        return

    safe_greeting = escape_md(greeting_name)
    name_phrase = f"{safe_greeting} عزیز، " if safe_greeting else ""
    await bot.send_message(
        chat_id=chat_id,
        text=f"📚 **{name_phrase}محتوای مرحله {stage} برای شما ارسال می‌شود:**\n"
             "لطفاً ویس‌ها و فایل‌های آموزشی را با دقت گوش داده و یادداشت‌برداری کنید 🌱✨",
        parse_mode="Markdown"
    )

    for item in msgs:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=item["from_chat_id"],
                message_id=item["message_id"]
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning(f"Failed to copy message {item.get('message_id')} for stage {stage}: {e}")

    btn_text = f"🟢 ✅ گوش دادم | رفتن به مرحله {next_stage} ➡️" if next_stage <= 3 else "🚀 🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=f"student_goto_{next_stage}", style="success")]])

    await bot.send_message(
        chat_id=chat_id,
        text=f"✨ تمام ویس‌های **مرحله {stage}** ارسال شد 🎓\n"
             "همچنین گزینه‌های کنترل همیشه در **پایین صفحه چت** در دسترس شما هستند:",
        parse_mode="Markdown",
        reply_markup=inline_kb
    )
    await bot.send_message(
        chat_id=chat_id,
        text="👇 *گزینه‌های دسترسی سریع:*",
        parse_mode="Markdown",
        reply_markup=reply_kb
    )

async def execute_drip_cycle(bot, force_ignore_interval: bool = False) -> int:
    eligible_pairs = database.get_eligible_students_for_drip(config.DB_PATH, force_ignore_interval=force_ignore_interval)
    sent_count = 0

    for st, pkg_msgs in eligible_pairs:
        user_id = st["user_id"]
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)
        if not pkg_msgs:
            continue

        pkg_id = pkg_msgs[0].get("package_id") or pkg_msgs[0]["id"]
        msg_ids = [m["id"] for m in pkg_msgs]

        try:
            if len(pkg_msgs) > 1:
                intro = (
                    f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎙🎬 **مبحث تکمیلی جدید دکتر هاشمی ({len(pkg_msgs)} فایل پیوسته) برای شما ارسال می‌شود:**\n\n"
                    "📌 لطفاً تمام فایل‌های این بسته آموزشی (ویدیو/عکس و ویس توضیحات) را با دقت و به ترتیب مشاهده کرده و گوش دهید 🎓"
                )
            else:
                caption_text = f"\n\n{escape_md(pkg_msgs[0].get('caption'))}" if pkg_msgs[0].get("caption") else ""
                intro = (
                    f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱✨\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎙 **مبحث تکمیلی جدید از طرف دکتر هاشمی برای شما ارسال می‌شود:**{caption_text}"
                )

            await bot.send_message(
                chat_id=user_id,
                text=intro,
                parse_mode="Markdown"
            )

            for m in pkg_msgs:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=m["from_chat_id"],
                    message_id=m["message_id"]
                )
                await asyncio.sleep(0.4)

            database.record_drip_package_delivery(config.DB_PATH, user_id, pkg_id, msg_ids)
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
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)
        msgs = database.get_stage_messages(config.DB_PATH, milestone)
        if not msgs:
            continue

        try:
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

            await bot.send_message(chat_id=user_id, text=header, parse_mode="Markdown")
            for item in msgs:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=item["from_chat_id"],
                    message_id=item["message_id"]
                )
                await asyncio.sleep(0.3)

            database.mark_milestone_sent(config.DB_PATH, user_id, milestone)
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
        raw_name = plan.get("assigned_name") or plan.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)
        duration = plan["duration_days"]

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
            await bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="Markdown",
                reply_markup=report_kb
            )
            await bot.send_message(
                chat_id=user_id,
                text="👇 یا مستقیماً از دکمه «🟣 📝 ارسال لیست مباحث خوانده‌شده 📩» در پایین کیبورد استفاده کنید:",
                reply_markup=get_student_reply_keyboard(4)
            )
            database.mark_study_plan_reminder_sent(config.DB_PATH, plan["id"])
            sent_count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending plan reminder to user {user_id}: {e}")

    return sent_count

async def execute_inactivity_warnings(bot) -> int:
    due_students = database.get_students_due_for_inactivity_warning(config.DB_PATH, threshold_days=2.0)
    sent_count = 0

    for st in due_students:
        user_id = st["user_id"]
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)
        stage = st["current_stage"]
        next_stage = stage + 1

        warning_text = (
            f"⚠️ **{name} عزیز، یک یادآوری بسیار مهم!** ⏳\n\n"
            f"الان بیش از ۲ روز است که ویس‌های **مرحله {stage}** را گوش نداده‌اید!\n"
            "مسیر مشاوره نیازمند استمرار و تعهد شماست و نباید از برنامه عقب بمانید 🌱\n\n"
            "📌 **حتماً تا آخر امشب وقت بگذارید، ویس‌های این مرحله را کامل گوش دهید و وارد مرحله بعد شوید.**\n\n"
            f"🔒 **توجه:** در حال حاضر برای شما محدودیت ایجاد شده است چون هنوز در مرحله {stage} هستید. "
            "به محض اینکه وارد فاز ۴ شوید، یک سری ویس‌ها و آموزش‌های بسیار تکمیلی و کلیدی برایتان ارسال می‌شود که جهش بزرگی در روند مطالعه شما ایجاد خواهد کرد 🚀✨\n\n"
            "👇 همین الان ویس‌ها را گوش دهید و پس از اتمام دکمه زیر را لمس کنید:"
        )

        btn_text = f"🟢 ✅ گوش دادم | رفتن به مرحله {next_stage} ➡️" if next_stage <= 3 else "🚀 🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_text, callback_data=f"student_goto_{next_stage}", style="success")],
            [InlineKeyboardButton(f"🔵 🔄 ارسال مجدد ویس‌های مرحله {stage} 🔁", callback_data=f"student_resend_stage_{stage}", style="primary")]
        ])

        try:
            await bot.send_message(
                chat_id=user_id,
                text=warning_text,
                parse_mode="Markdown",
                reply_markup=kb
            )
            database.mark_inactivity_warning_sent(config.DB_PATH, user_id)
            sent_count += 1
            logger.info(f"Inactivity warning sent to student {raw_name} ({user_id}) for stage {stage}.")
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending inactivity warning to user {user_id}: {e}")

    return sent_count

async def execute_morning_quotes_cycle(bot, target_user_id: Optional[int] = None) -> int:
    """Delivers daily 7:00 AM non-cliché motivational quotes to students."""
    today_str = get_iran_now().strftime("%Y-%m-%d")
    
    # If not a manual test on a specific user, check if already sent today
    if not target_user_id:
        last_sent_date = database.get_setting(config.DB_PATH, "last_morning_quote_date", "")
        if last_sent_date == today_str:
            return 0

    if target_user_id:
        st_obj = database.get_student(config.DB_PATH, target_user_id)
        if st_obj:
            students = [st_obj]
        else:
            students = [{"user_id": target_user_id, "full_name": "مدیر محترم", "assigned_name": "دکتر هاشمی"}]
    else:
        students = database.list_students(config.DB_PATH)

    if not students:
        return 0

    try:
        curr_idx = int(database.get_setting(config.DB_PATH, "morning_quote_index", "0"))
    except ValueError:
        curr_idx = 0

    quote_text = motivational_quotes.get_motivational_quote(curr_idx)
    sent_count = 0

    for st in students:
        if not st:
            continue
        user_id = st["user_id"]
        raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)

        msg_text = (
            f"☀️ **صبح بخیر {name} عزیز** 🌱✨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✨ « **{escape_md(quote_text)}** »\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🎯 *امروز یک روز طلایی برای پیشرفت و سبقت گرفتنه؛ با تمام تمرکز و پرانرژی استارت بزن!* 🚀🎓"
        )
        try:
            await bot.send_message(
                chat_id=user_id,
                text=msg_text,
                parse_mode="Markdown"
            )
            sent_count += 1
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.warning(f"Could not send morning quote to {user_id}: {e}")

    if not target_user_id:
        database.set_setting(config.DB_PATH, "last_morning_quote_date", today_str)
        database.set_setting(config.DB_PATH, "morning_quote_index", str((curr_idx + 1) % len(motivational_quotes.MOTIVATIONAL_QUOTES)))
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
                    "💡 *در صورت نیاز به بازیابی اطلاعات (مثلاً پس از ویرایش و ری‌استارت رندر)، کافیست همین فایل `.db` را در پی‌وی برای ربات بفرستید.*"
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

async def start_handler(update, context):
    if not update or not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or "دانش‌آموز"
    username = update.effective_user.username

    # 1. Check for invite argument (/start inv_XXXXXX) - Checked FIRST so invites always register students
    args = context.args if context else []
    if args and len(args) > 0 and args[0].startswith("inv_"):
        code = args[0]
        success, assigned_name = database.validate_and_use_invite(
            config.DB_PATH, code, user_id, username, user_name
        )
        if success:
            display_name = assigned_name or user_name
            safe_display_name = escape_md(display_name)
            salutation = f"سلام {safe_display_name} عزیز 🌸"
            await update.message.reply_text(
                f"✨ **{salutation}**\n\n"
                "به ربات مشاوره تحصیلی دکتر هاشمی خوش آمدید 🌱🎓\n"
                "فرآیند آموزشی شما در ۳ مرحله طراحی شده است.\n"
                "آماده‌اید؟ مرحله اول همین الان برای شما ارسال می‌شود 🚀:",
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(1)
            )
            await deliver_stage_content(context.bot, user_id, stage=1, next_stage=2, greeting_name=display_name)

            # Alert admin that a student just joined and is now in their students list
            if config.ADMIN_ID and user_id != config.ADMIN_ID:
                try:
                    uname = f"@{escape_md(username)}" if username else "بدون یوزرنیم"
                    await context.bot.send_message(
                        chat_id=config.ADMIN_ID,
                        text=f"🔔 **دانش‌آموز جدید ثبت‌نام شد:** 👤\n\n"
                             f"• نام: **{safe_display_name}** ({uname})\n"
                             "• کد دعوت با موفقیت فعال شد و این دانش‌آموز به لیست دانش‌آموزان شما اضافه گردید 🎯",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Could not alert admin about new student: {e}")
            return
        else:
            await update.message.reply_text(
                "❌ **لینک دعوت نامعتبر یا منقضی شده است.**\n"
                "این لینک قبلاً استفاده شده است یا وجود ندارد. لطفاً جهت دریافت لینک جدید با دکتر هاشمی در ارتباط باشید 🩺",
                parse_mode="Markdown"
            )
            return

    # 2. Admin check (Hojjat_hshmi or ADMIN_ID)
    if is_admin(user_id, username):
        await update.message.reply_text(
            "👑 **سلام آقای دکتر هاشمی گرامی**\n"
            "به پنل اختصاصی مدیریت ربات مشاوره تحصیلی خوش آمدید 🩺✨🌱\n\n"
            "👇 *دکمه‌های دسترسی سریع همیشه در پایین صفحه برای شما فعال هستند:*",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )
        return

    # 3. Existing registered student check
    student = database.get_student(config.DB_PATH, user_id)
    if student:
        stage = student["current_stage"]
        name = student.get("assigned_name") or user_name
        safe_name = escape_md(name)
        reply_kb = get_student_reply_keyboard(stage)
        if stage <= 3:
            await update.message.reply_text(
                f"👋 **سلام مجدد {safe_name} عزیز.** 🌱\n"
                f"شما در حال حاضر در **مرحله {stage}** هستید. محتوای این مرحله مجدداً برای شما ارسال می‌شود:",
                parse_mode="Markdown",
                reply_markup=reply_kb
            )
            await deliver_stage_content(context.bot, user_id, stage=stage, next_stage=stage + 1, greeting_name=name)
        else:
            await update.message.reply_text(
                f"🌟 **سلام {name} عزیز!**\n\n"
                "📌 **این صفحه را حتماً پین (Pin) داشته باشید تا همیشه در دسترستان باشد.**\n"
                "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب و ویس‌های جدید مشاوره‌ای در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** ✨",
                parse_mode="Markdown",
                reply_markup=reply_kb
            )
        return

    # 4. Unauthorized visitor
    await update.message.reply_text(
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
        await query.message.reply_text(
            "👑 **پنل مدیریت ربات مشاوره دکتر هاشمی:**",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

    elif data == "admin_ask_name":
        active_states[user_id] = "AWAITING_STUDENT_NAME"
        await query.message.reply_text(
            "📝 **لطفاً نام دانش‌آموز جدید را تایپ کنید:**\n\n"
            "مثلاً بنویسید: `نگار` یا `علی` یا `سارا`\n"
            "(ربات با همین نام به او خوش‌آمد خواهد گفت: *سلام نگار عزیز 🌸*)",
            parse_mode="Markdown"
        )

    elif data == "admin_manage_stages":
        await query.message.reply_text(
            "📁 **مرحله مورد نظر برای بارگذاری محتوا را انتخاب کنید:**",
            parse_mode="Markdown",
            reply_markup=get_stages_selection_keyboard()
        )

    elif data == "admin_milestones_menu":
        c8 = len(database.get_stage_messages(config.DB_PATH, 8))
        c30 = len(database.get_stage_messages(config.DB_PATH, 30))
        msg = (
            "🎯 **مدیریت محتوای روز ۸ و ۳۰ مشاوره:** 🏆\n\n"
            f"• فایل‌های ذخیره‌شده برای روز ۸: **{c8} پیام 🎯**\n"
            f"• فایل‌های ذخیره‌شده برای روز ۳۰: **{c30} پیام 🏆**\n\n"
            "💡 *این محتواها رأس روز ۸ و روز ۳۰ پس از استارت بات، خودکار برای هر دانش‌آموز ارسال می‌گردد.*"
        )
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_milestones_keyboard())

    elif data == "admin_test_quote":
        await query.message.reply_text("⏳ در حال آماده‌سازی و ارسال نمونه پیام انگیزشی ساعت ۷ صبح...")
        await execute_morning_quotes_cycle(context.bot, target_user_id=user_id)
        return

    elif data.startswith("admin_record_"):
        stage_num = int(data.split("_")[-1])
        active_states[user_id] = f"RECORD_STAGE_{stage_num}"
        active_package_ids[user_id] = int(get_iran_now().timestamp())
        if stage_num <= 3:
            stage_title = f"مرحله {stage_num}"
        elif stage_num == 4:
            stage_title = "بخش ۴ (بسته آموزشی جدید)"
        elif stage_num == 8:
            stage_title = "محتوای روز هشتم مشاوره 🎯"
        elif stage_num == 30:
            stage_title = "محتوای روز سی‌ام مشاوره 🏆"
        else:
            stage_title = f"بخش {stage_num}"

        note_text = (
            "✨ *نکته هوشمند بخش ۴:* اگر یک ویدیو یا عکس همراه با ویس توضیحات دارید، همه را پشت سر هم بفرستید؛ ربات آنها را در قالب یک بسته پیوسته ذخیره کرده و در روز نوبت با هم به دانش‌آموز تحویل می‌دهد.\n\n"
            if stage_num == 4 else ""
        )

        await query.message.reply_text(
            f"🎙 **حالت ذخیره‌سازی برای «{stage_title}» فعال شد.**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📥 هم‌اکنون پیام‌ها، ویس‌ها، ویدیوها و فایل‌ها را به این چت فوروارد کنید.\n"
            f"{note_text}"
            "👇 پس از پایان ارسال فایل‌های این بسته، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** در پایین صفحه را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_recording_reply_keyboard()
        )

    elif data == "admin_force_milestones":
        await query.message.reply_text("⏳ در حال بررسی نوبت‌های ارسالی روز ۸ و ۳۰ دانش‌آموزان...")
        sent = await execute_milestones_check(context.bot)
        if sent > 0:
            await query.message.reply_text(f"🎯 **موفق:** تعداد {sent} پکیج روز ۸ یا ۳۰ به دانش‌آموزان مربوطه ارسال شد.")
        else:
            await query.message.reply_text("ℹ️ در حال حاضر دانش‌آموزی که موعد روز ۸ یا ۳۰ او رسیده باشد و محتوا را نگرفته باشد یافت نشد.")

    elif data == "admin_drip_settings":
        interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
        hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))

        msg = (
            "⚙️ **تنظیمات زمان‌بندی ارسال خودکار (بخش ۴):** ⚙️\n\n"
            f"• **فاصله زمانی فعلی:** هر {interval} روز یکبار 🗓\n"
            f"• **ساعت ارسال در روز نوبت:** ساعت {hour}:۰۰ شب ⏰\n\n"
            "💡 *دانش‌آموزانی که ۳ مرحله را تمام کرده‌اند، طبق این برنامه ویس‌های بخش ۴ را یکی پس از دیگری دریافت می‌کنند.*"
        )
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_drip_settings_keyboard(interval, hour))

    elif data.startswith("drip_set_int_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_interval_days", val)
        await query.message.reply_text(f"✅ فاصله ارسال به **هر {val} روز یکبار** تغییر یافت.")

    elif data.startswith("drip_set_hour_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_send_hour", val)
        await query.message.reply_text(f"✅ ساعت ارسال روزانه به **ساعت {val}:۰۰ شب** تغییر یافت.")

    elif data == "admin_force_drip":
        await query.message.reply_text("⏳ در حال بررسی و ارسال نوبت ویس جدید به دانش‌آموزان واجد شرایط فاز ۴...")
        sent = await execute_drip_cycle(context.bot, force_ignore_interval=True)
        if sent > 0:
            await query.message.reply_text(f"🚀 **موفق:** تعداد {sent} ویس جدید برای دانش‌آموزان مرحله ۴ ارسال شد.")
        else:
            await query.message.reply_text("ℹ️ دانش‌آموز واجد شرایط جدیدی یافت نشد.")

    # --- Study Plans Management ---
    elif data == "admin_plans_menu":
        students = database.list_students(config.DB_PATH)
        if not students:
            await query.message.reply_text("ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
            return

        await query.message.reply_text(
            "🟡 🗓 **مدیریت برنامه‌های مطالعاتی دوره‌ای:**\n\n"
            "دانش‌آموز مورد نظر را انتخاب کنید تا برنامه جدید (۷، ۸، ۹ یا دلخواه) برایش فعال شود:",
            parse_mode="Markdown",
            reply_markup=get_student_list_for_plans_keyboard(students)
        )

    elif data.startswith("plan_user_"):
        target_uid = int(data.split("_")[-1])
        st = database.get_student(config.DB_PATH, target_uid)
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        active_plan = database.get_active_study_plan(config.DB_PATH, target_uid)

        plan_info = "در حال حاضر برنامه فعالی ندارد."
        if active_plan:
            dur = active_plan["duration_days"]
            status = active_plan["status"]
            plan_info = f"دارای برنامه **{dur} روزه** فعال (وضعیت: {status})"

        await query.message.reply_text(
            f"👤 **دانش‌آموز:** {name}\n"
            f"📊 **وضعیت کنونی:** {plan_info}\n\n"
            f"مدت برنامه جدید را برای «{name}» انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=get_plan_duration_keyboard(target_uid)
        )

    elif data.startswith("plan_set_"):
        parts = data.split("_")
        target_uid = int(parts[2])
        days = int(parts[3])
        database.create_study_plan(config.DB_PATH, target_uid, days)

        st = database.get_student(config.DB_PATH, target_uid)
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"

        await query.message.reply_text(
            f"✅ **برنامه مطالعاتی {days} روزه با موفقیت برای «{name}» فعال شد!** 🎯\n\n"
            f"• ۱ روز مانده به اتمام برنامه ({days - 1} روز دیگر)، پیام هشدار جهت ارسال مباحث خوانده‌شده خودکار برای دانش‌آموز ارسال خواهد شد.",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )

        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"🗓 **{name} عزیز، برنامه درسی {days} روزه جدید شما از امروز فعال شد!** 🎯🌱\n\n"
                     "با تمرکز و دقت کامل شروع کنید 🚀\n"
                     f"🔔 یک روز مانده به پایان برنامه، پیام یادآوری جهت ارسال لیست مباحث خوانده‌شده برایتان ارسال خواهد شد.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not notify student of new plan: {e}")

    elif data.startswith("plan_custom_"):
        target_uid = int(data.split("_")[-1])
        active_states[user_id] = f"AWAITING_CUSTOM_DAYS_{target_uid}"
        st = database.get_student(config.DB_PATH, target_uid)
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"

        await query.message.reply_text(
            f"✏️ **مدت برنامه برای «{name}» را به عدد وارد کنید:**\n\n"
            "مثلاً بنویسید: `7` یا `8` یا `10`",
            parse_mode="Markdown"
        )

    elif data == "admin_stats":
        report = generate_students_stats_report()
        await query.message.reply_text(report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())

    elif data == "admin_download_db":
        await query.message.reply_text("⏳ در حال تهیه نسخه پشتیبان کامل از دیتابیس...")
        await send_database_backup_file(context.bot, user_id)

# --- Student Callback Handler ---

async def student_callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    student = database.get_student(config.DB_PATH, user_id)
    if not student:
        await query.message.reply_text("⛔️ دسترسی شما یافت نشد. لطفاً مجدد با لینک دعوت استارت کنید.")
        return

    data = query.data
    name = student.get("assigned_name") or "دانش‌آموز"

    if data.startswith("student_goto_"):
        next_stage = int(data.split("_")[-1])
        database.update_student_stage(config.DB_PATH, user_id, next_stage)

        if next_stage <= 3:
            await query.message.reply_text(
                f"👏 **آفرین {name} عزیز! مرحله {next_stage} برات باز شد.** 🌱\n"
                "فایل‌های این مرحله ارسال می‌شوند:",
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(next_stage)
            )
            await deliver_stage_content(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1, greeting_name=name)
        else:
            # Stage 3 Completed - Entering Phase 4 (Exact user requested wording, no mention of قطره‌چکانی or ساعت ۲۲)
            completion_msg = (
                "🎉 **تبریک ویژه! شما هر ۳ مرحله اولیه آموزش را با موفقیت به پایان رساندید.** 🏆✨\n\n"
                "📌 **حتماً این صفحه و چت را پین (Pin) کنید تا همیشه در دسترستان باشد.**\n\n"
                "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب، ویس‌ها و نکات آموزشی جدید در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** 🌱"
            )
            await query.message.reply_text(
                completion_msg,
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(4)
            )

    elif data.startswith("student_resend_stage_"):
        st_num = int(data.split("_")[-1])
        await query.message.reply_text(
            f"🔄 **{name} عزیز، محتوای مرحله {st_num} مجدداً برای شما ارسال می‌شود:**",
            parse_mode="Markdown"
        )
        await deliver_stage_content(context.bot, user_id, stage=st_num, next_stage=st_num + 1, greeting_name=name)

    elif data == "student_start_report":
        active_states[user_id] = "AWAITING_STUDY_REPORT"
        await query.message.reply_text(
            f"📝 **{name} عزیز، لطفاً لیست مباحثی که تا امروز مطالعه کرده‌اید را تایپ کنید و بفرستید:**\n\n"
            "💡 *مثال:* زیست: گفتار ۱ فصل ۳ تمام شد و ۵۰ تست زدم. شیمی: تا صفحه ۴۰ خوانده شد و ویدیو تحلیل رو دیدم.",
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
        # Database restore document handler (Admin sends .db file)
        if msg.document:
            file_name = msg.document.file_name or ""
            if file_name.lower().endswith(".db"):
                await msg.reply_text("⏳ در حال دریافت و بررسی فایل دیتابیس برای بازیابی...")
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
                        await msg.reply_text(
                            "🎉 **دیتابیس با موفقیت کامل بازیابی شد!** 🟢✨\n\n"
                            f"👥 تعداد دانش‌آموزان بازیابی‌شده: **{st_count} نفر**\n"
                            "تمام مراحل، فایل‌های ذخیره‌شده و تنظیمات به طور کامل بازگردانده شدند 🚀",
                            parse_mode="Markdown",
                            reply_markup=get_admin_reply_keyboard()
                        )
                    else:
                        await msg.reply_text("❌ خطا در بازیابی دیتابیس. فایل نامعتبر است.")
                except Exception as e:
                    logger.error(f"Error restoring db from document: {e}")
                    await msg.reply_text(f"❌ خطا در پردازش فایل دیتابیس: {e}")
                return

        state = active_states.get(user_id)

        # A) State: Waiting for student name
        if state == "AWAITING_STUDENT_NAME":
            student_name = text if text else "دانش‌آموز"
            active_states[user_id] = None

            token = secrets.token_hex(4)
            code = f"inv_{token}"
            database.create_invite(config.DB_PATH, code, assigned_name=student_name)

            bot_info = await context.bot.get_me()
            bot_user = bot_info.username
            invite_link = f"https://t.me/{bot_user}?start={code}"

            safe_name = escape_md(student_name)
            reply_text = (
                f"🔗 **لینک دعوت اختصاصی برای «{safe_name}» ساخته شد:**\n\n"
                f"`{invite_link}`\n\n"
                "📌 **نکات مهم:**\n"
                f"• این لینک یکبارمصرف است و مختص **{safe_name}** می‌باشد.\n"
                f"• به محض باز کردن لینک، ربات با عبارت **«سلام {safe_name} عزیز 🌸»** به او خوش‌آمد می‌گوید و به طور خودکار به لیست دانش‌آموزان شما اضافه خواهد شد."
            )
            await msg.reply_text(reply_text, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
            return

        # B) State: Custom days input for study plan
        if state and state.startswith("AWAITING_CUSTOM_DAYS_"):
            target_uid = int(state.split("_")[-1])
            active_states[user_id] = None
            try:
                days = int(text)
            except ValueError:
                days = 7

            database.create_study_plan(config.DB_PATH, target_uid, days)
            st = database.get_student(config.DB_PATH, target_uid)
            raw_name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
            name = escape_md(raw_name)

            await msg.reply_text(
                f"✅ **برنامه {days} روزه با موفقیت برای «{name}» فعال شد!** 🎯\n"
                f"• ۱ روز مانده به پایان برنامه ({days - 1} روز بعد)، پیام یادآوری مباحث به دانش‌آموز ارسال خواهد شد.",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )

            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"🗓 **{name} عزیز، برنامه درسی {days} روزه جدید شما فعال شد!** 🎯🌱\n"
                         "با قدرت شروع کنید 🚀",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            return

        # C) Stop recording button
        if "اتمام ذخیره پیام‌ها" in text or "انصراف و بازگشت" in text:
            curr_state = active_states.get(user_id, "")
            active_states[user_id] = None
            pkg_id = active_package_ids.pop(user_id, None)
            stage_num = int(curr_state.split("_")[-1]) if "RECORD_STAGE_" in curr_state else 1

            conn = database.get_connection(config.DB_PATH)
            try:
                cur = conn.execute("SELECT COUNT(*) as cnt FROM stage_messages WHERE package_id = ?", (pkg_id,))
                pkg_cnt = cur.fetchone()["cnt"] if pkg_id else 0
            finally:
                conn.close()

            if stage_num == 4:
                tot_pkg = database.get_stage_4_package_count(config.DB_PATH)
                await msg.reply_text(
                    "🎉 **بسته آموزشی بخش ۴ با موفقیت ذخیره شد!** 🟢✨\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 تعداد فایل‌های این بسته: **{pkg_cnt} فایل** (ویدیو/عکس + ویس)\n"
                    f"📊 مجموع کل بسته‌های آماده بخش ۴: **{tot_pkg} بسته**\n\n"
                    "💡 *در روز نوبت ارسال، تمامی فایل‌های این بسته به صورت خودکار، پشت سر هم و یکجا برای دانش‌آموز ارسال خواهد شد.* 🚀",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
            else:
                count = len(database.get_stage_messages(config.DB_PATH, stage_num))
                await msg.reply_text(
                    f"✅ **عملیات ثبت محتوا برای مرحله {stage_num} به پایان رسید.** 🟢\n"
                    f"مجموع پیام‌های ذخیره شده در این مرحله: {count} پیام.\n\n"
                    "به منوی اصلی مدیریت بازگشتید:",
                    parse_mode="Markdown",
                    reply_markup=get_admin_reply_keyboard()
                )
            return

        # D) State: Recording forwarded messages for a stage
        if state and state.startswith("RECORD_STAGE_"):
            stage_num = int(state.split("_")[-1])
            pkg_id = active_package_ids.get(user_id)
            if not pkg_id:
                pkg_id = int(get_iran_now().timestamp())
                active_package_ids[user_id] = pkg_id

            caption = msg.caption or (text[:50] if text else None)
            database.add_stage_message(
                config.DB_PATH,
                stage=stage_num,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=caption,
                package_id=pkg_id
            )

            conn = database.get_connection(config.DB_PATH)
            try:
                cur = conn.execute("SELECT COUNT(*) as cnt FROM stage_messages WHERE package_id = ?", (pkg_id,))
                pkg_cnt = cur.fetchone()["cnt"]
            finally:
                conn.close()

            await msg.reply_text(
                f"✅ فایل شماره {pkg_cnt} در این بسته آموزشی ذخیره شد 🟢\n"
                "*(می‌توانید فایل‌های مکمل مثل ویدیو، عکس و ویس را در ادامه فوروارد کنید یا دکمه اتمام را بزنید)*",
                parse_mode="Markdown",
                reply_markup=get_recording_reply_keyboard()
            )
            return

        # E) Bottom Keyboard navigation for Admin
        if "ساخت لینک جدید" in text:
            active_states[user_id] = "AWAITING_STUDENT_NAME"
            await msg.reply_text(
                "📝 **لطفاً نام دانش‌آموز جدید را تایپ کنید:**\n\n"
                "مثلاً بنویسید: `نگار` یا `علی` یا `سارا`\n"
                "(ربات با همین نام به او خوش‌آمد خواهد گفت: *سلام نگار عزیز 🌸*)",
                parse_mode="Markdown"
            )
            return

        elif "بارگذاری محتوای مراحل" in text:
            await msg.reply_text(
                "🔵 📁 **مرحله مورد نظر برای بارگذاری محتوا را انتخاب کنید:**",
                parse_mode="Markdown",
                reply_markup=get_stages_selection_keyboard()
            )
            return

        elif "افزودن فایل به بخش ۴" in text:
            active_states[user_id] = "RECORD_STAGE_4"
            active_package_ids[user_id] = int(get_iran_now().timestamp())
            await msg.reply_text(
                "🟣 🎙 **حالت ذخیره‌سازی بسته جدید برای «بخش ۴» فعال شد.** 💡\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📥 اکنون می‌توانید فایل‌های این نوبت را فوروارد کنید:\n"
                "• یک ویس تک و مجزا\n"
                "• یا یک ویدیو همراه با ویس توضیحات\n"
                "• یا یک تصویر همراه با ویس توضیحات\n\n"
                "✨ *ربات به صورت خودکار تمام فایل‌هایی که اکنون بفرستید را در قالب یک بسته واحد متصل ذخیره می‌کند و در روز نوبت با هم به دانش‌آموز تحویل می‌دهد.*\n\n"
                "👇 پس از ارسال فایل‌های این بسته، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** در پایین صفحه را بزنید.",
                parse_mode="Markdown",
                reply_markup=get_recording_reply_keyboard()
            )
            return

        elif "محتوای روز ۸ و ۳۰" in text:
            c8 = len(database.get_stage_messages(config.DB_PATH, 8))
            c30 = len(database.get_stage_messages(config.DB_PATH, 30))
            msg_text = (
                "🟠 🎯 **مدیریت محتوای روز ۸ و ۳۰ مشاوره:** 🏆\n\n"
                f"• فایل‌های ذخیره‌شده برای روز ۸: **{c8} پیام 🎯**\n"
                f"• فایل‌های ذخیره‌شده برای روز ۳۰: **{c30} پیام 🏆**\n\n"
                "برای بارگذاری یا ارسال دستی، دکمه مورد نظر را انتخاب کنید:"
            )
            await msg.reply_text(msg_text, parse_mode="Markdown", reply_markup=get_milestones_keyboard())
            return

        elif "برنامه درسی دوره‌ای" in text:
            students = database.list_students(config.DB_PATH)
            if not students:
                await msg.reply_text("ℹ️ هنوز هیچ دانش‌آموزی در ربات ثبت‌نام نکرده است.")
                return

            await msg.reply_text(
                "🟡 🗓 **مدیریت برنامه‌های مطالعاتی دوره‌ای:**\n\n"
                "دانش‌آموز مورد نظر را انتخاب کنید تا برنامه جدید (۷، ۸، ۹ یا دلخواه) برایش فعال شود:",
                parse_mode="Markdown",
                reply_markup=get_student_list_for_plans_keyboard(students)
            )
            return

        elif "تنظیمات ارسال خودکار" in text:
            interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
            hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))
            msg_text = (
                "⚙️ ⏱ **تنظیمات زمان‌بندی ارسال خودکار (بخش ۴):** ⚙️\n\n"
                f"• **فاصله زمانی فعلی:** هر {interval} روز یکبار 🗓\n"
                f"• **ساعت ارسال در روز نوبت:** ساعت {hour}:۰۰ شب ⏰\n\n"
                "برای تغییر فاصله یا ساعت، از دکمه‌های زیر استفاده کنید:"
            )
            await msg.reply_text(msg_text, parse_mode="Markdown", reply_markup=get_drip_settings_keyboard(interval, hour))
            return

        elif "آمار و وضعیت دانش‌آموزان" in text:
            report = generate_students_stats_report()
            await msg.reply_text(report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
            return

        elif "تست جمله انگیزشی" in text:
            await msg.reply_text("⏳ در حال آماده‌سازی و ارسال نمونه پیام انگیزشی ساعت ۷ صبح...")
            await execute_morning_quotes_cycle(context.bot, target_user_id=user_id)
            return

        elif "دریافت بکاپ دیتابیس" in text:
            await msg.reply_text("⏳ در حال آماده‌سازی و ارسال نسخه پشتیبان دیتابیس...")
            await send_database_backup_file(context.bot, user_id)
            return

        elif "منوی اصلی مدیریت" in text:
            await msg.reply_text(
                "👑 **منوی اصلی مدیریت ربات مشاوره دکتر هاشمی:**",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

    # ==========================
    # 2. STUDENT ACTIONS & BUTTONS
    # ==========================
    student = database.get_student(config.DB_PATH, user_id)
    if student:
        curr_stage = student["current_stage"]
        raw_name = student.get("assigned_name") or student.get("full_name") or "دانش‌آموز"
        name = escape_md(raw_name)

        # A) State: Student typing their study report
        st_state = active_states.get(user_id)
        if st_state == "AWAITING_STUDY_REPORT":
            active_states[user_id] = None
            report_result = database.submit_study_plan_report(config.DB_PATH, user_id, text)
            dur = report_result["duration_days"] if report_result else 7

            safe_text = escape_md(text)

            await msg.reply_text(
                f"✅ **{name} عزیز، گزارش مباحث با موفقیت برای دکتر هاشمی ارسال شد!** 🌱✨\n\n"
                "پس از بررسی، برنامه دوره بعدی برایتان تنظیم خواهد شد 🎯",
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
                    await context.bot.send_message(chat_id=admin_target, text=report_alert, parse_mode="Markdown", reply_markup=admin_kb)
                except Exception as e:
                    logger.error(f"Could not alert admin about report: {e}")
            return

        if "ارسال لیست مباحث" in text:
            active_states[user_id] = "AWAITING_STUDY_REPORT"
            await msg.reply_text(
                f"📝 **{name} عزیز، لطفاً لیست مباحثی که مطالعه کرده‌اید را تایپ کنید و بفرستید:**\n\n"
                "💡 *مثال:* زیست: گفتار ۱ فصل ۳ تمام شد و ۵۰ تست زدم. شیمی: تا صفحه ۴۰ خوانده شد و ویدیو تحلیل رو دیدم.",
                parse_mode="Markdown"
            )
            return

        if "گوش دادم" in text:
            next_stage = curr_stage + 1
            database.update_student_stage(config.DB_PATH, user_id, next_stage)

            if next_stage <= 3:
                await msg.reply_text(
                    f"👏 **آفرین {name} عزیز! مرحله {next_stage} برات باز شد.** 🌱\n"
                    "فایل‌های این مرحله ارسال می‌شوند:",
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(next_stage)
                )
                await deliver_stage_content(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1, greeting_name=name)
            else:
                completion_msg = (
                    "🎉 **تبریک ویژه! شما هر ۳ مرحله اولیه آموزش را با موفقیت به پایان رساندید.** 🏆✨\n\n"
                    "📌 **حتماً این صفحه و چت را پین (Pin) کنید تا همیشه در دسترستان باشد.**\n\n"
                    "🎙 **هر چند وقت یک‌بار، مهم‌ترین مطالب، ویس‌ها و نکات آموزشی جدید در همین صفحه برای شما ارسال می‌شود و حتماً باید آن‌ها را با دقت گوش دهید.** 🌱"
                )
                await msg.reply_text(
                    completion_msg,
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(4)
                )
            return

        elif "ارسال مجدد" in text:
            if curr_stage <= 3:
                await deliver_stage_content(context.bot, user_id, stage=curr_stage, next_stage=curr_stage + 1, greeting_name=name)
            else:
                await msg.reply_text(
                    f"📌 **{name} عزیز، این چت را پین داشته باشید.** 🌱\n"
                    "مهم‌ترین مطالب و ویس‌های جدید به زودی در همین صفحه برای شما ارسال می‌شود.",
                    parse_mode="Markdown",
                    reply_markup=get_student_reply_keyboard(4)
                )
            return

        elif "پیام به دکتر هاشمی" in text:
            await msg.reply_text(
                "💬 **ارتباط مستقیم با دکتر هاشمی:**\n\n"
                "جهت ارسال گزارش کار، سوالات درسی یا ارتباط با مشاور، وارد پی‌وی زیر شوید:\n"
                "👉 @Hojjat_hshmi 🩺",
                parse_mode="Markdown",
                reply_markup=get_student_reply_keyboard(curr_stage)
            )
            return

# --- Background Automated Scheduler Loop ---

async def drip_scheduler_loop(app):
    logger.info("Background automated scheduler loop started.")
    last_run_day = None

    while True:
        try:
            now_iran = get_iran_now()
            today_str = now_iran.strftime("%Y-%m-%d")

            # 1. Daily 7:00 AM Morning Motivational Quotes Check
            if now_iran.hour == 7:
                quotes_sent = await execute_morning_quotes_cycle(app.bot)
                if quotes_sent > 0:
                    logger.info(f"Daily 7:00 AM motivational quotes delivered to {quotes_sent} students.")

            # 2. Daily Nightly Drip & Milestones Check (Default 23:00)
            target_hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))
            if now_iran.hour == target_hour and last_run_day != today_str:
                logger.info(f"Target hour {target_hour}:00 reached in Iran. Running daily scheduled deliveries...")
                milestones_sent = await execute_milestones_check(app.bot)
                drip_sent = await execute_drip_cycle(app.bot, force_ignore_interval=False)
                last_run_day = today_str
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
                logger.info(f"Sent {inactivity_warns} 2-day inactivity warnings to students.")

        except Exception as e:
            logger.error(f"Error in drip_scheduler_loop: {e}")

        await asyncio.sleep(60)

# --- Application Setup ---

def create_application():
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in config or environment variables!")
        return None

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_|^drip_|^plan_"))
    app.add_handler(CallbackQueryHandler(student_callback_handler, pattern="^student_"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_collector))

    return app

def main():
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
        asyncio.create_task(drip_scheduler_loop(application))

    app.post_init = post_init

    logger.info("Bot starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
