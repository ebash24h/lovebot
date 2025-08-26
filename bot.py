import asyncio
import logging
from datetime import datetime, timedelta
import sqlite3
import os
from typing import Dict, List, Optional, Tuple
import json
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler

# Конфигурация
BOT_TOKEN = "8371254843:AAFmnc9dkc0c_aGuLUlTp-byrnj-5lsI8IU"
DATABASE_PATH = "dating_bot.db"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Геокодер для работы с городами
geolocator = Nominatim(user_agent="dating_bot")

# Состояния для ConversationHandler
NAME, AGE, GENDER, LOCATION, LOOKING_FOR, AGE_RANGE, BIO, PHOTO = range(8)
BROWSING = 100
EDIT_NAME, EDIT_AGE, EDIT_BIO, EDIT_PHOTO = range(200, 204)

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        name TEXT NOT NULL,
        age INTEGER NOT NULL,
        gender TEXT NOT NULL,
        city TEXT NOT NULL,
        latitude REAL,
        longitude REAL,
        looking_for TEXT NOT NULL,
        min_age INTEGER NOT NULL,
        max_age INTEGER NOT NULL,
        bio TEXT,
        photo_id TEXT,
        is_active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица лайков
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER NOT NULL,
        to_user INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (from_user) REFERENCES users (user_id),
        FOREIGN KEY (to_user) REFERENCES users (user_id),
        UNIQUE(from_user, to_user)
    )
    ''')
    
    # Таблица матчей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user1) REFERENCES users (user_id),
        FOREIGN KEY (user2) REFERENCES users (user_id),
        UNIQUE(user1, user2)
    )
    ''')
    
    # Таблица просмотренных профилей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS viewed_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        viewer_user INTEGER NOT NULL,
        viewed_user INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (viewer_user) REFERENCES users (user_id),
        FOREIGN KEY (viewed_user) REFERENCES users (user_id),
        UNIQUE(viewer_user, viewed_user)
    )
    ''')
    
    # Таблица изменений возраста
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS age_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        old_age INTEGER NOT NULL,
        new_age INTEGER NOT NULL,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

# Функции для работы с базой данных
class DatabaseManager:
    @staticmethod
    def get_connection():
        return sqlite3.connect(DATABASE_PATH)
    
    @staticmethod
    def user_exists(user_id: int) -> bool:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone() is not None
        conn.close()
        return result
    
    @staticmethod
    def create_user(user_data: dict):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO users 
        (user_id, username, name, age, gender, city, latitude, longitude, 
         looking_for, min_age, max_age, bio, photo_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_data['user_id'], user_data['username'], user_data['name'],
            user_data['age'], user_data['gender'], user_data['city'],
            user_data['latitude'], user_data['longitude'], user_data['looking_for'],
            user_data['min_age'], user_data['max_age'], user_data['bio'],
            user_data['photo_id']
        ))
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_user(user_id: int) -> Optional[dict]:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row:
            columns = [description[0] for description in cursor.description]
            conn.close()
            return dict(zip(columns, row))
        conn.close()
        return None
    
    @staticmethod
    def get_potential_matches(user_id: int) -> List[dict]:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        user = DatabaseManager.get_user(user_id)
        if not user:
            return []
        
        cursor.execute('''
        SELECT u.* FROM users u
        WHERE u.user_id != ? 
        AND u.is_active = 1
        AND u.gender = ?
        AND u.age BETWEEN ? AND ?
        AND u.user_id NOT IN (
            SELECT to_user FROM likes WHERE from_user = ?
        )
        AND u.user_id NOT IN (
            SELECT viewed_user FROM viewed_profiles WHERE viewer_user = ?
        )
        ORDER BY RANDOM()
        LIMIT 1
        ''', (
            user_id, user['looking_for'], user['min_age'], user['max_age'],
            user_id, user_id
        ))
        
        matches = []
        for row in cursor.fetchall():
            columns = [description[0] for description in cursor.description]
            user_dict = dict(zip(columns, row))
            
            if user['latitude'] and user['longitude'] and user_dict['latitude'] and user_dict['longitude']:
                distance = geodesic(
                    (user['latitude'], user['longitude']),
                    (user_dict['latitude'], user_dict['longitude'])
                ).kilometers
                
                if distance <= 100:
                    matches.append(user_dict)
        
        conn.close()
        return matches
    
    @staticmethod
    def add_like(from_user: int, to_user: int) -> bool:
        """Добавляет лайк и возвращает True, если образовался матч"""
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('INSERT OR IGNORE INTO likes (from_user, to_user) VALUES (?, ?)', (from_user, to_user))
        
        cursor.execute('SELECT 1 FROM likes WHERE from_user = ? AND to_user = ?', (to_user, from_user))
        is_match = cursor.fetchone() is not None
        
        if is_match:
            cursor.execute('INSERT OR IGNORE INTO matches (user1, user2) VALUES (?, ?)', 
                          (min(from_user, to_user), max(from_user, to_user)))
        
        conn.commit()
        conn.close()
        return is_match
    
    @staticmethod
    def mark_as_viewed(viewer_user: int, viewed_user: int):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO viewed_profiles (viewer_user, viewed_user) VALUES (?, ?)', 
                      (viewer_user, viewed_user))
        conn.commit()
        conn.close()
    
    @staticmethod
    def get_matches(user_id: int) -> List[dict]:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
        SELECT u.* FROM matches m
        JOIN users u ON (u.user_id = m.user1 OR u.user_id = m.user2)
        WHERE (m.user1 = ? OR m.user2 = ?) AND u.user_id != ?
        ORDER BY m.created_at DESC
        ''', (user_id, user_id, user_id))
        
        matches = []
        for row in cursor.fetchall():
            columns = [description[0] for description in cursor.description]
            matches.append(dict(zip(columns, row)))
        
        conn.close()
        return matches
    
    @staticmethod
    def can_change_age(user_id: int) -> Tuple[bool, str]:
        """Проверяет, можно ли изменить возраст"""
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT changed_at FROM age_changes 
        WHERE user_id = ? 
        ORDER BY changed_at DESC 
        LIMIT 1
        ''', (user_id,))
        
        last_change = cursor.fetchone()
        if last_change:
            from datetime import datetime, timedelta
            last_change_time = datetime.fromisoformat(last_change[0])
            if datetime.now() - last_change_time < timedelta(hours=24):
                conn.close()
                return False, "Возраст можно менять не чаще раз в 24 часа"
        
        cursor.execute('''
        SELECT COUNT(*) FROM age_changes 
        WHERE user_id = ? 
        AND changed_at > datetime('now', '-30 days')
        ''', (user_id,))
        
        monthly_changes = cursor.fetchone()[0]
        conn.close()
        
        if monthly_changes >= 3:
            return False, "Возраст можно менять не более 3 раз в месяц"
        
        return True, ""
    
    @staticmethod
    def update_user_field(user_id: int, field: str, value) -> bool:
        """Обновляет поле пользователя"""
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        
        if field == 'age':
            cursor.execute('SELECT age FROM users WHERE user_id = ?', (user_id,))
            old_age = cursor.fetchone()[0]
            
            cursor.execute('''
            INSERT INTO age_changes (user_id, old_age, new_age) 
            VALUES (?, ?, ?)
            ''', (user_id, old_age, value))
        
        cursor.execute(f'UPDATE users SET {field} = ? WHERE user_id = ?', (value, user_id))
        conn.commit()
        conn.close()
        return True

# Вспомогательные функции
def get_city_coordinates(city_name: str) -> Tuple[Optional[float], Optional[float]]:
    """Получает координаты города"""
    try:
        location = geolocator.geocode(city_name + ", Russia")
        if location:
            return location.latitude, location.longitude
    except Exception as e:
        logger.error(f"Ошибка геокодинга: {e}")
    return None, None

def create_main_menu() -> InlineKeyboardMarkup:
    """Создает главное меню"""
    keyboard = [
        [InlineKeyboardButton("👀 Смотреть анкеты", callback_data="browse")],
        [InlineKeyboardButton("❤️ Мои матчи", callback_data="matches")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("✏️ Редактировать анкету", callback_data="edit_profile")],
        [InlineKeyboardButton("⚙️ Настройки поиска", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_edit_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📝 Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton("🎂 Изменить возраст", callback_data="edit_age")],
        [InlineKeyboardButton("👤 Изменить пол", callback_data="edit_gender")],
        [InlineKeyboardButton("📖 Изменить описание", callback_data="edit_bio")],
        [InlineKeyboardButton("📸 Изменить фото", callback_data="edit_photo")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_profile_actions() -> InlineKeyboardMarkup:
    """Создает кнопки для действий с анкетой"""
    keyboard = [
        [
            InlineKeyboardButton("👎 Пропустить", callback_data="skip"),
            InlineKeyboardButton("❤️ Лайк", callback_data="like")
        ],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_profile(user_data: dict) -> str:
    """Форматирует профиль пользователя"""
    return f"""👤 {user_data['name']}, {user_data['age']} лет
📍 {user_data['city']}

📝 О себе:
{user_data['bio'] or 'Информация не указана'}"""

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    
    if DatabaseManager.user_exists(user_id):
        await update.message.reply_text(
            "👋 С возвращением! Выберите действие:",
            reply_markup=create_main_menu()
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать в бот знакомств!\n\n"
            "Давайте создадим ваш профиль. Как вас зовут?",
            reply_markup=ReplyKeyboardRemove()
        )
        return NAME

# Регистрация - имя
async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Сколько вам лет? (укажите число)")
    return AGE

# Регистрация - возраст
async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text)
        if age < 18 or age > 100:
            await update.message.reply_text("Возраст должен быть от 18 до 100 лет. Попробуйте еще раз:")
            return AGE
        
        context.user_data['age'] = age
        
        keyboard = [
            [InlineKeyboardButton("👨 Мужской", callback_data="gender_male")],
            [InlineKeyboardButton("👩 Женский", callback_data="gender_female")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text("Выберите ваш пол:", reply_markup=reply_markup)
        return GENDER
        
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите возраст числом:")
        return AGE

# Регистрация - пол
async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    gender = query.data.split("_")[1]
    context.user_data['gender'] = gender
    
    await query.edit_message_text("📍 Отправьте вашу геолокацию или напишите название города:")
    return LOCATION

# Регистрация - локация
async def get_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.location:
        lat, lon = update.message.location.latitude, update.message.location.longitude
        
        try:
            location = geolocator.reverse(f"{lat}, {lon}")
            city = location.address.split(",")[0] if location else "Неизвестный город"
        except:
            city = "Неизвестный город"
            
        context.user_data.update({'city': city, 'latitude': lat, 'longitude': lon})
    else:
        city = update.message.text
        lat, lon = get_city_coordinates(city)
        
        if not lat or not lon:
            await update.message.reply_text(
                "Не удалось найти этот город. Попробуйте ввести название по-другому "
                "или отправьте геолокацию."
            )
            return LOCATION
            
        context.user_data.update({'city': city, 'latitude': lat, 'longitude': lon})
    
    keyboard = [
        [InlineKeyboardButton("👨 Парней", callback_data="looking_male")],
        [InlineKeyboardButton("👩 Девушек", callback_data="looking_female")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Кого вы ищете?", reply_markup=reply_markup)
    return LOOKING_FOR

# Регистрация - кого ищет
async def get_looking_for(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    looking_for = query.data.split("_")[1]
    context.user_data['looking_for'] = looking_for
    
    await query.edit_message_text("В каком возрасте ищете? Напишите диапазон через дефис (например: 20-30):")
    return AGE_RANGE

# Регистрация - возрастной диапазон
async def get_age_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age_range = update.message.text.split("-")
        min_age = int(age_range[0].strip())
        max_age = int(age_range[1].strip())
        
        if min_age < 18 or max_age > 100 or min_age > max_age:
            await update.message.reply_text("Неверный диапазон возраста. Пример: 20-30 (от 18 до 100 лет):")
            return AGE_RANGE
            
        context.user_data.update({'min_age': min_age, 'max_age': max_age})
        await update.message.reply_text("Расскажите о себе (опишите ваши интересы, хобби):")
        return BIO
        
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Напишите возрастной диапазон через дефис (например: 20-30):")
        return AGE_RANGE

# Регистрация - описание
async def get_bio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['bio'] = update.message.text
    await update.message.reply_text("Отправьте вашу фотографию:")
    return PHOTO

# Регистрация - фото
async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фотографию:")
        return PHOTO
    
    photo_id = update.message.photo[-1].file_id
    context.user_data['photo_id'] = photo_id
    
    user_data = {
        'user_id': update.effective_user.id,
        'username': update.effective_user.username,
        **context.user_data
    }
    
    DatabaseManager.create_user(user_data)
    
    await update.message.reply_text(
        "✅ Ваш профиль создан! Добро пожаловать в бот знакомств.",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

# Просмотр анкет
async def browse_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
        message = query.message
    else:
        user_id = update.effective_user.id
        message = update.message
    
    matches = DatabaseManager.get_potential_matches(user_id)
    
    if not matches:
        text = "😔 Анкеты закончились! Попробуйте изменить параметры поиска."
        if query:
            await query.edit_message_text(text, reply_markup=create_main_menu())
        else:
            await message.reply_text(text, reply_markup=create_main_menu())
        return
    
    match = matches[0]
    context.user_data['current_profile'] = match['user_id']
    
    if query:
        await query.message.delete()
    
    await context.bot.send_photo(
        chat_id=message.chat_id,
        photo=match['photo_id'],
        caption=format_profile(match),
        reply_markup=create_profile_actions()
    )

# Лайк
async def like_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    target_user_id = context.user_data.get('current_profile')
    
    if not target_user_id:
        return
    
    DatabaseManager.mark_as_viewed(user_id, target_user_id)
    is_match = DatabaseManager.add_like(user_id, target_user_id)
    
    if is_match:
        target_user = DatabaseManager.get_user(target_user_id)
        current_user = DatabaseManager.get_user(user_id)
        
        keyboard = [
            [InlineKeyboardButton("👀 Продолжить просмотр", callback_data="continue_browse")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_caption(
            f"💕 Взаимная симпатия с {target_user['name']}!\n\n"
            f"Можете связаться: @{target_user['username'] or 'пользователь скрыл username'}",
            reply_markup=reply_markup
        )
        
        try:
            await context.bot.send_message(
                target_user_id,
                f"💕 У вас взаимная симпатия с {current_user['name']}!\n\n"
                f"Можете связаться: @{current_user['username'] or 'пользователь скрыл username'}"
            )
        except:
            pass
    else:
        keyboard = [[InlineKeyboardButton("👀 Продолжить просмотр", callback_data="continue_browse")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_caption("❤️ Лайк отправлен!", reply_markup=reply_markup)

# Продолжить просмотр
async def continue_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    matches = DatabaseManager.get_potential_matches(user_id)
    
    if not matches:
        await query.edit_message_caption(
            "😔 Анкеты закончились! Попробуйте изменить параметры поиска.",
            reply_markup=create_main_menu()
        )
        return
    
    match = matches[0]
    context.user_data['current_profile'] = match['user_id']
    
    await query.message.delete()
    await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=match['photo_id'],
        caption=format_profile(match),
        reply_markup=create_profile_actions()
    )

# Пропустить
async def skip_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    target_user_id = context.user_data.get('current_profile')
    
    if target_user_id:
        DatabaseManager.mark_as_viewed(user_id, target_user_id)
    
    await query.message.delete()
    await browse_profiles(update, context)

# Мои матчи
async def show_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    matches = DatabaseManager.get_matches(user_id)
    
    if not matches:
        await query.edit_message_text(
            "😔 У вас пока нет матчей. Попробуйте поставить больше лайков!",
            reply_markup=create_main_menu()
        )
        return
    
    matches_text = "💕 Ваши матчи:\n\n"
    for i, match in enumerate(matches[:10], 1):
        username = f"@{match['username']}" if match['username'] else "пользователь скрыл username"
        matches_text += f"{i}. {match['name']}, {match['age']} лет - {username}\n"
    
    keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(matches_text, reply_markup=reply_markup)

# Главное меню
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🏠 Главное меню:", reply_markup=create_main_menu())

# Мой профиль
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user = DatabaseManager.get_user(user_id)
    
    if not user:
        return
    
    looking_for_text = "парней" if user['looking_for'] == 'male' else "девушек"
    gender_text = "Мужской" if user['gender'] == 'male' else "Женский"
    
    profile_text = f"""👤 Ваш профиль:

📝 Имя: {user['name']}
🎂 Возраст: {user['age']} лет
👤 Пол: {gender_text}
📍 Город: {user['city']}
💕 Ищете: {looking_for_text} {user['min_age']}-{user['max_age']} лет

📖 О себе:
{user['bio']}"""
    
    keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(profile_text, reply_markup=reply_markup)

# Редактирование профиля
async def edit_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "✏️ Что хотите изменить в своем профиле?",
        reply_markup=create_edit_menu()
    )

# Редактирование имени
async def edit_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("📝 Введите новое имя:")
    return EDIT_NAME

async def edit_name_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    new_name = update.message.text
    
    DatabaseManager.update_user_field(user_id, 'name', new_name)
    
    await update.message.reply_text(
        f"✅ Имя изменено на: {new_name}",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

# Редактирование возраста
async def edit_age_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    can_change, message = DatabaseManager.can_change_age(user_id)
    
    if not can_change:
        await query.edit_message_text(
            f"❌ {message}",
            reply_markup=create_edit_menu()
        )
        return ConversationHandler.END
    
    await query.edit_message_text("🎂 Введите новый возраст (18-100):")
    return EDIT_AGE

async def edit_age_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_id = update.effective_user.id
        new_age = int(update.message.text)
        
        if new_age < 18 or new_age > 100:
            await update.message.reply_text("Возраст должен быть от 18 до 100 лет. Попробуйте еще раз:")
            return EDIT_AGE
        
        DatabaseManager.update_user_field(user_id, 'age', new_age)
        
        await update.message.reply_text(
            f"✅ Возраст изменен на: {new_age} лет",
            reply_markup=create_main_menu()
        )
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите возраст числом:")
        return EDIT_AGE

# Редактирование пола
async def edit_gender_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("👨 Мужской", callback_data="edit_gender_male")],
        [InlineKeyboardButton("👩 Женский", callback_data="edit_gender_female")],
        [InlineKeyboardButton("◀️ Назад", callback_data="edit_profile")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text("👤 Выберите новый пол:", reply_markup=reply_markup)

async def edit_gender_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    new_gender = query.data.split("_")[2]
    
    DatabaseManager.update_user_field(user_id, 'gender', new_gender)
    
    gender_text = "Мужской" if new_gender == 'male' else "Женский"
    await query.edit_message_text(
        f"✅ Пол изменен на: {gender_text}",
        reply_markup=create_main_menu()
    )

# Редактирование описания
async def edit_bio_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("📖 Введите новое описание о себе:")
    return EDIT_BIO

async def edit_bio_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    new_bio = update.message.text
    
    DatabaseManager.update_user_field(user_id, 'bio', new_bio)
    
    await update.message.reply_text(
        "✅ Описание обновлено!",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

# Редактирование фото
async def edit_photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("📸 Отправьте новую фотографию:")
    return EDIT_PHOTO

async def edit_photo_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправьте фотографию:")
        return EDIT_PHOTO
    
    user_id = update.effective_user.id
    new_photo_id = update.message.photo[-1].file_id
    
    DatabaseManager.update_user_field(user_id, 'photo_id', new_photo_id)
    
    await update.message.reply_text(
        "✅ Фотография обновлена!",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

# Настройки поиска
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user = DatabaseManager.get_user(user_id)
    
    if not user:
        return
    
    looking_for_text = "парней" if user['looking_for'] == 'male' else "девушек"
    
    settings_text = f"""⚙️ Настройки поиска:

🔍 Ищете: {looking_for_text}
🎂 Возраст: {user['min_age']}-{user['max_age']} лет
📍 Город: {user['city']}

Для изменения настроек создайте новый профиль командой /start"""
    
    keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(settings_text, reply_markup=reply_markup)

# Отмена
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Отменено.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main() -> None:
    init_database()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # ConversationHandler для регистрации
    registration_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age)],
            GENDER: [CallbackQueryHandler(get_gender, pattern=r"^gender_")],
            LOCATION: [MessageHandler(filters.TEXT | filters.LOCATION, get_location)],
            LOOKING_FOR: [CallbackQueryHandler(get_looking_for, pattern=r"^looking_")],
            AGE_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age_range)],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bio)],
            PHOTO: [MessageHandler(filters.PHOTO, get_photo)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # ConversationHandler для редактирования профиля
    edit_profile_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_name_start, pattern="edit_name"),
            CallbackQueryHandler(edit_age_start, pattern="edit_age"),
            CallbackQueryHandler(edit_bio_start, pattern="edit_bio"),
            CallbackQueryHandler(edit_photo_start, pattern="edit_photo"),
        ],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name_process)],
            EDIT_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_age_process)],
            EDIT_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_bio_process)],
            EDIT_PHOTO: [MessageHandler(filters.PHOTO, edit_photo_process)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Добавляем обработчики
    application.add_handler(registration_handler)
    application.add_handler(edit_profile_handler)
    
    # Основные кнопки
    application.add_handler(CallbackQueryHandler(browse_profiles, pattern="browse"))
    application.add_handler(CallbackQueryHandler(continue_browse, pattern="continue_browse"))
    application.add_handler(CallbackQueryHandler(like_profile, pattern="like"))
    application.add_handler(CallbackQueryHandler(skip_profile, pattern="skip"))
    application.add_handler(CallbackQueryHandler(show_matches, pattern="matches"))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern="main_menu"))
    application.add_handler(CallbackQueryHandler(show_profile, pattern="profile"))
    application.add_handler(CallbackQueryHandler(show_settings, pattern="settings"))
    application.add_handler(CallbackQueryHandler(edit_profile_menu, pattern="edit_profile"))
    application.add_handler(CallbackQueryHandler(edit_gender_start, pattern="edit_gender"))
    application.add_handler(CallbackQueryHandler(edit_gender_process, pattern=r"edit_gender_(male|female)"))
    
    logger.info("Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()