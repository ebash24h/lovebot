import os
import re
import asyncio
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict

from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, filters, ConversationHandler
)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN or not DATABASE_URL:
    raise RuntimeError("TELEGRAM_TOKEN или DATABASE_URL не установлены в .env")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lovebot")

geolocator = Nominatim(user_agent="lovebot")

# Состояния для регистрации
NAME, AGE, GENDER, LOCATION, LOOKING_FOR, AGE_RANGE, BIO, PHOTO = range(8)

# Состояния для редактирования
EDIT_NAME, EDIT_AGE, EDIT_BIO, EDIT_PHOTO = range(100, 104)

# База данных
class Database:
    @staticmethod
    def execute(sql: str, params: tuple = (), fetch: Optional[str] = None):
        """Выполняет SQL запрос с автоматическим управлением соединением"""
        try:
            with psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    if fetch == "one":
                        return cur.fetchone()
                    elif fetch == "all":
                        return cur.fetchall()
                    return None
        except psycopg2.Error as e:
            logger.error(f"Database error: {e}")
            return None

    @staticmethod
    def init_tables():
        """Создает таблицы если их нет"""
        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                name TEXT NOT NULL,
                age INT NOT NULL,
                gender TEXT NOT NULL,
                city TEXT NOT NULL,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                looking_for TEXT NOT NULL,
                min_age INT NOT NULL,
                max_age INT NOT NULL,
                bio TEXT,
                photo_id TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS likes (
                from_user BIGINT NOT NULL,
                to_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            )""",
            """CREATE TABLE IF NOT EXISTS matches (
                user1 BIGINT NOT NULL,
                user2 BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user1, user2)
            )""",
            """CREATE TABLE IF NOT EXISTS viewed_profiles (
                viewer_user BIGINT NOT NULL,
                viewed_user BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (viewer_user, viewed_user)
            )"""
        ]
        
        for table_sql in tables:
            Database.execute(table_sql)

# Функции для работы с пользователями
class UserManager:
    @staticmethod
    def exists(user_id: int) -> bool:
        """Проверяет существование пользователя"""
        result = Database.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,), fetch="one")
        return bool(result)

    @staticmethod
    def create(data: Dict) -> bool:
        """Создает нового пользователя"""
        sql = """INSERT INTO users 
                (user_id, username, name, age, gender, city, latitude, longitude,
                 looking_for, min_age, max_age, bio, photo_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
        
        params = (
            data['user_id'], data.get('username'), data['name'], data['age'],
            data['gender'], data['city'], data.get('latitude'), data.get('longitude'),
            data['looking_for'], data['min_age'], data['max_age'], 
            data.get('bio'), data.get('photo_id')
        )
        
        result = Database.execute(sql, params)
        return result is not None

    @staticmethod
    def get(user_id: int):
        """Получает пользователя по ID"""
        return Database.execute("SELECT * FROM users WHERE user_id = %s", (user_id,), fetch="one")

    @staticmethod
    def update_field(user_id: int, field: str, value):
        """Обновляет поле пользователя"""
        sql = f"UPDATE users SET {field} = %s WHERE user_id = %s"
        Database.execute(sql, (value, user_id))

    @staticmethod
    def find_candidates(user_id: int):
        """Находит подходящих кандидатов"""
        user = UserManager.get(user_id)
        if not user:
            return []

        # Определяем фильтр по полу
        gender_condition = ""
        if user['looking_for'] in ['мужчина', 'male']:
            gender_condition = "AND (gender ILIKE 'мужч%' OR gender = 'male')"
        elif user['looking_for'] in ['женщина', 'female']:
            gender_condition = "AND (gender ILIKE 'жен%' OR gender = 'female')"

        sql = f"""SELECT * FROM users 
                 WHERE user_id != %s 
                 AND is_active = TRUE 
                 AND age BETWEEN %s AND %s 
                 {gender_condition}
                 AND user_id NOT IN (
                     SELECT to_user FROM likes WHERE from_user = %s
                 )
                 AND user_id NOT IN (
                     SELECT viewed_user FROM viewed_profiles WHERE viewer_user = %s
                 )
                 ORDER BY RANDOM() LIMIT 1"""

        return Database.execute(sql, (user_id, user['min_age'], user['max_age'], user_id, user_id), fetch="all")

# Функции для лайков и матчей
class MatchManager:
    @staticmethod
    def add_like(from_user: int, to_user: int) -> bool:
        """Добавляет лайк и проверяет матч"""
        # Добавляем лайк
        Database.execute(
            "INSERT INTO likes (from_user, to_user) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (from_user, to_user)
        )
        
        # Проверяем обратный лайк
        mutual = Database.execute(
            "SELECT 1 FROM likes WHERE from_user = %s AND to_user = %s",
            (to_user, from_user), fetch="one"
        )
        
        if mutual:
            # Создаем матч
            user1, user2 = sorted([from_user, to_user])
            Database.execute(
                "INSERT INTO matches (user1, user2) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user1, user2)
            )
            return True
        return False

    @staticmethod
    def mark_viewed(viewer: int, viewed: int):
        """Отмечает профиль как просмотренный"""
        Database.execute(
            "INSERT INTO viewed_profiles (viewer_user, viewed_user) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (viewer, viewed)
        )

    @staticmethod
    def get_matches(user_id: int):
        """Получает матчи пользователя"""
        sql = """SELECT u.* FROM matches m
                JOIN users u ON (u.user_id = CASE WHEN m.user1 = %s THEN m.user2 ELSE m.user1 END)
                WHERE m.user1 = %s OR m.user2 = %s
                ORDER BY m.created_at DESC"""
        return Database.execute(sql, (user_id, user_id, user_id), fetch="all")

# Вспомогательные функции
def get_coordinates(city: str):
    """Получает координаты города"""
    try:
        location = geolocator.geocode(city + ", Russia")
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return (None, None)

def format_profile(user) -> str:
    """Форматирует профиль пользователя"""
    return f"""👤 {user['name']}, {user['age']} лет
📍 {user['city']}

📝 О себе: {user['bio'] or 'Не указано'}"""

def create_main_menu():
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("👀 Смотреть анкеты", callback_data="browse")],
        [InlineKeyboardButton("❤️ Мои матчи", callback_data="matches")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_menu")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_browse_keyboard(target_id: int):
    """Кнопки для просмотра анкет"""
    keyboard = [
        [
            InlineKeyboardButton("❤️ Лайк", callback_data=f"like_{target_id}"),
            InlineKeyboardButton("👎 Пропустить", callback_data=f"skip_{target_id}")
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if UserManager.exists(user_id):
        await update.message.reply_text(
            "С возвращением! Выберите действие:",
            reply_markup=create_main_menu()
        )
        return ConversationHandler.END
    
    await update.message.reply_text(
        "Привет! Я бот знакомств. Давай создадим твою анкету.\n\nКак тебя зовут?",
        reply_markup=ReplyKeyboardRemove()
    )
    return NAME

# Регистрация - имя
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Попробуй еще раз:")
        return NAME
    
    context.user_data['name'] = name
    await update.message.reply_text("Сколько тебе лет? (от 18 до 100)")
    return AGE

# Регистрация - возраст
async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        age = int(update.message.text)
        if 18 <= age <= 100:
            context.user_data['age'] = age
            
            keyboard = [
                [InlineKeyboardButton("👨 Мужчина", callback_data="gender_male")],
                [InlineKeyboardButton("👩 Женщина", callback_data="gender_female")]
            ]
            
            await update.message.reply_text(
                "Выбери свой пол:", 
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return GENDER
        else:
            await update.message.reply_text("Возраст должен быть от 18 до 100 лет:")
            return AGE
    except ValueError:
        await update.message.reply_text("Введи возраст числом:")
        return AGE

# Регистрация - пол
async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    gender = "мужчина" if query.data == "gender_male" else "женщина"
    context.user_data['gender'] = gender
    
    await query.edit_message_text("Из какого ты города? Напиши название:")
    return LOCATION

# Регистрация - город
async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if len(city) < 2:
        await update.message.reply_text("Введи корректное название города:")
        return LOCATION
    
    lat, lon = get_coordinates(city)
    context.user_data.update({
        'city': city,
        'latitude': lat,
        'longitude': lon
    })
    
    keyboard = [
        [InlineKeyboardButton("👨 Парней", callback_data="looking_male")],
        [InlineKeyboardButton("👩 Девушек", callback_data="looking_female")]
    ]
    
    await update.message.reply_text(
        "Кого ищешь?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LOOKING_FOR

# Регистрация - кого ищет
async def get_looking_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    looking_for = "мужчина" if query.data == "looking_male" else "женщина"
    context.user_data['looking_for'] = looking_for
    
    await query.edit_message_text(
        "В каком возрасте? Напиши диапазон через дефис (например: 20-30):"
    )
    return AGE_RANGE

# Регистрация - возрастной диапазон
async def get_age_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.match(r'(\d{2})-(\d{2})', text)
    
    if not match:
        await update.message.reply_text("Введи в формате: 20-30")
        return AGE_RANGE
    
    min_age, max_age = int(match.group(1)), int(match.group(2))
    
    if min_age < 18 or max_age > 100 or min_age > max_age:
        await update.message.reply_text("Некорректный диапазон. Пример: 20-30")
        return AGE_RANGE
    
    context.user_data.update({
        'min_age': min_age,
        'max_age': max_age
    })
    
    await update.message.reply_text("Расскажи немного о себе:")
    return BIO

# Регистрация - описание
async def get_bio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bio = update.message.text.strip()[:500]
    context.user_data['bio'] = bio
    
    await update.message.reply_text("Отправь свое фото или напиши /skip чтобы пропустить:")
    return PHOTO

# Регистрация - фото
async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
        context.user_data['photo_id'] = photo_id
    else:
        await update.message.reply_text("Это не фото. Отправь фото или /skip:")
        return PHOTO
    
    return await save_user_profile(update, context)

async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['photo_id'] = None
    return await save_user_profile(update, context)

async def save_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет профиль пользователя"""
    user_data = {
        'user_id': update.effective_user.id,
        'username': update.effective_user.username,
        **context.user_data
    }
    
    if UserManager.create(user_data):
        await update.message.reply_text(
            "✅ Профиль создан! Добро пожаловать в бот знакомств.",
            reply_markup=create_main_menu()
        )
    else:
        await update.message.reply_text("Ошибка создания профиля. Попробуй /start еще раз.")
    
    return ConversationHandler.END

# Просмотр анкет
async def browse_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id
    
    if not UserManager.exists(user_id):
        text = "Сначала создай анкету командой /start"
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return
    
    candidates = UserManager.find_candidates(user_id)
    
    if not candidates:
        text = "Анкеты закончились! Попробуй позже."
        keyboard = create_main_menu()
        if query:
            await query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)
        return
    
    candidate = candidates[0]
    text = format_profile(candidate)
    keyboard = create_browse_keyboard(candidate['user_id'])
    
    # Удаляем предыдущее сообщение если это callback
    if query:
        await query.message.delete()
    
    # Отправляем анкету с фото или без
    if candidate.get('photo_id'):
        try:
            await context.bot.send_photo(
                chat_id=user_id,
                photo=candidate['photo_id'],
                caption=text,
                reply_markup=keyboard
            )
        except:
            await context.bot.send_message(user_id, text, reply_markup=keyboard)
    else:
        await context.bot.send_message(user_id, text, reply_markup=keyboard)

# Обработка лайков
async def handle_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    target_id = int(query.data.split('_')[1])
    
    # Отмечаем как просмотренный
    MatchManager.mark_viewed(user_id, target_id)
    
    # Ставим лайк
    is_match = MatchManager.add_like(user_id, target_id)
    
    if is_match:
        target_user = UserManager.get(target_id)
        current_user = UserManager.get(user_id)
        
        # Уведомляем о матче
        await query.edit_message_caption(
            f"💕 Взаимная симпатия с {target_user['name']}!\n\n"
            f"Контакт: @{target_user['username'] or 'скрыт'}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👀 Смотреть дальше", callback_data="browse"),
                InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
            ]])
        )
        
        # Уведомляем второго пользователя
        try:
            await context.bot.send_message(
                target_id,
                f"💕 Взаимная симпатия с {current_user['name']}!\n\n"
                f"Контакт: @{current_user['username'] or 'скрыт'}"
            )
        except:
            pass
    else:
        await query.edit_message_caption(
            "❤️ Лайк отправлен!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👀 Смотреть дальше", callback_data="browse")
            ]])
        )

# Обработка пропуска
async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    target_id = int(query.data.split('_')[1])
    
    # Отмечаем как просмотренный
    MatchManager.mark_viewed(user_id, target_id)
    
    # Показываем следующую анкету
    await query.message.delete()
    await browse_profiles(update, context)

# Мои матчи
async def show_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    matches = MatchManager.get_matches(query.from_user.id)
    
    if not matches:
        await query.edit_message_text(
            "У тебя пока нет матчей. Ставь больше лайков!",
            reply_markup=create_main_menu()
        )
        return
    
    text = "💕 Твои матчи:\n\n"
    for match in matches[:10]:
        username = f"@{match['username']}" if match['username'] else "контакт скрыт"
        text += f"• {match['name']}, {match['age']} — {username}\n"
    
    await query.edit_message_text(text, reply_markup=create_main_menu())

# Мой профиль
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = UserManager.get(query.from_user.id)
    if not user:
        await query.edit_message_text("Профиль не найден")
        return
    
    looking_text = "парней" if user['looking_for'] == 'мужчина' else "девушек"
    text = f"""👤 Твой профиль:

📝 Имя: {user['name']}
🎂 Возраст: {user['age']} лет
👤 Пол: {user['gender']}
📍 Город: {user['city']}
💕 Ищешь: {looking_text} {user['min_age']}-{user['max_age']} лет

📖 О себе: {user['bio'] or 'Не указано'}"""
    
    await query.edit_message_text(text, reply_markup=create_main_menu())

# Главное меню
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏠 Главное меню:",
        reply_markup=create_main_menu()
    )

# Отмена операций
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

def main():
    """Запуск бота"""
    # Инициализация базы данных
    Database.init_tables()
    
    # Создание приложения
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Обработчик регистрации
    registration_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age)],
            GENDER: [CallbackQueryHandler(get_gender, pattern=r"^gender_")],
            LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_location)],
            LOOKING_FOR: [CallbackQueryHandler(get_looking_for, pattern=r"^looking_")],
            AGE_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age_range)],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bio)],
            PHOTO: [MessageHandler(filters.PHOTO, get_photo), CommandHandler("skip", skip_photo)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    # Добавляем обработчики
    application.add_handler(registration_handler)
    application.add_handler(CallbackQueryHandler(browse_profiles, pattern="browse"))
    application.add_handler(CallbackQueryHandler(handle_like, pattern=r"^like_\d+"))
    application.add_handler(CallbackQueryHandler(handle_skip, pattern=r"^skip_\d+"))
    application.add_handler(CallbackQueryHandler(show_matches, pattern="matches"))
    application.add_handler(CallbackQueryHandler(show_profile, pattern="profile"))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="main_menu"))
    
    # Команды
    application.add_handler(CommandHandler("browse", browse_profiles))
    application.add_handler(CommandHandler("matches", show_matches))
    application.add_handler(CommandHandler("profile", show_profile))
    
    logger.info("Бот запускается...")
    
    # Запуск polling
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()