import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated, Chat
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8917698584:AAGyjwsbdX7oP8MyUDz8qzsjXM8EKZeFZ24"

ADMIN_USERNAMES = {"kitt069", "jar_yo", "rudiment01"}
ADMIN_IDS = set()

DB_PATH = Path(__file__).parent / "inventory.db"

INITIAL_ITEMS = {
    "Овощи": ["лук", "перец болгарский", "капуста пекинская", "томаты", "огурцы"],
    "Лаваш": ["лаваш обычный", "лаваш сырный", "лаваш зеленый (сельдерей)", "лаваш розовый (томатный)", "лаваш черный", "булки для донера", "пита", "булочки для хот-дога"],
    "Мясо+Ингредиенты+Соусы+Специи+Фритюр": [
        "куриное филе", "сосиски", "ветчина", "сыр чеддер", "сыр голландский", "огурцы маринованные",
        "лук криспи", "халапеньо", "острый соус", "паприка", "соль", "приправа для курицы",
        "приправа для шашлыка", "масло для фритюра (бутыль)", "майонез", "сметана", "барбекю соус",
        "бургер соус", "сырный соус", "чесночный соус", "гриль соус", "кетчуп", "горчица",
        "картофель-фри", "картофель по-деревенски", "наггетсы", "стрипсы", "сырные палочки"
    ],
    "Лимонады": [
        "основа персиковая", "основа личи", "основа бабл гам", "основа груша-жасмин", "основа лаванда-лимон",
        "сушенный грейпфрут", "краситель золотой", "краситель розовый", "краситель серый", "маршмеллоу",
        "сушеная лаванда", "персики консервированные (банка)", "пюре манго/абрикос (банка)"
    ],
    "Упаковка": [
        "конверт для шаурмы", "конверт для сэндвича", "коробка для лодочки", "коробка для фритюра (квадратная)",
        "фольга", "пленка", "пакеты", "стаканы для лимонада", "стаканы для кофе", "стаканы для чая",
        "крышки для стаканов лимонада", "крышки для стаканов кофе", "трубочки", "палочки размешивания",
        "сахар порционный", "вилки одноразовые", "подстаканники"
    ],
}

REMINDER_GROUPS = {
    "frequent": ["Овощи", "Лаваш", "Мясо+Ингредиенты+Соусы+Специи+Фритюр"],
    "weekly": ["Лимонады", "Упаковка"],
}

DAY_MAP = {
    "понедельник": "mon", "пн": "mon", "понед": "mon",
    "вторник": "tue", "вт": "tue",
    "среда": "wed", "ср": "wed",
    "четверг": "thu", "чт": "thu",
    "пятница": "fri", "пт": "fri",
    "суббота": "sat", "сб": "sat",
    "воскресенье": "sun", "вс": "sun",
}

DAY_REVERSE = {
    "mon": "понедельник", "tue": "вторник", "wed": "среда",
    "thu": "четверг", "fri": "пятница", "sat": "суббота", "sun": "воскресенье",
}

# ================== БАЗА ДАННЫХ ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
        await db.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, name TEXT NOT NULL, is_active INTEGER DEFAULT 1,
            FOREIGN KEY (category_id) REFERENCES categories(id), UNIQUE(category_id, name))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT, full_name TEXT,
            category TEXT NOT NULL, created_at TEXT NOT NULL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS report_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL, item_name TEXT NOT NULL, quantity TEXT NOT NULL,
            FOREIGN KEY (report_id) REFERENCES reports(id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reminder_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT UNIQUE NOT NULL, days TEXT NOT NULL,
            time TEXT NOT NULL DEFAULT '10:00', enabled INTEGER DEFAULT 1)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, is_admin INTEGER DEFAULT 0,
            last_seen TEXT, notify_enabled INTEGER DEFAULT 1)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS notify_groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at TEXT,
            enabled INTEGER DEFAULT 1)""")
        await db.commit()

        try:
            await db.execute("ALTER TABLE users ADD COLUMN notify_enabled INTEGER DEFAULT 1")
            await db.commit()
        except Exception:
            pass

        for cat_name, items in INITIAL_ITEMS.items():
            await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_name,))
            cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,))
            cat_id = (await cursor.fetchone())[0]
            for item in items:
                await db.execute("INSERT OR IGNORE INTO items (category_id, name) VALUES (?, ?)", (cat_id, item))

        await db.execute("INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)",
                         ("frequent", "tue,fri", "10:00"))
        await db.execute("INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)",
                         ("weekly", "mon", "10:00"))
        await db.commit()


async def get_categories() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT name FROM categories ORDER BY id")
        return [r[0] for r in await cursor.fetchall()]


async def get_items_by_category(category_name: str) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT i.name FROM items i JOIN categories c ON c.id = i.category_id
            WHERE c.name = ? AND i.is_active = 1 ORDER BY i.id""", (category_name,))
        return [r[0] for r in await cursor.fetchall()]


async def add_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        row = await cursor.fetchone()
        if not row:
            await db.execute("INSERT INTO categories (name) VALUES (?)", (category_name,))
            cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
            row = await cursor.fetchone()
        try:
            await db.execute("INSERT INTO items (category_id, name) VALUES (?, ?)", (row[0], item_name.strip()))
            await db.commit()
            return True
        except Exception:
            return False


async def deactivate_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""UPDATE items SET is_active = 0
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)""",
                                  (item_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


async def rename_item(category_name: str, old_name: str, new_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""UPDATE items SET name = ?
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)""",
                                  (new_name, old_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


async def rename_category(old_name: str, new_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (new_name,))
        if await cursor.fetchone():
            return False
        cursor = await db.execute("UPDATE categories SET name = ? WHERE name = ?", (new_name, old_name))
        await db.commit()
        return cursor.rowcount > 0


async def delete_category(category_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        row = await cursor.fetchone()
        if not row:
            return False
        cat_id = row[0]
        await db.execute("DELETE FROM items WHERE category_id = ?", (cat_id,))
        await db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        await db.commit()
        return True


async def save_report(user_id: int, username: Optional[str], full_name: str, category: str, items: Dict[str, str]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        cursor = await db.execute(
            "INSERT INTO reports (user_id, username, full_name, category, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, category, now))
        report_id = cursor.lastrowid
        for item_name, qty in items.items():
            await db.execute("INSERT INTO report_items (report_id, item_name, quantity) VALUES (?, ?, ?)",
                             (report_id, item_name, qty))
        await db.commit()
        return report_id


async def get_last_reports(limit: int = 15) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, username, full_name, category, created_at FROM reports ORDER BY id DESC LIMIT ?",
            (limit,))
        return [dict(r) for r in await cursor.fetchall()]


async def get_reports_by_day_month_and_category(day_month: str, category: str) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reports WHERE category = ? AND created_at LIKE ?",
            (category, f"{day_month}.%"))
        reports = [dict(r) for r in await cursor.fetchall()]
        result = []
        for r in reports:
            cursor = await db.execute(
                "SELECT item_name, quantity FROM report_items WHERE report_id = ?", (r["id"],))
            items = [dict(i) for i in await cursor.fetchall()]
            result.append({"report": r, "items": items})
        return result


async def upsert_user(user_id: int, username: Optional[str], full_name: str, is_admin: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute("""INSERT INTO users (user_id, username, full_name, is_admin, last_seen, notify_enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                last_seen=excluded.last_seen,
                is_admin=CASE WHEN excluded.is_admin=1 THEN 1 ELSE users.is_admin END""",
                         (user_id, username, full_name, 1 if is_admin else 0, now))
        await db.commit()


async def set_notify_enabled(user_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET notify_enabled = ? WHERE user_id = ?",
                         (1 if enabled else 0, user_id))
        await db.commit()


async def get_notify_enabled(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT notify_enabled FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return True
        return bool(row[0])


async def get_users_for_notify() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE notify_enabled = 1")
        return [r[0] for r in await cursor.fetchall()]


# --- Группы для уведомлений ---
async def add_notify_group(chat_id: int, title: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        try:
            await db.execute(
                "INSERT INTO notify_groups (chat_id, title, added_at, enabled) VALUES (?, ?, ?, 1)",
                (chat_id, title or str(chat_id), now))
            await db.commit()
            return True
        except Exception:
            # Уже есть — просто включаем
            await db.execute(
                "UPDATE notify_groups SET enabled = 1, title = ? WHERE chat_id = ?",
                (title or str(chat_id), chat_id))
            await db.commit()
            return True


async def remove_notify_group(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM notify_groups WHERE chat_id = ?", (chat_id,))
        await db.commit()
        return cursor.rowcount > 0


async def set_notify_group_enabled(chat_id: int, enabled: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE notify_groups SET enabled = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_notify_groups(only_enabled: bool = False) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if only_enabled:
            cursor = await db.execute(
                "SELECT chat_id, title, added_at, enabled FROM notify_groups WHERE enabled = 1 ORDER BY added_at")
        else:
            cursor = await db.execute(
                "SELECT chat_id, title, added_at, enabled FROM notify_groups ORDER BY added_at")
        return [dict(r) for r in await cursor.fetchall()]


async def get_reminder_settings() -> Dict[str, dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM reminder_settings")
        return {r["group_key"]: dict(r) for r in await cursor.fetchall()}


async def update_reminder(group_key: str, days: str, time: str, enabled: int = 1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminder_settings SET days=?, time=?, enabled=? WHERE group_key=?",
            (days, time, enabled, group_key))
        await db.commit()


# ================== СОСТОЯНИЯ ==================
class InventoryStates(StatesGroup):
    choosing_category = State()
    entering_quantity = State()
    confirming = State()


class AdminStates(StatesGroup):
    waiting_add_category = State()
    waiting_add_item = State()

    waiting_del_choice = State()
    waiting_del_category = State()
    waiting_del_item = State()
    waiting_del_confirm = State()

    waiting_ren_choice = State()
    waiting_ren_category = State()
    waiting_rename_old = State()
    waiting_rename_new = State()
    waiting_ren_cat_old = State()
    waiting_ren_cat_new = State()
    waiting_ren_cat_confirm = State()

    waiting_reminder_group = State()
    waiting_reminder_days = State()
    waiting_reminder_time = State()

    waiting_report_date = State()
    waiting_report_category = State()

    waiting_groups_action = State()
    waiting_group_remove = State()


# ================== КЛАВИАТУРЫ ==================
def main_menu(is_admin: bool = False, notify_on: bool = True):
    b = ReplyKeyboardBuilder()
    b.button(text="📋 Начать подсчёт остатков")
    b.button(text="ℹ️ Помощь")
    if notify_on:
        b.button(text="🔕 Выключить уведомления в ЛС")
    else:
        b.button(text="🔔 Включить уведомления в ЛС")
    if is_admin:
        b.button(text="🛠 Админ-панель")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def categories_kb(categories: list):
    b = ReplyKeyboardBuilder()
    mapping = {
        "Овощи": "🥬 Овощи",
        "Лаваш": "🌯 Лаваш",
        "Мясо+Ингредиенты+Соусы+Специи+Фритюр": "🥩 Мясо+Ингр+Соусы+Специи+Фритюр",
        "Лимонады": "🥤 Лимонады",
        "Упаковка": "📦 Упаковка",
    }
    for cat in categories:
        b.button(text=mapping.get(cat, cat))
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def quantity_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="⏭ Пропустить (0)")
    b.button(text="❌ Отменить подсчёт")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def confirm_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="✅ Отправить отчёт")
    b.button(text="🔄 Начать заново")
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def admin_menu():
    b = ReplyKeyboardBuilder()
    b.button(text="➕ Добавить позицию")
    b.button(text="🗑 Удалить")
    b.button(text="✏️ Переименовать")
    b.button(text="📊 Последние отчёты")
    b.button(text="⏰ Настройки напоминаний")
    b.button(text="📢 Группы для уведомлений")
    b.button(text="📋 Список всех позиций")
    b.button(text="◀️ Назад в меню")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def cancel_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Отмена")
    return b.as_markup(resize_keyboard=True)


def yes_no_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="✅ Да, уверен")
    b.button(text="❌ Нет, отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def del_choice_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="🗑 Удалить позицию")
    b.button(text="🗑 Удалить целую категорию")
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def ren_choice_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="✏️ Переименовать позицию")
    b.button(text="✏️ Переименовать категорию")
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def reminder_group_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="Частые (Овощи, Лаваш, Мясо)")
    b.button(text="Раз в неделю (Лимонады, Упаковка)")
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def groups_menu_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="➕ Добавить группу")
    b.button(text="➖ Убрать группу")
    b.button(text="📋 Список групп")
    b.button(text="◀️ Назад в админку")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def short_to_full(text: str) -> Optional[str]:
    mapping = {
        "🥬 Овощи": "Овощи",
        "🌯 Лаваш": "Лаваш",
        "🥩 Мясо+Ингр+Соусы+Специи+Фритюр": "Мясо+Ингредиенты+Соусы+Специи+Фритюр",
        "🥤 Лимонады": "Лимонады",
        "📦 Упаковка": "Упаковка",
    }
    return mapping.get(text)


def parse_russian_days(text: str) -> Optional[str]:
    text = text.lower().replace(" и ", ",").replace(";", ",")
    parts = re.split(r"[,/\s]+", text)
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        eng = DAY_MAP.get(p)
        if eng and eng not in result:
            result.append(eng)
    if not result:
        return None
    return ",".join(result)


def days_to_russian(days_str: str) -> str:
    parts = [DAY_REVERSE.get(d.strip(), d.strip()) for d in days_str.split(",") if d.strip()]
    return ", ".join(parts)


def parse_times(text: str) -> Optional[str]:
    parts = re.split(r"[,;\s]+", text.strip())
    times = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})$", p)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        t = f"{h:02d}:{mi:02d}"
        if t not in times:
            times.append(t)
    if not times:
        return None
    return ",".join(times)


# ================== ХЕНДЛЕРЫ ==================
router = Router()


def is_admin(user) -> bool:
    if user.id in ADMIN_IDS:
        return True
    if user.username and user.username.lower() in ADMIN_USERNAMES:
        return True
    return False


async def _main_menu_for(user) -> object:
    admin = is_admin(user)
    notify = await get_notify_enabled(user.id)
    return main_menu(admin, notify)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    admin = is_admin(user)
    if admin:
        ADMIN_IDS.add(user.id)
    await upsert_user(user.id, user.username, user.full_name or "", admin)

    # Если /start в группе — предлагаем зарегистрировать группу
    if message.chat.type in ("group", "supergroup"):
        if admin:
            title = message.chat.title or str(message.chat.id)
            await add_notify_group(message.chat.id, title)
            await message.answer(
                f"✅ Группа «{title}» добавлена в список для уведомлений.\n"
                f"Chat ID: <code>{message.chat.id}</code>",
                parse_mode="HTML")
        else:
            await message.answer("Привет! Чтобы получать напоминания в эту группу, попроси админа добавить бота через админ-панель или написать /start в группе.")
        return

    text = "👋 Привет! Это бот для подсчёта остатков.\n\nНажми «📋 Начать подсчёт остатков» и выбери категорию."
    if admin:
        text += "\n\n🛠 Ты админ — тебе доступна админ-панель."
    notify = await get_notify_enabled(user.id)
    await message.answer(text, reply_markup=main_menu(admin, notify))


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "📌 <b>Как пользоваться</b>\n\n"
        "1. Нажми «Начать подсчёт остатков»\n"
        "2. Выбери категорию\n"
        "3. Вводи количество\n"
        "4. Подтверди и отправь\n\n"
        "Можно написать 0 или «Пропустить».\n\n"
        "🔔/🔕 — включить или выключить напоминания в личные сообщения.\n\n"
        "📢 Напоминания также могут приходить в группы (настраивает админ).",
        parse_mode="HTML")


@router.message(F.text == "◀️ Назад в меню")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=await _main_menu_for(message.from_user))


@router.message(F.text == "🔕 Выключить уведомления в ЛС")
async def disable_notify(message: Message):
    await set_notify_enabled(message.from_user.id, False)
    await message.answer(
        "🔕 Уведомления в личные сообщения выключены.\n"
        "Напоминания больше не будут приходить тебе в ЛС.\n"
        "(В группы, если они подключены, по-прежнему будут приходить.)",
        reply_markup=await _main_menu_for(message.from_user))


@router.message(F.text == "🔔 Включить уведомления в ЛС")
async def enable_notify(message: Message):
    await set_notify_enabled(message.from_user.id, True)
    await message.answer(
        "🔔 Уведомления в личные сообщения включены.\n"
        "Ты снова будешь получать напоминания о подсчёте.",
        reply_markup=await _main_menu_for(message.from_user))


@router.message(F.text == "📋 Начать подсчёт остатков")
async def start_inventory(message: Message, state: FSMContext):
    await state.clear()
    cats = await get_categories()
    await state.set_state(InventoryStates.choosing_category)
    await message.answer("Выбери категорию:", reply_markup=categories_kb(cats))


@router.message(InventoryStates.choosing_category, F.text == "❌ Отмена")
@router.message(InventoryStates.entering_quantity, F.text == "❌ Отменить подсчёт")
@router.message(InventoryStates.confirming, F.text == "❌ Отмена")
async def cancel_inventory(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Подсчёт отменён.", reply_markup=await _main_menu_for(message.from_user))


@router.message(InventoryStates.choosing_category)
async def category_chosen(message: Message, state: FSMContext):
    full_name = short_to_full(message.text)
    if not full_name:
        cats = await get_categories()
        if message.text in cats:
            full_name = message.text
        else:
            await message.answer("Выбери категорию кнопкой.")
            return
    items = await get_items_by_category(full_name)
    if not items:
        await message.answer("В этой категории нет позиций.")
        return
    await state.update_data(category=full_name, items=items, current_index=0, quantities={})
    await state.set_state(InventoryStates.entering_quantity)
    await ask_next_item(message, state)


async def ask_next_item(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data["current_index"]
    items = data["items"]
    if idx >= len(items):
        await show_summary(message, state)
        return
    item = items[idx]
    await message.answer(
        f"<b>{data['category']}</b>\nПозиция {idx + 1} из {len(items)}:\n\n👉 <b>{item}</b>\n\n"
        f"Введи количество или «Пропустить»:",
        parse_mode="HTML", reply_markup=quantity_kb())


@router.message(InventoryStates.entering_quantity, F.text == "⏭ Пропустить (0)")
async def skip_item(message: Message, state: FSMContext):
    data = await state.get_data()
    quantities = data.get("quantities", {})
    quantities[data["items"][data["current_index"]]] = "0"
    await state.update_data(quantities=quantities, current_index=data["current_index"] + 1)
    await ask_next_item(message, state)


@router.message(InventoryStates.entering_quantity)
async def quantity_entered(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer("Введи количество или нажми «Пропустить».")
        return
    data = await state.get_data()
    quantities = data.get("quantities", {})
    quantities[data["items"][data["current_index"]]] = text
    await state.update_data(quantities=quantities, current_index=data["current_index"] + 1)
    await ask_next_item(message, state)


async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    lines = [f"📋 <b>Отчёт: {data['category']}</b>\n"]
    for item, qty in data.get("quantities", {}).items():
        lines.append(f"• {item}: <b>{qty}</b>")
    lines.append("\nВсё верно? Отправляем?")
    await state.set_state(InventoryStates.confirming)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=confirm_kb())


@router.message(InventoryStates.confirming, F.text == "🔄 Начать заново")
async def restart_inventory(message: Message, state: FSMContext):
    await state.clear()
    cats = await get_categories()
    await state.set_state(InventoryStates.choosing_category)
    await message.answer("Выбери категорию заново:", reply_markup=categories_kb(cats))


@router.message(InventoryStates.confirming, F.text == "✅ Отправить отчёт")
async def send_report(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = message.from_user
    await save_report(user.id, user.username, user.full_name or "", data["category"], data.get("quantities", {}))

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        f"📥 <b>Новый отчёт</b>\n📅 {now}\nКатегория: <b>{data['category']}</b>\n"
        f"От: {user.full_name or 'Без имени'}" + (f" (@{user.username})" if user.username else "") + "\n"
    ]
    for item, qty in data.get("quantities", {}).items():
        lines.append(f"• {item}: <b>{qty}</b>")

    report_text = "\n".join(lines)
    for admin_id in list(ADMIN_IDS):
        try:
            await bot.send_message(admin_id, report_text, parse_mode="HTML")
        except Exception:
            pass

    await state.clear()
    await message.answer(f"✅ Отчёт от {now} отправлен! Спасибо 🙌",
                         reply_markup=await _main_menu_for(user))


# ---------- Админка ----------
@router.message(F.text == "🛠 Админ-панель")
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        await message.answer("⛔️ Только для администраторов.")
        return
    await state.clear()
    await message.answer("Админ-панель:", reply_markup=admin_menu())


@router.message(F.text == "📋 Список всех позиций")
async def list_all_items(message: Message):
    if not is_admin(message.from_user):
        return
    cats = await get_categories()
    parts = ["📋 <b>Все позиции:</b>\n"]
    for cat in cats:
        items = await get_items_by_category(cat)
        parts.append(f"\n<b>{cat}</b> ({len(items)}):")
        for item in items:
            parts.append(f"  • {item}")
    full = "\n".join(parts)
    if len(full) > 4000:
        chunk = ""
        for p in parts:
            if len(chunk) + len(p) > 3900:
                await message.answer(chunk, parse_mode="HTML")
                chunk = p
            else:
                chunk += "\n" + p if chunk else p
        if chunk:
            await message.answer(chunk, parse_mode="HTML")
    else:
        await message.answer(full, parse_mode="HTML")


# ===== ГРУППЫ ДЛЯ УВЕДОМЛЕНИЙ =====
@router.message(F.text == "📢 Группы для уведомлений")
async def groups_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    groups = await get_notify_groups()
    if groups:
        lines = ["📢 <b>Группы для уведомлений:</b>\n"]
        for g in groups:
            status = "✅" if g["enabled"] else "⏸"
            lines.append(f"{status} <b>{g['title']}</b>\n   ID: <code>{g['chat_id']}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
    else:
        await message.answer("Пока нет ни одной группы.\nДобавь бота в группу и нажми «➕ Добавить группу».")
    await state.set_state(AdminStates.waiting_groups_action)
    await message.answer("Управление группами:", reply_markup=groups_menu_kb())


@router.message(AdminStates.waiting_groups_action, F.text == "◀️ Назад в админку")
@router.message(AdminStates.waiting_groups_action, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_group_remove, F.text == "❌ Отмена")
async def groups_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-панель:", reply_markup=admin_menu())


@router.message(AdminStates.waiting_groups_action, F.text == "📋 Список групп")
async def groups_list(message: Message):
    if not is_admin(message.from_user):
        return
    groups = await get_notify_groups()
    if not groups:
        await message.answer("Список пуст.")
        return
    lines = ["📢 <b>Текущие группы:</b>\n"]
    for g in groups:
        status = "✅ вкл" if g["enabled"] else "⏸ выкл"
        lines.append(f"• <b>{g['title']}</b> ({status})\n  ID: <code>{g['chat_id']}</code>\n  добавлена: {g['added_at']}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(AdminStates.waiting_groups_action, F.text == "➕ Добавить группу")
async def groups_add_help(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await message.answer(
        "📌 <b>Как добавить группу</b>\n\n"
        "1. Добавь бота в нужную группу (как участника)\n"
        "2. Дай боту право писать сообщения\n"
        "3. В этой группе напиши команду:\n"
        "   <code>/start</code>\n"
        "   (от имени админа)\n\n"
        "Или перешли сюда любое сообщение из группы — бот возьмёт chat_id.\n\n"
        "Можно добавить <b>несколько групп</b> — напоминания уйдут во все сразу.",
        parse_mode="HTML", reply_markup=groups_menu_kb())


@router.message(AdminStates.waiting_groups_action, F.text == "➖ Убрать группу")
async def groups_remove_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    groups = await get_notify_groups()
    if not groups:
        await message.answer("Нечего убирать — список пуст.")
        return
    lines = ["Введи <b>chat_id</b> группы, которую нужно убрать:\n"]
    for g in groups:
        lines.append(f"• {g['title']}: <code>{g['chat_id']}</code>")
    await state.set_state(AdminStates.waiting_group_remove)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_group_remove)
async def groups_remove_process(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await message.answer("Введи числовой chat_id, например: <code>-1001234567890</code>", parse_mode="HTML")
        return
    ok = await remove_notify_group(chat_id)
    await state.set_state(AdminStates.waiting_groups_action)
    if ok:
        await message.answer(f"✅ Группа {chat_id} убрана из уведомлений.", reply_markup=groups_menu_kb())
    else:
        await message.answer("Группа с таким ID не найдена.", reply_markup=groups_menu_kb())


@router.message(AdminStates.waiting_groups_action, F.forward_from_chat)
async def groups_add_by_forward(message: Message, state: FSMContext):
    """Админ переслал сообщение из группы — берём chat_id."""
    if not is_admin(message.from_user):
        return
    chat = message.forward_from_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await message.answer("Это не сообщение из группы. Перешли сообщение именно из группы.")
        return
    title = chat.title or str(chat.id)
    await add_notify_group(chat.id, title)
    await message.answer(
        f"✅ Группа «{title}» добавлена!\nID: <code>{chat.id}</code>\n\n"
        f"Не забудь: бот должен быть участником этой группы и иметь право писать.",
        parse_mode="HTML", reply_markup=groups_menu_kb())


@router.message(AdminStates.waiting_groups_action)
async def groups_add_by_id(message: Message, state: FSMContext):
    """Можно просто вписать chat_id вручную."""
    if not is_admin(message.from_user):
        return
    text = message.text.strip()
    # Игнорируем кнопки меню
    if text in ("➕ Добавить группу", "➖ Убрать группу", "📋 Список групп", "◀️ Назад в админку"):
        return
    try:
        chat_id = int(text)
    except ValueError:
        await message.answer(
            "Не понял. Либо перешли сообщение из группы, либо введи chat_id числом,\n"
            "либо используй кнопки меню.")
        return
    await add_notify_group(chat_id, str(chat_id))
    await message.answer(
        f"✅ Группа с ID <code>{chat_id}</code> добавлена.\n"
        f"Если бот уже в группе — уведомления пойдут туда.",
        parse_mode="HTML", reply_markup=groups_menu_kb())


# Когда бота добавляют в группу — автоматически регистрируем (если добавил админ)
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added_to_group(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    # Кто добавил
    actor = event.from_user
    title = chat.title or str(chat.id)
    await add_notify_group(chat.id, title)
    logging.info(f"Bot added to group {chat.id} ({title}) by {actor.id if actor else '?'}")
    try:
        await event.bot.send_message(
            chat.id,
            f"✅ Бот добавлен. Группа «{title}» зарегистрирована для уведомлений о подсчёте остатков.")
    except Exception:
        pass
    # Уведомим админов в ЛС
    for admin_id in list(ADMIN_IDS):
        try:
            await event.bot.send_message(
                admin_id,
                f"📢 Бот добавлен в группу «{title}»\nID: <code>{chat.id}</code>\n"
                f"Группа автоматически добавлена в список уведомлений.",
                parse_mode="HTML")
        except Exception:
            pass


# ===== ДОБАВЛЕНИЕ =====
@router.message(F.text == "➕ Добавить позицию")
async def start_add_item(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_add_category)
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>название категории</b> (существующую или новую):\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_add_category, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_add_item, F.text == "❌ Отмена")
async def cancel_add(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_add_category)
async def process_add_category(message: Message, state: FSMContext):
    cat = message.text.strip()
    if not cat:
        await message.answer("Введи название категории.")
        return
    await state.update_data(add_category=cat)
    await state.set_state(AdminStates.waiting_add_item)
    await message.answer(
        f"Категория: <b>{cat}</b>\n\nТеперь введи <b>название новой позиции</b>:",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_add_item)
async def process_add_item(message: Message, state: FSMContext):
    data = await state.get_data()
    cat = data.get("add_category")
    name = message.text.strip()
    if not name or not cat:
        await message.answer("Ошибка. Попробуй заново.")
        await state.clear()
        await message.answer("Админ-панель:", reply_markup=admin_menu())
        return
    ok = await add_item(cat, name)
    await state.clear()
    if ok:
        await message.answer(f"✅ Позиция «{name}» добавлена в категорию «{cat}».", reply_markup=admin_menu())
    else:
        await message.answer("Не удалось добавить (возможно, уже существует).", reply_markup=admin_menu())


# ===== УДАЛЕНИЕ =====
@router.message(F.text == "🗑 Удалить")
async def start_delete(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_del_choice)
    await message.answer("Что удаляем?", reply_markup=del_choice_kb())


@router.message(AdminStates.waiting_del_choice, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_del_category, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_del_item, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_del_confirm, F.text == "❌ Нет, отмена")
@router.message(AdminStates.waiting_del_confirm, F.text == "❌ Отмена")
async def cancel_del(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_del_choice, F.text == "🗑 Удалить позицию")
async def del_item_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_del_category)
    await state.update_data(del_mode="item")
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>название категории</b>, из которой удалить позицию:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_del_choice, F.text == "🗑 Удалить целую категорию")
async def del_cat_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_del_category)
    await state.update_data(del_mode="category")
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"⚠️ Введи <b>название категории</b>, которую нужно полностью удалить:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_del_category)
async def process_del_category_step(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("del_mode", "item")
    cat = message.text.strip()

    if mode == "category":
        cats = await get_categories()
        if cat not in cats:
            await message.answer("Категория не найдена. Попробуй ещё раз или «❌ Отмена».")
            return
        items = await get_items_by_category(cat)
        await state.update_data(del_category=cat)
        await state.set_state(AdminStates.waiting_del_confirm)
        await message.answer(
            f"⚠️ <b>ВНИМАНИЕ!</b>\n\n"
            f"Ты собираешься <b>полностью удалить категорию «{cat}»</b>\n"
            f"и все позиции внутри неё ({len(items)} шт.).\n\n"
            f"Это действие необратимо.\n\n"
            f"Ты уверен?",
            parse_mode="HTML", reply_markup=yes_no_kb())
        return

    items = await get_items_by_category(cat)
    if not items:
        await message.answer("Категория не найдена или пуста. Попробуй ещё раз или «❌ Отмена».")
        return
    await state.update_data(del_category=cat)
    await state.set_state(AdminStates.waiting_del_item)
    items_text = "\n".join(f"• {i}" for i in items)
    await message.answer(
        f"Категория: <b>{cat}</b>\n\nВведи <b>точное название позиции</b> для удаления:\n\n{items_text}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_del_confirm, F.text == "✅ Да, уверен")
async def confirm_del_category(message: Message, state: FSMContext):
    data = await state.get_data()
    cat = data.get("del_category")
    ok = await delete_category(cat)
    await state.clear()
    if ok:
        await message.answer(f"✅ Категория «{cat}» и все её позиции удалены.", reply_markup=admin_menu())
    else:
        await message.answer("Не удалось удалить.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_del_item)
async def process_del_item(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await deactivate_item(data.get("del_category"), message.text.strip())
    await state.clear()
    await message.answer("✅ Удалено." if ok else "Не найдено.", reply_markup=admin_menu())


# ===== ПЕРЕИМЕНОВАНИЕ =====
@router.message(F.text == "✏️ Переименовать")
async def start_rename(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_ren_choice)
    await message.answer("Что переименовываем?", reply_markup=ren_choice_kb())


@router.message(AdminStates.waiting_ren_choice, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_ren_category, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_rename_old, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_rename_new, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_ren_cat_old, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_ren_cat_new, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_ren_cat_confirm, F.text == "❌ Нет, отмена")
@router.message(AdminStates.waiting_ren_cat_confirm, F.text == "❌ Отмена")
async def cancel_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_ren_choice, F.text == "✏️ Переименовать позицию")
async def ren_item_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ren_category)
    await state.update_data(ren_mode="item")
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>название категории</b>:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_ren_choice, F.text == "✏️ Переименовать категорию")
async def ren_cat_start(message: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ren_cat_old)
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>текущее название категории</b>, которую хочешь переименовать:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_ren_category)
async def process_ren_category_for_item(message: Message, state: FSMContext):
    cat = message.text.strip()
    items = await get_items_by_category(cat)
    if not items:
        await message.answer("Категория не найдена или пуста. Попробуй ещё раз или «❌ Отмена».")
        return
    await state.update_data(ren_category=cat)
    await state.set_state(AdminStates.waiting_rename_old)
    items_text = "\n".join(f"• {i}" for i in items)
    await message.answer(
        f"Категория: <b>{cat}</b>\n\nВведи <b>старое название</b> позиции:\n\n{items_text}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_rename_old)
async def process_rename_old(message: Message, state: FSMContext):
    await state.update_data(old_name=message.text.strip())
    await state.set_state(AdminStates.waiting_rename_new)
    await message.answer("Теперь введи <b>новое название</b> позиции:", parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_rename_new)
async def process_rename_new(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await rename_item(data.get("ren_category"), data.get("old_name"), message.text.strip())
    await state.clear()
    await message.answer("✅ Переименовано." if ok else "Не удалось.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_ren_cat_old)
async def process_ren_cat_old(message: Message, state: FSMContext):
    old = message.text.strip()
    cats = await get_categories()
    if old not in cats:
        await message.answer("Категория не найдена. Попробуй ещё раз или «❌ Отмена».")
        return
    await state.update_data(ren_cat_old=old)
    await state.set_state(AdminStates.waiting_ren_cat_new)
    await message.answer(
        f"Текущее название: <b>{old}</b>\n\nВведи <b>новое название</b> категории:",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_ren_cat_new)
async def process_ren_cat_new(message: Message, state: FSMContext):
    new_name = message.text.strip()
    if not new_name:
        await message.answer("Введи новое название.")
        return
    data = await state.get_data()
    old = data.get("ren_cat_old")
    await state.update_data(ren_cat_new=new_name)
    await state.set_state(AdminStates.waiting_ren_cat_confirm)
    await message.answer(
        f"⚠️ Переименовать категорию?\n\n"
        f"«{old}» → «{new_name}»\n\n"
        f"Все позиции останутся на месте, изменится только название категории.\n\n"
        f"Ты уверен?",
        parse_mode="HTML", reply_markup=yes_no_kb())


@router.message(AdminStates.waiting_ren_cat_confirm, F.text == "✅ Да, уверен")
async def confirm_ren_category(message: Message, state: FSMContext):
    data = await state.get_data()
    old = data.get("ren_cat_old")
    new = data.get("ren_cat_new")
    ok = await rename_category(old, new)
    await state.clear()
    if ok:
        await message.answer(f"✅ Категория переименована:\n«{old}» → «{new}»", reply_markup=admin_menu())
    else:
        await message.answer("Не удалось (возможно, такое название уже есть).", reply_markup=admin_menu())


# ===== ОТЧЁТЫ =====
@router.message(F.text == "📊 Последние отчёты")
async def last_reports(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    reports = await get_last_reports(12)
    if not reports:
        await message.answer("Отчётов пока нет.")
        return
    lines = ["📊 <b>Последние отчёты:</b>\n"]
    for r in reports:
        who = f"@{r['username']}" if r["username"] else (r["full_name"] or "—")
        lines.append(f"📅 <b>{r['created_at']}</b>\n   {r['category']}\n   {who}\n")
    lines.append(
        "\nЧтобы открыть полный отчёт — введи <b>день.месяц</b> и категорию.\n"
        "Пример: сначала <code>16.10</code>, потом название категории.")
    await message.answer("\n".join(lines), parse_mode="HTML")
    await state.set_state(AdminStates.waiting_report_date)
    await message.answer("Введи дату в формате <b>дд.мм</b> (например 16.10):",
                         parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_report_date, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_report_category, F.text == "❌ Отмена")
async def cancel_report_search(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_report_date)
async def process_report_date(message: Message, state: FSMContext):
    text = message.text.strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?$", text)
    if not m:
        await message.answer("Формат: <b>16.10</b> или <b>16.10.2026</b>", parse_mode="HTML")
        return
    day = m.group(1).zfill(2)
    month = m.group(2).zfill(2)
    day_month = f"{day}.{month}"
    await state.update_data(report_day_month=day_month)
    await state.set_state(AdminStates.waiting_report_category)
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Дата: <b>{day_month}</b>\n\nТеперь введи <b>название категории</b>:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_report_category)
async def process_report_category(message: Message, state: FSMContext):
    data = await state.get_data()
    day_month = data.get("report_day_month")
    category = message.text.strip()
    full = short_to_full(category) or category
    results = await get_reports_by_day_month_and_category(day_month, full)
    await state.clear()
    if not results:
        await message.answer(
            f"Отчётов по категории «{full}» за {day_month} не найдено.",
            reply_markup=admin_menu())
        return
    for res in results:
        r = res["report"]
        lines = [
            f"📄 <b>Отчёт от {r['created_at']}</b>\n"
            f"Категория: {r['category']}\n"
            f"От: {r['full_name']} (@{r['username'] or '—'})\n"
        ]
        for it in res["items"]:
            lines.append(f"• {it['item_name']}: <b>{it['quantity']}</b>")
        await message.answer("\n".join(lines), parse_mode="HTML")
    await message.answer("Готово.", reply_markup=admin_menu())


# ===== НАПОМИНАНИЯ =====
@router.message(F.text == "⏰ Настройки напоминаний")
async def reminder_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    settings = await get_reminder_settings()
    freq = settings.get("frequent", {})
    week = settings.get("weekly", {})

    freq_days = days_to_russian(freq.get("days", "—")) if freq.get("enabled") else "выключено"
    week_days = days_to_russian(week.get("days", "—")) if week.get("enabled") else "выключено"
    freq_time = freq.get("time", "—") if freq.get("enabled") else "—"
    week_time = week.get("time", "—") if week.get("enabled") else "—"

    text = (
        f"⏰ <b>Настройки напоминаний</b>\n\n"
        f"<b>Частые</b> (Овощи, Лаваш, Мясо-группа):\n"
        f"  Дни: {freq_days}\n"
        f"  Время: {freq_time}\n\n"
        f"<b>Раз в неделю</b> (Лимонады, Упаковка):\n"
        f"  Дни: {week_days}\n"
        f"  Время: {week_time}\n\n"
        f"Можно указать несколько времён через запятую.\n"
        f"Уведомления уходят: в ЛС (кто включил) + во все подключённые группы.\n\n"
        f"Выбери, какую группу изменить:"
    )
    await state.clear()
    await state.set_state(AdminStates.waiting_reminder_group)
    await message.answer(text, parse_mode="HTML", reply_markup=reminder_group_kb())


@router.message(AdminStates.waiting_reminder_group, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_reminder_days, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_reminder_time, F.text == "❌ Отмена")
async def cancel_reminder(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_reminder_group)
async def process_reminder_group(message: Message, state: FSMContext):
    text = message.text.lower()
    if "частые" in text or "овощи" in text or "лаваш" in text or "мясо" in text:
        group = "frequent"
        title = "Частые"
    elif "неделю" in text or "лимонад" in text or "упаковк" in text:
        group = "weekly"
        title = "Раз в неделю"
    else:
        await message.answer("Выбери кнопку: «Частые» или «Раз в неделю».")
        return
    await state.update_data(rem_group=group, rem_title=title)
    await state.set_state(AdminStates.waiting_reminder_days)
    await message.answer(
        f"Группа: <b>{title}</b>\n\n"
        f"Введи <b>дни</b> (можно несколько через запятую):\n"
        f"Пример: <code>понедельник, среда, пятница</code>\n"
        f"Или: <code>пн, ср, пт</code>\n\n"
        f"Чтобы выключить — напиши <code>выкл</code>",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_reminder_days)
async def process_reminder_days(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    data = await state.get_data()
    group = data.get("rem_group")
    title = data.get("rem_title")

    if text in ("выкл", "выключить", "off", "откл"):
        await update_reminder(group, "mon", "10:00", 0)
        setup_scheduler()
        await state.clear()
        await message.answer(f"✅ Напоминания «{title}» выключены.", reply_markup=admin_menu())
        return

    days_eng = parse_russian_days(text)
    if not days_eng:
        await message.answer(
            "Не понял дни. Напиши, например:\n<code>понедельник, вторник</code>\nили <code>пн, вт, пт</code>",
            parse_mode="HTML")
        return

    await state.update_data(rem_days=days_eng)
    await state.set_state(AdminStates.waiting_reminder_time)
    await message.answer(
        f"Дни: <b>{days_to_russian(days_eng)}</b>\n\n"
        f"Теперь введи <b>время</b> (можно несколько через запятую):\n"
        f"Пример: <code>10:00</code>\n"
        f"Или: <code>10:00, 11:05, 18:30</code>",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_reminder_time)
async def process_reminder_time(message: Message, state: FSMContext):
    times_str = parse_times(message.text)
    if not times_str:
        await message.answer(
            "Формат: <b>10:00</b> или несколько: <b>10:00, 11:05, 18:30</b>",
            parse_mode="HTML")
        return

    data = await state.get_data()
    group = data.get("rem_group")
    days = data.get("rem_days")
    title = data.get("rem_title")

    await update_reminder(group, days, times_str, 1)
    setup_scheduler()

    await state.clear()
    await message.answer(
        f"✅ Напоминания «{title}» обновлены:\n"
        f"Дни: {days_to_russian(days)}\n"
        f"Время: {times_str}",
        reply_markup=admin_menu())


# ================== ПЛАНИРОВЩИК ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


async def send_reminder(group_key: str):
    settings = await get_reminder_settings()
    conf = settings.get(group_key)
    if not conf or not conf.get("enabled"):
        return
    categories = REMINDER_GROUPS.get(group_key, [])
    text = (
        f"⏰ <b>Напоминание о подсчёте остатков</b>\n\n"
        f"Пора сделать подсчёт:\n<b>{', '.join(categories)}</b>\n\n"
        f"Открой бота → «📋 Начать подсчёт остатков»."
    )

    # 1) Личные сообщения тем, кто включил уведомления
    for uid in await get_users_for_notify():
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception:
            pass

    # 2) Все подключённые группы (можно несколько сразу)
    for g in await get_notify_groups(only_enabled=True):
        try:
            await bot.send_message(g["chat_id"], text, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Failed to send to group {g['chat_id']}: {e}")


def setup_scheduler():
    scheduler.remove_all_jobs()

    async def _load_and_schedule():
        settings = await get_reminder_settings()
        for group_key, conf in settings.items():
            if not conf.get("enabled"):
                continue
            days_str = conf.get("days", "")
            time_str = conf.get("time", "10:00")
            days = [d.strip() for d in days_str.split(",") if d.strip()]
            if not days:
                continue
            times = [t.strip() for t in time_str.split(",") if t.strip()]
            for i, t in enumerate(times):
                try:
                    hour, minute = map(int, t.split(":"))
                except Exception:
                    continue
                trigger = CronTrigger(
                    day_of_week=",".join(days),
                    hour=hour,
                    minute=minute,
                    timezone="Europe/Moscow")
                job_id = f"rem_{group_key}_{i}"
                scheduler.add_job(
                    send_reminder,
                    trigger=trigger,
                    args=[group_key],
                    id=job_id,
                    replace_existing=True)
                logging.info(f"Scheduled {group_key} [{i}]: days={days}, time={hour:02d}:{minute:02d}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_load_and_schedule())
    except RuntimeError:
        pass


async def on_startup():
    await init_db()
    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
    logging.info("Bot started, scheduler running")


async def main():
    logging.basicConfig(level=logging.INFO)
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())