import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# Config & Globals
# =========================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set in .env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lovebot")

geolocator = Nominatim(user_agent="lovebot")

# Conversation states
NAME, AGE, GENDER, LOCATION, LOOKING_FOR, AGE_RANGE, BIO, PHOTO = range(8)
BROWSING = 100
EDIT_NAME, EDIT_AGE, EDIT_BIO, EDIT_PHOTO = range(200, 204)

# =========================
# Database helpers (Postgres)
# =========================

def db_execute(sql: str, params: tuple = (), fetch: Optional[str] = None):
    """Run SQL with automatic connection management.
    fetch: None | 'one' | 'all'
    Returns dict or list[dict] when fetching.
    """
    with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None


def init_db():
    # Create tables if not exist
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            name TEXT,
            age INT,
            gender TEXT,
            city TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            looking_for TEXT,
            min_age INT,
            max_age INT,
            bio TEXT,
            photo_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS likes (
            from_user BIGINT NOT NULL,
            to_user BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (from_user, to_user)
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            user1 BIGINT NOT NULL,
            user2 BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user1, user2)
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS viewed_profiles (
            viewer_user BIGINT NOT NULL,
            viewed_user BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (viewer_user, viewed_user)
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS age_changes (
            user_id BIGINT NOT NULL,
            old_age INT NOT NULL,
            new_age INT NOT NULL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


# =========================
# Model helpers
# =========================

def user_exists(user_id: int) -> bool:
    row = db_execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,), fetch="one")
    return bool(row)


def get_user(user_id: int):
    return db_execute("SELECT * FROM users WHERE user_id=%s", (user_id,), fetch="one")


def upsert_user(data: dict):
    db_execute(
        """
        INSERT INTO users (
            user_id, username, name, age, gender, city, latitude, longitude,
            looking_for, min_age, max_age, bio, photo_id, is_active
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s, TRUE)
        )
        ON CONFLICT (user_id) DO UPDATE SET
            username=EXCLUDED.username,
            name=EXCLUDED.name,
            age=EXCLUDED.age,
            gender=EXCLUDED.gender,
            city=EXCLUDED.city,
            latitude=EXCLUDED.latitude,
            longitude=EXCLUDED.longitude,
            looking_for=EXCLUDED.looking_for,
            min_age=EXCLUDED.min_age,
            max_age=EXCLUDED.max_age,
            bio=EXCLUDED.bio,
            photo_id=EXCLUDED.photo_id,
            is_active=EXCLUDED.is_active
        """,
        (
            data.get("user_id"), data.get("username"), data.get("name"), data.get("age"),
            data.get("gender"), data.get("city"), data.get("latitude"), data.get("longitude"),
            data.get("looking_for"), data.get("min_age"), data.get("max_age"), data.get("bio"),
            data.get("photo_id"), data.get("is_active"),
        ),
    )


def set_active(user_id: int, active: bool):
    db_execute("UPDATE users SET is_active=%s WHERE user_id=%s", (active, user_id))


def save_like(from_user: int, to_user: int) -> Tuple[bool, Optional[Tuple[int, int]]]:
    """Save like. If mutual, create match and return (True, (u1,u2)) with ordered pair.
    """
    # insert like (ignore conflict)
    db_execute(
        "INSERT INTO likes(from_user,to_user) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (from_user, to_user),
    )
    # check reciprocal
    row = db_execute(
        "SELECT 1 FROM likes WHERE from_user=%s AND to_user=%s",
        (to_user, from_user),
        fetch="one",
    )
    if row:
        u1, u2 = sorted([from_user, to_user])
        db_execute(
            "INSERT INTO matches(user1,user2) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (u1, u2),
        )
        return True, (u1, u2)
    return False, None


def get_matches_for(user_id: int):
    rows = db_execute(
        """
        SELECT CASE WHEN user1=%s THEN user2 ELSE user1 END AS mate_id
        FROM matches
        WHERE user1=%s OR user2=%s
        ORDER BY created_at DESC
        """,
        (user_id, user_id, user_id),
        fetch="all",
    )
    mates = []
    for r in rows or []:
        mate = get_user(r["mate_id"])  # type: ignore[index]
        if mate:
            mates.append(mate)
    return mates


def mark_viewed(viewer: int, viewed: int):
    db_execute(
        "INSERT INTO viewed_profiles(viewer_user, viewed_user) VALUES(%s,%s) ON CONFLICT DO NOTHING",
        (viewer, viewed),
    )


def find_candidate_for(user_id: int):
    user = get_user(user_id)
    if not user:
        return None

    gender_filter = ""
    params = [user_id]

    # Normalize looking_for values
    lf = (user.get("looking_for") or "").strip().lower()
    if lf in ("мужчина", "мужчин", "male", "парень", "м"):
        gender_filter = "AND u.gender ILIKE 'муж%' OR u.gender ILIKE 'male'"
    elif lf in ("женщина", "женщин", "female", "девушка", "ж"):
        gender_filter = "AND u.gender ILIKE 'жен%' OR u.gender ILIKE 'female'"
    else:
        gender_filter = ""  # any

    sql = f"""
        SELECT u.*
        FROM users u
        WHERE u.user_id<>%s
          AND u.is_active = TRUE
          {('' if not gender_filter else 'AND (' + gender_filter + ')')}
          AND u.age BETWEEN %s AND %s
          AND NOT EXISTS (SELECT 1 FROM likes l WHERE l.from_user=%s AND l.to_user=u.user_id)
          AND NOT EXISTS (SELECT 1 FROM viewed_profiles v WHERE v.viewer_user=%s AND v.viewed_user=u.user_id)
        ORDER BY RANDOM()
        LIMIT 1
    """
    params.extend([user["min_age"], user["max_age"], user_id, user_id])
    row = db_execute(sql, tuple(params), fetch="one")
    return row


# =========================
# UI helpers
# =========================

def pretty_profile(p: dict) -> str:
    fields = [
        f"Имя: {p.get('name')}",
        f"Возраст: {p.get('age')}",
        f"Город: {p.get('city')}",
        f"Пол: {p.get('gender')}",
    ]
    if p.get("bio"):
        fields.append(f"О себе: {p['bio']}")
    return "\n".join(fields)


def like_kb(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❤️ Лайк", callback_data=f"like:{target_id}"),
                InlineKeyboardButton("👎 Пропустить", callback_data=f"skip:{target_id}"),
            ],
            [InlineKeyboardButton("⏹️ Стоп", callback_data="stop")],
        ]
    )


# =========================
# Conversation: Registration
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    assert user

    if not user_exists(user.id):
        await update.message.reply_text(
            "Привет! Я бот знакомств. Давай заполним анкету. Как тебя зовут?",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return NAME

    await update.message.reply_text(
        "С возвращением! Используй команды:\n" \
        "/browse — смотреть анкеты\n" \
        "/profile — моя анкета\n" \
        "/matches — мои мэтчи\n" \
        "/edit — редактировать анкету\n" \
        "/pause — скрыть / показать анкету",
    )
    return ConversationHandler.END


async def name_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Введи имя ещё раз:")
        return NAME
    context.user_data["name"] = name
    await update.message.reply_text("Сколько тебе лет?")
    return AGE


async def age_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit() or not (18 <= int(text) <= 100):
        await update.message.reply_text("Введи возраст от 18 до 100:")
        return AGE
    context.user_data["age"] = int(text)

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Мужчина", callback_data="gender:мужчина"),
            InlineKeyboardButton("Женщина", callback_data="gender:женщина"),
        ], [
            InlineKeyboardButton("Другое", callback_data="gender:другое"),
        ]]
    )
    await update.message.reply_text("Укажи пол:", reply_markup=kb)
    return GENDER


async def gender_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, value = q.data.split(":", 1)
    context.user_data["gender"] = value
    await q.edit_message_text("Из какого ты города? Напиши текстом (например, Киев).")
    return LOCATION


async def location_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = (update.message.text or "").strip()
    if len(city) < 2:
        await update.message.reply_text("Введи корректный город:")
        return LOCATION
    try:
        loc = geolocator.geocode(city, language="ru")
        if not loc:
            raise ValueError("not found")
        context.user_data["city"] = city
        context.user_data["latitude"] = loc.latitude
        context.user_data["longitude"] = loc.longitude
    except Exception:
        context.user_data["city"] = city
        context.user_data["latitude"] = None
        context.user_data["longitude"] = None

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Мужчин", callback_data="lf:мужчина"),
            InlineKeyboardButton("Женщин", callback_data="lf:женщина"),
            InlineKeyboardButton("Любой", callback_data="lf:any"),
        ]]
    )
    await update.message.reply_text("Кого ты ищешь?", reply_markup=kb)
    return LOOKING_FOR


async def looking_for_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, val = q.data.split(":", 1)
    context.user_data["looking_for"] = val
    await q.edit_message_text(
        "Укажи возрастной диапазон партнёра в формате 18-35:"
    )
    return AGE_RANGE


async def age_range_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = re.match(r"^(\d{2})\s*[-–]\s*(\d{2})$", text)
    if not m:
        await update.message.reply_text("Пример: 20-35")
        return AGE_RANGE
    a, b = int(m.group(1)), int(m.group(2))
    if a < 18 or b < a or b > 100:
        await update.message.reply_text("Диапазон должен быть от 18 до 100 и min<=max.")
        return AGE_RANGE
    context.user_data["min_age"] = a
    context.user_data["max_age"] = b
    await update.message.reply_text("Коротко расскажи о себе:")
    return BIO


async def bio_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bio"] = (update.message.text or "").strip()[:500]
    await update.message.reply_text("Пришли фото или напиши /skip, чтобы пропустить.")
    return PHOTO


async def photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришли именно фото или /skip.")
        return PHOTO
    file_id = update.message.photo[-1].file_id  # biggest size
    context.user_data["photo_id"] = file_id
    return await save_profile(update, context)


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["photo_id"] = None
    return await save_profile(update, context)


async def save_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    assert tg_user

    data = {
        "user_id": tg_user.id,
        "username": tg_user.username,
        "name": context.user_data.get("name"),
        "age": context.user_data.get("age"),
        "gender": context.user_data.get("gender"),
        "city": context.user_data.get("city"),
        "latitude": context.user_data.get("latitude"),
        "longitude": context.user_data.get("longitude"),
        "looking_for": context.user_data.get("looking_for"),
        "min_age": context.user_data.get("min_age"),
        "max_age": context.user_data.get("max_age"),
        "bio": context.user_data.get("bio"),
        "photo_id": context.user_data.get("photo_id"),
        "is_active": True,
    }
    upsert_user(data)

    await update.message.reply_text(
        "Готово! Анкета сохранена. Используй /browse чтобы смотреть анкеты или /profile чтобы посмотреть свою."
    )
    return ConversationHandler.END


# =========================
# Browse
# =========================
async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    assert tg_user
    if not user_exists(tg_user.id):
        await update.message.reply_text("Сначала заполни анкету: /start")
        return
    await send_candidate(update, context, tg_user.id)


async def send_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    cand = find_candidate_for(user_id)
    if not cand:
        await update.effective_chat.send_message("Пока подходящих анкет нет. Попробуй позже ✌️")
        return

    text = pretty_profile(cand)
    kb = like_kb(cand["user_id"])  # type: ignore[index]

    if cand.get("photo_id"):
        try:
            await update.effective_chat.send_photo(cand["photo_id"], caption=text, reply_markup=kb)  # type: ignore[index]
        except Exception:
            await update.effective_chat.send_message(text, reply_markup=kb)
    else:
        await update.effective_chat.send_message(text, reply_markup=kb)


async def like_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data
    if data == "stop":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Окей, остановил просмотр.")
        return

    action, target_s = data.split(":", 1)
    target = int(target_s)
    user_id = update.effective_user.id

    if action == "like":
        matched, pair = save_like(user_id, target)
        mark_viewed(user_id, target)
        if matched:
            await q.message.reply_text("Есть взаимный лайк! 🎉 Вы можете начать общение.")
            # Try to notify the other side as well
            try:
                await context.bot.send_message(target, f"У тебя взаимный лайк с @{update.effective_user.username or user_id}!")
            except Exception:
                pass
    elif action == "skip":
        mark_viewed(user_id, target)

    # Next candidate
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await send_candidate(update, context, user_id)


# =========================
# Profile & Matches
# =========================
async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    prof = get_user(tg_user.id)
    if not prof:
        await update.message.reply_text("Профиль не найден. Набери /start и заполни анкету.")
        return
    txt = pretty_profile(prof)
    if prof.get("photo_id"):
        try:
            await update.message.reply_photo(prof["photo_id"], caption=txt)  # type: ignore[index]
            return
        except Exception:
            pass
    await update.message.reply_text(txt)


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    mates = get_matches_for(tg_user.id)
    if not mates:
        await update.message.reply_text("Пока мэтчей нет.")
        return
    lines = []
    for m in mates:
        line = f"{m.get('name')} ({m.get('age')}) — @{m.get('username') or 'без юзернейма'}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


# =========================
# Edit & Pause
# =========================
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prof = get_user(update.effective_user.id)
    if not prof:
        await update.message.reply_text("Сначала создай анкету: /start")
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Имя", callback_data="edit:name"),
            InlineKeyboardButton("Возраст", callback_data="edit:age"),
        ], [
            InlineKeyboardButton("О себе", callback_data="edit:bio"),
            InlineKeyboardButton("Фото", callback_data="edit:photo"),
        ]]
    )
    await update.message.reply_text("Что редактируем?", reply_markup=kb)


async def edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, field = q.data.split(":", 1)
    context.user_data["edit_field"] = field

    prompts = {
        "name": "Введи новое имя:",
        "age": "Введи новый возраст (18-100):",
        "bio": "Пришли новый текст о себе:",
        "photo": "Пришли новое фото:",
    }
    await q.edit_message_text(prompts[field])

    return {
        "name": EDIT_NAME,
        "age": EDIT_AGE,
        "bio": EDIT_BIO,
        "photo": EDIT_PHOTO,
    }[field]


async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Слишком коротко. Ещё раз:")
        return EDIT_NAME
    upsert_user({"user_id": update.effective_user.id, "name": name})
    await update.message.reply_text("Имя обновлено.")
    return ConversationHandler.END


async def edit_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit() or not (18 <= int(text) <= 100):
        await update.message.reply_text("Возраст 18-100. Ещё раз:")
        return EDIT_AGE
    upsert_user({"user_id": update.effective_user.id, "age": int(text)})
    await update.message.reply_text("Возраст обновлён.")
    return ConversationHandler.END


async def edit_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bio = (update.message.text or "").strip()[:500]
    upsert_user({"user_id": update.effective_user.id, "bio": bio})
    await update.message.reply_text("Описание обновлено.")
    return ConversationHandler.END


async def edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Пришли фото:")
        return EDIT_PHOTO
    file_id = update.message.photo[-1].file_id
    upsert_user({"user_id": update.effective_user.id, "photo_id": file_id})
    await update.message.reply_text("Фото обновлено.")
    return ConversationHandler.END


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prof = get_user(update.effective_user.id)
    if not prof:
        await update.message.reply_text("Сначала создай анкету: /start")
        return
    new_state = not bool(prof.get("is_active"))
    set_active(update.effective_user.id, new_state)
    await update.message.reply_text("Анкета активна." if new_state else "Анкета скрыта.")


# =========================
# App
# =========================

def build_app() -> Application:
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_step)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_step)],
            GENDER: [CallbackQueryHandler(gender_cb, pattern=r"^gender:")],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, location_step)],
            LOOKING_FOR: [CallbackQueryHandler(looking_for_cb, pattern=r"^lf:")],
            AGE_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_range_step)],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bio_step)],
            PHOTO: [MessageHandler(filters.PHOTO, photo_step), CommandHandler("skip", skip_photo)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
    )

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_cmd), CallbackQueryHandler(edit_cb, pattern=r"^edit:")],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name)],
            EDIT_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_age)],
            EDIT_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_bio)],
            EDIT_PHOTO: [MessageHandler(filters.PHOTO, edit_photo)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        allow_reentry=True,
        map_to_parent={}
    )

    app.add_handler(reg_conv)
    app.add_handler(edit_conv)

    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))

    app.add_handler(CallbackQueryHandler(like_skip_cb, pattern=r"^(like|skip|stop):?"))

    return app


async def main_async():
    app = build_app()
    logger.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.idle()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
