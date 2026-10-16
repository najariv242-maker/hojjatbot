import os
import sys
import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

import config
import database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Active states: "AWAITING_STUDENT_NAME", "RECORD_STAGE_...", "AWAITING_CUSTOM_DAYS_<user_id>", "AWAITING_STUDY_REPORT"
active_states: Dict[int, Optional[str]] = {}

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

def is_admin(user_id: int, username: Optional[str]) -> bool:
    if config.ADMIN_ID and user_id == config.ADMIN_ID:
        return True
    if username and config.ADMIN_USERNAME and username.lower() == config.ADMIN_USERNAME.lower():
        return True
    return False

# --- Persistent Bottom Reply Keyboards (With Vivid Color Badges) ---

def get_admin_reply_keyboard():
    keyboard = [
        [KeyboardButton("🟢 🔗 ساخت لینک جدید (با نام) 👤"), KeyboardButton("🔵 📁 بارگذاری محتوای مراحل 📥")],
        [KeyboardButton("🟣 🎙 افزودن فایل به بخش ۴ 💡"), KeyboardButton("🟠 🎯 محتوای روز ۸ و ۳۰ 🏆")],
        [KeyboardButton("🟡 🗓 برنامه درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯"), KeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار ⚙️")],
        [KeyboardButton("📊 آمار و وضعیت دانش‌آموزان 📈"), KeyboardButton("👑 🏠 منوی اصلی مدیریت 👑")]
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
        [InlineKeyboardButton("🟢 🔗 ساخت لینک اختصاصی (با نام) 👤", callback_data="admin_ask_name")],
        [InlineKeyboardButton("🔵 📁 مدیریت و بارگذاری محتوای مراحل 📥", callback_data="admin_manage_stages")],
        [InlineKeyboardButton("🟣 🎙 افزودن ویس به بخش ۴ 💡", callback_data="admin_record_4")],
        [InlineKeyboardButton("🟠 🎯 تنظیم محتوای روز ۸ و ۳۰ 🏆", callback_data="admin_milestones_menu")],
        [InlineKeyboardButton("🟡 🗓 برنامه‌ریزی درسی دوره‌ای (۷، ۸، ۹ روزه) 🎯", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("⚙️ ⏱ تنظیمات ارسال خودکار بخش ۴ ⚙️", callback_data="admin_drip_settings")],
        [InlineKeyboardButton("📊 📈 لیست و وضعیت دقیق دانش‌آموزان 📈", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stages_selection_keyboard():
    keyboard = [
        [InlineKeyboardButton("🟢 📥 بارگذاری مرحله ۱ 🎓", callback_data="admin_record_1")],
        [InlineKeyboardButton("🔵 📥 بارگذاری مرحله ۲ 📚", callback_data="admin_record_2")],
        [InlineKeyboardButton("🟣 📥 بارگذاری مرحله ۳ 🧠", callback_data="admin_record_3")],
        [InlineKeyboardButton("🟠 🎙 بارگذاری فایل‌های بخش ۴ 💡", callback_data="admin_record_4")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_milestones_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎯 📥 بارگذاری محتوای روز ۸ مشاوره 🎯", callback_data="admin_record_8")],
        [InlineKeyboardButton("🏆 📥 بارگذاری محتوای روز ۳۰ مشاوره 🏆", callback_data="admin_record_30")],
        [InlineKeyboardButton("⚡️ 🚀 ارسال فوری نوبت‌های روز ۸ و ۳۰ ⚡️", callback_data="admin_force_milestones")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_drip_settings_keyboard(interval_days: int, hour: int):
    keyboard = [
        [InlineKeyboardButton("⚡️ 🚀 ارسال نوبت امروز (اجرای دستی) ⚡️", callback_data="admin_force_drip")],
        [
            InlineKeyboardButton(f"هر ۱ روز {'✅' if interval_days == 1 else ''}", callback_data="drip_set_int_1"),
            InlineKeyboardButton(f"هر ۲ روز {'✅' if interval_days == 2 else ''}", callback_data="drip_set_int_2"),
            InlineKeyboardButton(f"هر ۳ روز {'✅' if interval_days == 3 else ''}", callback_data="drip_set_int_3"),
        ],
        [
            InlineKeyboardButton(f"ساعت ۱۰ شب (۲۲){' ✅' if hour == 22 else ''}", callback_data="drip_set_hour_22"),
            InlineKeyboardButton(f"ساعت ۱۱ شب (۲۳){' ✅' if hour == 23 else ''}", callback_data="drip_set_hour_23"),
        ],
        [InlineKeyboardButton("🔴 🔙 بازگشت به منوی مدیریت 🏠", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_list_for_plans_keyboard(students: list):
    keyboard = []
    row = []
    for st in students:
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        uid = st["user_id"]
        row.append(InlineKeyboardButton(f"👤 {name}", callback_data=f"plan_user_{uid}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔴 🔙 بازگشت به منوی اصلی 🏠", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_plan_duration_keyboard(target_user_id: int):
    keyboard = [
        [
            InlineKeyboardButton("🟢 🗓 برنامه ۷ روزه 🎯", callback_data=f"plan_set_{target_user_id}_7"),
            InlineKeyboardButton("🔵 🗓 برنامه ۸ روزه 🎯", callback_data=f"plan_set_{target_user_id}_8"),
            InlineKeyboardButton("🟣 🗓 برنامه ۹ روزه 🎯", callback_data=f"plan_set_{target_user_id}_9"),
        ],
        [InlineKeyboardButton("🟠 ✏️ تعداد روز دلخواه (دستی)", callback_data=f"plan_custom_{target_user_id}")],
        [InlineKeyboardButton("🔴 🔙 بازگشت به لیست دانش‌آموزان 👥", callback_data="admin_plans_menu")]
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

    name_phrase = f"{greeting_name} عزیز، " if greeting_name else ""
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
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, callback_data=f"student_goto_{next_stage}")]])

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

    for st, msg in eligible_pairs:
        user_id = st["user_id"]
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
        caption_text = f"\n\n{msg.get('caption')}" if msg.get("caption") else ""
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🌸 **سلام {name} عزیز، وقتت بخیر!** 🌱\n"
                     f"🎙 مبحث تکمیلی جدید از طرف دکتر هاشمی برای شما ارسال می‌شود:{caption_text}",
                parse_mode="Markdown"
            )
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=msg["from_chat_id"],
                message_id=msg["message_id"]
            )
            database.record_drip_delivery(config.DB_PATH, user_id, msg["id"])
            sent_count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error delivering drip message to student {user_id}: {e}")

    return sent_count

async def execute_milestones_check(bot) -> int:
    due_list = database.get_students_due_for_milestones(config.DB_PATH)
    sent_count = 0

    for st, milestone in due_list:
        user_id = st["user_id"]
        name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"
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
        name = plan.get("assigned_name") or plan.get("full_name") or "دانش‌آموز"
        duration = plan["duration_days"]

        msg = (
            f"⚠️🔔 **یادآوری بسیار مهم برنامه درسی ({duration} روزه):**\n\n"
            f"🌸 **{name} عزیز، فردا برنامه مطالعاتی شما به پایان می‌رسد!** ⏳\n\n"
            "📌 لطفاً حتماً قبل از گزارش کارت برای دکتر هاشمی، لیست تمام مباحثی که تا این لحظه خوانده‌اید را ارسال کنید.\n"
            "💡 *راهنمای نمونه:* بنویسید کدام مباحث را خوانده‌اید، چند تا تست زده‌اید، و اگر ویدیو دیده‌اید تا کجای ویدیو پیش رفته‌اید.\n\n"
            "👇 *برای ارسال گزارش، دکمه زیر را لمس کرده و متنتان را بفرستید:*"
        )
        report_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🟣 📝 ارسال لیست مباحث خوانده‌شده 📩", callback_data="student_start_report")
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
            salutation = f"سلام {display_name} عزیز 🌸"
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
                    uname = f"@{username}" if username else "بدون یوزرنیم"
                    await context.bot.send_message(
                        chat_id=config.ADMIN_ID,
                        text=f"🔔 **دانش‌آموز جدید ثبت‌نام شد:** 👤\n\n"
                             f"• نام: **{display_name}** ({uname})\n"
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
        reply_kb = get_student_reply_keyboard(stage)
        if stage <= 3:
            await update.message.reply_text(
                f"👋 **سلام مجدد {name} عزیز.** 🌱\n"
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

    elif data.startswith("admin_record_"):
        stage_num = int(data.split("_")[-1])
        active_states[user_id] = f"RECORD_STAGE_{stage_num}"
        if stage_num <= 3:
            stage_title = f"مرحله {stage_num}"
        elif stage_num == 4:
            stage_title = "بخش ۴ (مطالب مهم جدید)"
        elif stage_num == 8:
            stage_title = "محتوای روز هشتم مشاوره 🎯"
        elif stage_num == 30:
            stage_title = "محتوای روز سی‌ام مشاوره 🏆"
        else:
            stage_title = f"بخش {stage_num}"

        await query.message.reply_text(
            f"🎙 **حالت ذخیره‌سازی برای «{stage_title}» فعال شد.**\n\n"
            "📥 هم‌اکنون پیام‌ها، ویس‌ها، متن‌ها و فایل‌ها را از گروه مشاوره به این چت فوروارد کنید.\n"
            "هر پیامی بفرستید خودکار در آرشیو ثبت می‌شود 🟢\n"
            "👇 پس از اتمام، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** در پایین صفحه را بزنید.",
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
        students = database.list_students(config.DB_PATH)
        counts = database.get_student_count_by_stage(config.DB_PATH)

        s1 = counts.get(1, 0)
        s2 = counts.get(2, 0)
        s3 = counts.get(3, 0)
        s4 = counts.get(4, 0)
        total = len(students)

        report = (
            "📊 **آمار و وضعیت دانش‌آموزان مشاوره:** 📈\n\n"
            f"👥 کل دانش‌آموزان ثبت‌شده: {total} نفر\n"
            f"• در حال گذراندن مرحله ۱: {s1} نفر 🎓\n"
            f"• در حال گذراندن مرحله ۲: {s2} نفر 📚\n"
            f"• در حال گذراندن مرحله ۳: {s3} نفر 🧠\n"
            f"• وارد شده به فاز ۴: {s4} نفر 🚀\n\n"
            "📋 **لیست دانش‌آموزان:**\n"
        )
        now = datetime.utcnow()
        for idx, st in enumerate(students[:20], start=1):
            disp_name = st.get("assigned_name") or st.get("full_name")
            uname = f"@{st['username']}" if st["username"] else "بدون یوزرنیم"
            reg_str = st.get("registered_at")
            days_ago = ""
            if reg_str:
                try:
                    dt = datetime.fromisoformat(str(reg_str).replace("Z", ""))
                    days_ago = f" (روز {(now - dt).days + 1})"
                except Exception:
                    pass
            d8 = "✅" if st.get("day_8_sent") else "⏳"
            d30 = "✅" if st.get("day_30_sent") else "⏳"
            report += f"{idx}. **{disp_name}** ({uname}){days_ago} ⬅️ مرحله {st['current_stage']} | روز۸: {d8} | روز۳۰: {d30}\n"

        if total == 0:
            report += "هنوز دانش‌آموزی ثبت‌نام نکرده است.\n"

        await query.message.reply_text(report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())

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

            reply_text = (
                f"🔗 **لینک دعوت اختصاصی برای «{student_name}» ساخته شد:**\n\n"
                f"`{invite_link}`\n\n"
                "📌 **نکات مهم:**\n"
                f"• این لینک یکبارمصرف است و مختص **{student_name}** می‌باشد.\n"
                f"• به محض باز کردن لینک، ربات با عبارت **«سلام {student_name} عزیز 🌸»** به او خوش‌آمد می‌گوید و به طور خودکار به لیست دانش‌آموزان شما اضافه خواهد شد."
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
            name = st.get("assigned_name") or st.get("full_name") or "دانش‌آموز"

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
            stage_num = int(curr_state.split("_")[-1]) if "RECORD_STAGE_" in curr_state else 1
            count = len(database.get_stage_messages(config.DB_PATH, stage_num))
            await msg.reply_text(
                f"✅ **عملیات ثبت محتوا به پایان رسید.** 🟢\n"
                f"مجموع پیام‌های ذخیره شده در این بخش: {count} پیام.\n\n"
                "به منوی اصلی مدیریت بازگشتید:",
                parse_mode="Markdown",
                reply_markup=get_admin_reply_keyboard()
            )
            return

        # D) State: Recording forwarded messages for a stage
        if state and state.startswith("RECORD_STAGE_"):
            stage_num = int(state.split("_")[-1])
            caption = msg.caption or (text[:50] if text else None)
            database.add_stage_message(
                config.DB_PATH,
                stage=stage_num,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=caption
            )
            count = len(database.get_stage_messages(config.DB_PATH, stage_num))
            await msg.reply_text(
                f"✅ فایل شماره {count} برای بخش {stage_num} ذخیره شد 🟢",
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
            await msg.reply_text(
                "🟣 🎙 **حالت ذخیره‌سازی برای «بخش ۴ (ویس‌های جدید)» فعال شد.** 💡\n\n"
                "📥 ویس‌ها، فیلم‌ها و پیام‌های جدید را فوروارد کنید.\n"
                "👇 پس از اتمام، دکمه **«🟢 ✅ اتمام ذخیره پیام‌ها 🟢»** در پایین صفحه را بزنید.",
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
            students = database.list_students(config.DB_PATH)
            counts = database.get_student_count_by_stage(config.DB_PATH)
            s1 = counts.get(1, 0)
            s2 = counts.get(2, 0)
            s3 = counts.get(3, 0)
            s4 = counts.get(4, 0)
            report = (
                "📊 **آمار و وضعیت دانش‌آموزان مشاوره:** 📈\n\n"
                f"👥 کل دانش‌آموزان ثبت‌شده: {len(students)} نفر\n"
                f"• در حال گذراندن مرحله ۱: {s1} نفر 🎓\n"
                f"• در حال گذراندن مرحله ۲: {s2} نفر 📚\n"
                f"• در حال گذراندن مرحله ۳: {s3} نفر 🧠\n"
                f"• وارد شده به فاز ۴: {s4} نفر 🚀\n\n"
                "📋 **لیست دانش‌آموزان:**\n"
            )
            now = datetime.utcnow()
            for idx, st in enumerate(students[:20], start=1):
                disp_name = st.get("assigned_name") or st.get("full_name")
                uname = f"@{st['username']}" if st["username"] else "بدون یوزرنیم"
                reg_str = st.get("registered_at")
                days_ago = ""
                if reg_str:
                    try:
                        dt = datetime.fromisoformat(str(reg_str).replace("Z", ""))
                        days_ago = f" (روز {(now - dt).days + 1})"
                    except Exception:
                        pass
                d8 = "✅" if st.get("day_8_sent") else "⏳"
                d30 = "✅" if st.get("day_30_sent") else "⏳"
                report += f"{idx}. **{disp_name}** ({uname}){days_ago} ⬅️ مرحله {st['current_stage']} | روز۸: {d8} | روز۳۰: {d30}\n"

            if len(students) == 0:
                report += "هنوز دانش‌آموزی ثبت‌نام نکرده است.\n"

            await msg.reply_text(report, parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
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
        name = student.get("assigned_name") or student.get("full_name") or "دانش‌آموز"

        # A) State: Student typing their study report
        st_state = active_states.get(user_id)
        if st_state == "AWAITING_STUDY_REPORT":
            active_states[user_id] = None
            report_result = database.submit_study_plan_report(config.DB_PATH, user_id, text)
            dur = report_result["duration_days"] if report_result else 7

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
                    f"📝 *متن گزارش:*\n{text}\n\n"
                    f"🎯 *برنامه دوره قبلی:* {dur} روزه (تکمیل شد)\n"
                    "👇 جهت تایید و فعال‌سازی برنامه دوره جدید، دکمه زیر را لمس کنید:"
                )
                admin_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"➕ تنظیم برنامه دوره جدید برای {name}", callback_data=f"plan_user_{user_id}")
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
            target_hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))

            today_str = now_iran.strftime("%Y-%m-%d")
            if now_iran.hour == target_hour and last_run_day != today_str:
                logger.info(f"Target hour {target_hour}:00 reached in Iran. Running daily scheduled deliveries...")
                milestones_sent = await execute_milestones_check(app.bot)
                drip_sent = await execute_drip_cycle(app.bot, force_ignore_interval=False)
                last_run_day = today_str
                logger.info(f"Daily cycle finished. Milestones: {milestones_sent}, Drip: {drip_sent}")

            plan_reminders = await execute_study_plans_reminders(app.bot)
            if plan_reminders > 0:
                logger.info(f"Sent {plan_reminders} study plan 1-day reminders.")

        except Exception as e:
            logger.error(f"Error in drip_scheduler_loop: {e}")

        await asyncio.sleep(600)

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
