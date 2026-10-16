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

admin_active_state: Dict[int, Optional[str]] = {}

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

def get_admin_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔗 ساخت لینک اختصاصی دانش‌آموز (با نام)", callback_data="admin_ask_name")],
        [InlineKeyboardButton("📁 مدیریت و بارگذاری محتوای مراحل", callback_data="admin_manage_stages")],
        [InlineKeyboardButton("🎙 افزودن ویس جدید به بخش ۴", callback_data="admin_record_4")],
        [InlineKeyboardButton("⏱ تنظیمات ارسال خودکار بخش ۴", callback_data="admin_drip_settings")],
        [InlineKeyboardButton("📊 لیست و وضعیت دقیق دانش‌آموزان", callback_data="admin_stats")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stages_selection_keyboard():
    keyboard = [
        [InlineKeyboardButton("📥 بارگذاری مرحله ۱", callback_data="admin_record_1")],
        [InlineKeyboardButton("📥 بارگذاری مرحله ۲", callback_data="admin_record_2")],
        [InlineKeyboardButton("📥 بارگذاری مرحله ۳", callback_data="admin_record_3")],
        [InlineKeyboardButton("🎙 بارگذاری فایل‌های بخش ۴", callback_data="admin_record_4")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_stop_recording_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ اتمام ذخیره پیام‌ها", callback_data="admin_stop_recording")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_drip_settings_keyboard(interval_days: int, hour: int):
    keyboard = [
        [InlineKeyboardButton("🚀 ارسال نوبت امروز (اجرای دستی)", callback_data="admin_force_drip")],
        [
            InlineKeyboardButton(f"هر ۱ روز {'✅' if interval_days == 1 else ''}", callback_data="drip_set_int_1"),
            InlineKeyboardButton(f"هر ۲ روز {'✅' if interval_days == 2 else ''}", callback_data="drip_set_int_2"),
            InlineKeyboardButton(f"هر ۳ روز {'✅' if interval_days == 3 else ''}", callback_data="drip_set_int_3"),
        ],
        [
            InlineKeyboardButton(f"ساعت ۱۰ شب (۲۲){' ✅' if hour == 22 else ''}", callback_data="drip_set_hour_22"),
            InlineKeyboardButton(f"ساعت ۱۱ شب (۲۳){' ✅' if hour == 23 else ''}", callback_data="drip_set_hour_23"),
        ],
        [InlineKeyboardButton("🔙 بازگشت به منوی مدیریت", callback_data="admin_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_stage_keyboard(next_stage: int):
    if next_stage <= 3:
        btn_text = f"✅ گوش دادم | رفتن به مرحله {next_stage} ➡️"
        callback = f"student_goto_{next_stage}"
    else:
        btn_text = "🎉 اتمام دوره اصلی | ورود به فاز ۴ 🚀"
        callback = "student_goto_4"
    keyboard = [
        [InlineKeyboardButton(btn_text, callback_data=callback)],
        [InlineKeyboardButton("🔄 ارسال مجدد ویس‌های همین مرحله", callback_data="student_repeat_current")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_student_hub_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔄 دریافت مجدد مطالب مرحله فعلی", callback_data="student_repeat_current")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def deliver_stage_content(bot, chat_id: int, stage: int, next_stage: int, greeting_name: str = ""):
    msgs = database.get_stage_messages(config.DB_PATH, stage)
    if not msgs:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📌 محتوای **مرحله {stage}** به زودی توسط دکتر هاشمی بارگذاری می‌شود.",
            parse_mode="Markdown"
        )
        return

    name_phrase = f"{greeting_name} جان، " if greeting_name else ""
    await bot.send_message(
        chat_id=chat_id,
        text=f"📚 **{name_phrase}محتوای مرحله {stage} برای شما ارسال می‌شود:**\nلطفاً ویس‌ها و نکات زیر را با دقت گوش داده و یادداشت‌برداری کنید 🌱",
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

    await bot.send_message(
        chat_id=chat_id,
        text=f"✨ تمام ویس‌های **مرحله {stage}** ارسال شد.\nپس از گوش دادن کامل به این بخش، دکمه زیر را لمس کنید تا مرحله بعدی برای شما باز شود:",
        parse_mode="Markdown",
        reply_markup=get_student_stage_keyboard(next_stage)
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
                text=f"🌸 **سلام {name} جان، وقتت بخیر!**\n🎙 مبحث تکمیلی جدید امروز از طرف دکتر هاشمی برای شما ارسال می‌شود:{caption_text}",
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

async def start_handler(update, context):
    if not update or not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or "دانش‌آموز"
    username = update.effective_user.username

    if is_admin(user_id, username):
        await update.message.reply_text(
            "👑 **سلام آقای دکتر هاشمی گرامی**\nبه پنل اختصاصی مدیریت ربات مشاوره تحصیلی خوش آمدید 🩺✨",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )
        return

    args = context.args if context else []
    if args and len(args) > 0 and args[0].startswith("inv_"):
        code = args[0]
        success, assigned_name = database.validate_and_use_invite(
            config.DB_PATH, code, user_id, username, user_name
        )
        if success:
            salutation = f"سلام {assigned_name} جان 🌸" if assigned_name else f"سلام {user_name} عزیز 🌸"
            await update.message.reply_text(
                f"✨ **{salutation}**\n\nبه ربات مشاوره تحصیلی دکتر هاشمی خوش آمدید 🌱\nفرآیند آموزشی شما در ۳ مرحله طراحی شده است.\nآماده‌اید؟ مرحله اول همین الان برای شما ارسال می‌شود:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=1, next_stage=2, greeting_name=assigned_name or "")
            return
        else:
            await update.message.reply_text(
                "❌ **لینک دعوت نامعتبر یا منقضی شده است.**\nاین لینک قبلاً استفاده شده است یا وجود ندارد. لطفاً جهت دریافت لینک جدید با دکتر هاشمی در ارتباط باشید.",
                parse_mode="Markdown"
            )
            return

    student = database.get_student(config.DB_PATH, user_id)
    if student:
        stage = student["current_stage"]
        name = student.get("assigned_name") or user_name
        if stage <= 3:
            await update.message.reply_text(
                f"👋 **سلام مجدد {name} جان.**\nشما در حال حاضر در **مرحله {stage}** هستید. محتوای این مرحله مجدداً برای شما ارسال می‌شود:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=stage, next_stage=stage + 1, greeting_name=name)
        else:
            interval = database.get_setting(config.DB_PATH, "drip_interval_days", "2")
            hour = database.get_setting(config.DB_PATH, "drip_send_hour", "23")
            await update.message.reply_text(
                f"🎉 **سلام {name} جان!**\n\nشما دوره‌های ۳ مرحله‌ای اولیه را تکمیل کرده‌اید و وارد **فاز ۴** شده‌اید.\n🎙 ویس‌ها و نکات تکمیلی جدید به صورت منظم (هر {interval} روز یکبار رأس ساعت {hour}:۰۰ شب) خودکار برای شما در همین چت ارسال خواهد شد ✨",
                parse_mode="Markdown",
                reply_markup=get_student_hub_keyboard()
            )
        return

    await update.message.reply_text(
        "⛔️ **دسترسی تایید نشده است**\n\nورود به ربات مشاوره فقط با «لینک دعوت اختصاصی» که دکتر هاشمی برایتان ارسال می‌کند امکان‌پذیر است.",
        parse_mode="Markdown"
    )

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
        admin_active_state[user_id] = None
        await query.edit_message_text(
            "👑 **پنل مدیریت ربات مشاوره دکتر هاشمی:**",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )

    elif data == "admin_ask_name":
        admin_active_state[user_id] = "AWAITING_STUDENT_NAME"
        await query.message.reply_text(
            "📝 **لطفاً نام دانش‌آموز جدید را تایپ کنید:**\n\nمثلاً بنویسید: `نگار` یا `علی` یا `سارا`\n(ربات با همین نام به او خوش‌آمد خواهد گفت: *سلام نگار جان*)",
            parse_mode="Markdown"
        )

    elif data == "admin_manage_stages":
        await query.edit_message_text(
            "📁 **مرحله مورد نظر برای بارگذاری محتوا را انتخاب کنید:**\n\nپس از انتخاب، پیام‌ها را از گروه مشاوره مستقیماً به اینجا فوروارد کنید.",
            parse_mode="Markdown",
            reply_markup=get_stages_selection_keyboard()
        )

    elif data.startswith("admin_record_"):
        stage_num = int(data.split("_")[-1])
        admin_active_state[user_id] = f"RECORD_STAGE_{stage_num}"
        stage_title = f"مرحله {stage_num}" if stage_num <= 3 else "بخش ۴ (ویس‌های جدید و زمان‌بندی‌شده)"

        await query.edit_message_text(
            f"🎙 **حالت ذخیره‌سازی برای «{stage_title}» فعال شد.**\n\n📥 هم‌اکنون پیام‌ها، ویس‌ها، متن‌ها و فیلم‌های این مرحله را از گروه مشاوره به این چت فوروارد کنید.\nهر پیامی بفرستید خودکار در آرشیو ثبت می‌شود.\nوقتی تمام پیام‌های این بخش را فرستادید، دکمه «اتمام ذخیره» را بزنید.",
            parse_mode="Markdown",
            reply_markup=get_stop_recording_keyboard()
        )

    elif data == "admin_stop_recording":
        curr_state = admin_active_state.get(user_id, "")
        stage_num = int(curr_state.split("_")[-1]) if "RECORD_STAGE_" in curr_state else 1
        admin_active_state[user_id] = None
        count = len(database.get_stage_messages(config.DB_PATH, stage_num))

        await query.edit_message_text(
            f"✅ **ذخیره پیام‌های مرحله با موفقیت به پایان رسید.**\nمجموع پیام‌های ذخیره شده در این مرحله: {count} پیام.\n\nبه پنل اصلی بازگشتید.",
            parse_mode="Markdown",
            reply_markup=get_admin_main_keyboard()
        )

    elif data == "admin_drip_settings":
        interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
        hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))

        msg = (
            "⏱ **تنظیمات زمان‌بندی ارسال خودکار (بخش ۴):**\n\n"
            f"• **فاصله زمانی فعلی:** هر {interval} روز یکبار\n"
            f"• **ساعت ارسال در روز نوبت:** ساعت {hour}:۰۰ شب\n\n"
            "💡 *دانش‌آموزانی که ۳ مرحله را تمام کرده‌اند، طبق این برنامه به صورت خودکار ویس‌های بخش ۴ را یکی پس از دیگری دریافت می‌کنند.*\n"
            "برای تغییر فاصله یا ساعت، دکمه‌های زیر را لمس کنید:"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_drip_settings_keyboard(interval, hour))

    elif data.startswith("drip_set_int_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_interval_days", val)
        interval = int(val)
        hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))
        await query.edit_message_reply_markup(reply_markup=get_drip_settings_keyboard(interval, hour))
        await query.message.reply_text(f"✅ فاصله ارسال به **هر {val} روز یکبار** تغییر یافت.")

    elif data.startswith("drip_set_hour_"):
        val = data.split("_")[-1]
        database.set_setting(config.DB_PATH, "drip_send_hour", val)
        hour = int(val)
        interval = int(database.get_setting(config.DB_PATH, "drip_interval_days", "2"))
        await query.edit_message_reply_markup(reply_markup=get_drip_settings_keyboard(interval, hour))
        await query.message.reply_text(f"✅ ساعت ارسال روزانه به **ساعت {val}:۰۰ شب** تغییر یافت.")

    elif data == "admin_force_drip":
        await query.message.reply_text("⏳ در حال بررسی و ارسال نوبت ویس جدید به دانش‌آموزان واجد شرایط فاز ۴...")
        sent = await execute_drip_cycle(context.bot, force_ignore_interval=True)
        if sent > 0:
            await query.message.reply_text(f"🚀 **موفق:** تعداد {sent} ویس جدید برای دانش‌آموزان مرحله ۴ ارسال شد.")
        else:
            await query.message.reply_text("ℹ️ دانش‌آموز واجد شرایط جدیدی یافت نشد (یا همه ویس‌های بخش ۴ را قبلاً دریافت کرده‌اند یا هنوز دانش‌آموزی وارد فاز ۴ نشده است).")

    elif data == "admin_stats":
        students = database.list_students(config.DB_PATH)
        counts = database.get_student_count_by_stage(config.DB_PATH)

        s1 = counts.get(1, 0)
        s2 = counts.get(2, 0)
        s3 = counts.get(3, 0)
        s4 = counts.get(4, 0)
        total = len(students)

        report = (
            "📊 **آمار و وضعیت دانش‌آموزان مشاوره:**\n\n"
            f"👥 کل دانش‌آموزان: {total} نفر\n"
            f"• در حال گذراندن مرحله ۱: {s1} نفر\n"
            f"• در حال گذراندن مرحله ۲: {s2} نفر\n"
            f"• در حال گذراندن مرحله ۳: {s3} نفر\n"
            f"• وارد شده به فاز ۴ (ارسال خودکار): {s4} نفر\n\n"
            "📋 **لیست دانش‌آموزان:**\n"
        )
        for idx, st in enumerate(students[:15], start=1):
            disp_name = st.get("assigned_name") or st.get("full_name")
            uname = f"@{st['username']}" if st["username"] else "بدون یوزرنیم"
            report += f"{idx}. **{disp_name}** ({uname}) ⬅️ مرحله {st['current_stage']}\n"

        if total == 0:
            report += "هنوز دانش‌آموزی ثبت‌نام نکرده است.\n"

        await query.message.reply_text(report, parse_mode="Markdown")

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
                f"👏 **آفرین {name} جان! مرحله {next_stage} برات باز شد.**\nفایل‌های این مرحله ارسال می‌شوند:",
                parse_mode="Markdown"
            )
            await deliver_stage_content(context.bot, user_id, stage=next_stage, next_stage=next_stage + 1, greeting_name=name)
        else:
            interval = database.get_setting(config.DB_PATH, "drip_interval_days", "2")
            hour = database.get_setting(config.DB_PATH, "drip_send_hour", "23")
            await query.message.reply_text(
                f"🎉 **تبریک ویژه {name} جان! شما هر ۳ مرحله اولیه آموزش را به پایان رساندید.**\n\nاز این پس شما وارد **فاز ۴ مشاوره** شدید. ویس‌ها و نکات مشاوره‌ای مکمل به صورت قطره‌چکانی و منظم (هر {interval} روز ساعت {hour}:۰۰ شب) خودکار برای شما در همین صفحه ارسال خواهد شد 🌱",
                parse_mode="Markdown",
                reply_markup=get_student_hub_keyboard()
            )

    elif data == "student_repeat_current":
        curr_stage = student["current_stage"]
        if curr_stage <= 3:
            await deliver_stage_content(context.bot, user_id, stage=curr_stage, next_stage=curr_stage + 1, greeting_name=name)
        else:
            await query.message.reply_text(
                f"🌸 **{name} جان، شما در فاز ۴ هستید.**\nویس‌های جدید طبق برنامه زمان‌بندی خودکار برایتان ارسال می‌شود.",
                parse_mode="Markdown"
            )

async def admin_message_collector(update, context):
    if not update or not update.message:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username
    if not is_admin(user_id, username):
        return

    state = admin_active_state.get(user_id)
    msg = update.message

    if state == "AWAITING_STUDENT_NAME":
        student_name = msg.text.strip() if msg.text else "دانش‌آموز"
        admin_active_state[user_id] = None

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
            f"• به محض باز کردن لینک، ربات با عبارت **«سلام {student_name} جان»** به او خوش‌آمد می‌گوید و مرحله اول را آغاز می‌کند."
        )
        await msg.reply_text(reply_text, parse_mode="Markdown")
        return

    if state and state.startswith("RECORD_STAGE_"):
        stage_num = int(state.split("_")[-1])
        caption = msg.caption or (msg.text[:50] if msg.text else None)

        database.add_stage_message(
            config.DB_PATH,
            stage=stage_num,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            caption=caption
        )

        stage_title = f"مرحله {stage_num}" if stage_num <= 3 else "بخش ۴"
        count = len(database.get_stage_messages(config.DB_PATH, stage_num))

        await msg.reply_text(
            f"✅ فایل شماره {count} برای **{stage_title}** ذخیره شد.",
            parse_mode="Markdown"
        )

async def drip_scheduler_loop(app):
    logger.info("Drip automated scheduler loop started.")
    last_run_day = None

    while True:
        try:
            now_iran = get_iran_now()
            target_hour = int(database.get_setting(config.DB_PATH, "drip_send_hour", "23"))

            today_str = now_iran.strftime("%Y-%m-%d")
            if now_iran.hour == target_hour and last_run_day != today_str:
                logger.info(f"Target hour {target_hour}:00 reached in Iran. Executing scheduled drip...")
                sent = await execute_drip_cycle(app.bot, force_ignore_interval=False)
                last_run_day = today_str
                logger.info(f"Scheduled drip finished. Sent to {sent} students.")

        except Exception as e:
            logger.error(f"Error in drip_scheduler_loop: {e}")

        await asyncio.sleep(600)

def create_application():
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is not set in config or environment variables!")
        return None

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_|^drip_"))
    app.add_handler(CallbackQueryHandler(student_callback_handler, pattern="^student_"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_message_collector))

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
