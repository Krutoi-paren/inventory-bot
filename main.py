import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
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

# Русские дни → английские для APScheduler
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
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, is_admin INTEGER DEFAULT 0, last_seen TEXT)""")
        await db.commit()

        for cat_name, items in INITIAL_ITEMS.items():
            await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_name,))
            cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,))
            cat_id = (await cursor.fetchone())[0]
            for item in items:
                await db.execute("INSERT OR IGNORE INTO items (category_id, name) VALUES (?, ?)", (cat_id, item))

        await db.execute("INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)", ("frequent", "tue,fri", "10:00"))
        await db.execute("INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)", ("weekly", "mon", "10:00"))
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


async def add_category(name: str) -> bool:
    """Добавить новую категорию. Возвращает True если создана, False если уже есть."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO categories (name) VALUES (?)", (name.strip(),))
            await db.commit()
            return True
        except:
            return False


async def add_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        # Если категории нет — создаём
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
        except:
            return False


async def deactivate_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""UPDATE items SET is_active = 0
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)""", (item_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


async def rename_item(category_name: str, old_name: str, new_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""UPDATE items SET name = ?
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)""", (new_name, old_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


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
    """Ищем отчёты по дню.месяцу (например 16.10) и категории. Год и время игнорируем при поиске."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # created_at выглядит как "16.10.2026 20:32"
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
        await db.execute("""INSERT INTO users (user_id, username, full_name, is_admin, last_seen) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name, last_seen=excluded.last_seen,
            is_admin=CASE WHEN excluded.is_admin=1 THEN 1 ELSE users.is_admin END""",
                         (user_id, username, full_name, 1 if is_admin else 0, now))
        await db.commit()


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


async def get_all_active_users() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cursor.fetchall()]


# ================== СОСТОЯНИЯ ==================
class InventoryStates(StatesGroup):
    choosing_category = State()
    entering_quantity = State()
    confirming = State()


class AdminStates(StatesGroup):
    # Добавление: сначала категория, потом позиция
    waiting_add_category = State()
    waiting_add_item = State()
    # Удаление: сначала категория, потом позиция
    waiting_del_category = State()
    waiting_del_item = State()
    # Переименование: категория → старое → новое
    waiting_ren_category = State()
    waiting_rename_old = State()
    waiting_rename_new = State()
    # Напоминания: выбор группы → дни → время
    waiting_reminder_group = State()
    waiting_reminder_days = State()
    waiting_reminder_time = State()
    # Поиск отчёта: дата → категория
    waiting_report_date = State()
    waiting_report_category = State()


# ================== КЛАВИАТУРЫ ==================
def main_menu(is_admin: bool = False):
    b = ReplyKeyboardBuilder()
    b.button(text="📋 Начать подсчёт остатков")
    b.button(text="ℹ️ Помощь")
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
    b.button(text="🗑 Удалить позицию")
    b.button(text="✏️ Переименовать позицию")
    b.button(text="📊 Последние отчёты")
    b.button(text="⏰ Настройки напоминаний")
    b.button(text="📋 Список всех позиций")
    b.button(text="◀️ Назад в меню")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def cancel_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Отмена")
    return b.as_markup(resize_keyboard=True)


def reminder_group_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="Частые (Овощи, Лаваш, Мясо)")
    b.button(text="Раз в неделю (Лимонады, Упаковка)")
    b.button(text="❌ Отмена")
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
    """Превращает 'понедельник, вторник, пятница' → 'mon,tue,fri'"""
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


# ================== ХЕНДЛЕРЫ ==================
router = Router()


def is_admin(user) -> bool:
    if user.id in ADMIN_IDS:
        return True
    if user.username and user.username.lower() in ADMIN_USERNAMES:
        return True
    return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    admin = is_admin(user)
    if admin:
        ADMIN_IDS.add(user.id)
    await upsert_user(user.id, user.username, user.full_name or "", admin)
    text = "👋 Привет! Это бот для подсчёта остатков.\n\nНажми «📋 Начать подсчёт остатков» и выбери категорию."
    if admin:
        text += "\n\n🛠 Ты админ — тебе доступна админ-панель."
    await message.answer(text, reply_markup=main_menu(admin))


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "📌 <b>Как пользоваться</b>\n\n"
        "1. Нажми «Начать подсчёт остатков»\n"
        "2. Выбери категорию\n"
        "3. Вводи количество\n"
        "4. Подтверди и отправь\n\n"
        "Можно написать 0 или «Пропустить».",
        parse_mode="HTML")


@router.message(F.text == "◀️ Назад в меню")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu(is_admin(message.from_user)))


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
    await message.answer("Подсчёт отменён.", reply_markup=main_menu(is_admin(message.from_user)))


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
    report_id = await save_report(
        user.id, user.username, user.full_name or "", data["category"], data.get("quantities", {}))

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
        except:
            pass

    await state.clear()
    await message.answer(f"✅ Отчёт от {now} отправлен! Спасибо 🙌", reply_markup=main_menu(is_admin(user)))


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


# ===== ДОБАВЛЕНИЕ ПОЗИЦИИ =====
# Сначала категория (можно новую!), потом название позиции
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


# ===== УДАЛЕНИЕ ПОЗИЦИИ =====
@router.message(F.text == "🗑 Удалить позицию")
async def start_del_item(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_del_category)
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>название категории</b>, из которой удалить:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_del_category, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_del_item, F.text == "❌ Отмена")
async def cancel_del(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_del_category)
async def process_del_category(message: Message, state: FSMContext):
    cat = message.text.strip()
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


@router.message(AdminStates.waiting_del_item)
async def process_del_item(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await deactivate_item(data.get("del_category"), message.text.strip())
    await state.clear()
    await message.answer("✅ Удалено." if ok else "Не найдено.", reply_markup=admin_menu())


# ===== ПЕРЕИМЕНОВАНИЕ =====
@router.message(F.text == "✏️ Переименовать позицию")
async def start_rename(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_ren_category)
    cats = await get_categories()
    cat_list = "\n".join(f"• {c}" for c in cats)
    await message.answer(
        f"Введи <b>название категории</b>:\n\n{cat_list}",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_ren_category, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_rename_old, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_rename_new, F.text == "❌ Отмена")
async def cancel_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_ren_category)
async def process_ren_category(message: Message, state: FSMContext):
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
    await message.answer("Теперь введи <b>новое название</b>:", parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_rename_new)
async def process_rename_new(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await rename_item(data.get("ren_category"), data.get("old_name"), message.text.strip())
    await state.clear()
    await message.answer("✅ Переименовано." if ok else "Не удалось.", reply_markup=admin_menu())


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
        # Показываем дату как дд.мм + время (год убираем из отображения для удобства)
        date_part = r["created_at"]  # 16.10.2026 20:32
        lines.append(f"📅 <b>{date_part}</b>\n   {r['category']}\n   {who}\n")
    lines.append(
        "\nЧтобы открыть полный отчёт — введи <b>день.месяц</b> и категорию.\n"
        "Пример: сначала напиши <code>16.10</code>, потом выбери/напиши категорию.")
    await message.answer("\n".join(lines), parse_mode="HTML")
    await state.set_state(AdminStates.waiting_report_date)
    await message.answer("Введи дату в формате <b>дд.мм</b> (например 16.10):", parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_report_date, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_report_category, F.text == "❌ Отмена")
async def cancel_report_search(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_report_date)
async def process_report_date(message: Message, state: FSMContext):
    text = message.text.strip()
    # Принимаем дд.мм или дд.мм.гггг
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
    # Пробуем короткое имя → полное
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

    text = (
        f"⏰ <b>Настройки напоминаний</b>\n\n"
        f"<b>Частые</b> (Овощи, Лаваш, Мясо-группа):\n"
        f"  Дни: {freq_days}\n"
        f"  Время: {freq.get('time', '—')}\n\n"
        f"<b>Раз в неделю</b> (Лимонады, Упаковка):\n"
        f"  Дни: {week_days}\n"
        f"  Время: {week.get('time', '—')}\n\n"
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
        setup_scheduler()  # пересоздаём задачи
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
        f"Теперь введи <b>время</b> в формате ЧЧ:ММ\n"
        f"Пример: <code>10:00</code> или <code>09:30</code>",
        parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_reminder_time)
async def process_reminder_time(message: Message, state: FSMContext):
    text = message.text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m:
        await message.answer("Формат времени: <b>10:00</b> или <b>9:30</b>", parse_mode="HTML")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("Часы 0–23, минуты 0–59.")
        return
    time_str = f"{hour:02d}:{minute:02d}"

    data = await state.get_data()
    group = data.get("rem_group")
    days = data.get("rem_days")
    title = data.get("rem_title")

    await update_reminder(group, days, time_str, 1)
    setup_scheduler()  # ВАЖНО: пересоздаём задачи планировщика

    await state.clear()
    await message.answer(
        f"✅ Напоминания «{title}» обновлены:\n"
        f"Дни: {days_to_russian(days)}\n"
        f"Время: {time_str}",
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
    for uid in await get_all_active_users():
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except:
            pass


def setup_scheduler():
    """Пересоздаёт все задачи напоминаний из БД. Вызывать после любого изменения настроек."""
    scheduler.remove_all_jobs()

    async def _load_and_schedule():
        settings = await get_reminder_settings()
        for group_key, conf in settings.items():
            if not conf.get("enabled"):
                continue
            days_str = conf.get("days", "")
            time_str = conf.get("time", "10:00")
            try:
                hour, minute = map(int, time_str.split(":"))
            except:
                hour, minute = 10, 0
            days = [d.strip() for d in days_str.split(",") if d.strip()]
            if not days:
                continue
            trigger = CronTrigger(
                day_of_week=",".join(days),
                hour=hour,
                minute=minute,
                timezone="Europe/Moscow")
            scheduler.add_job(
                send_reminder,
                trigger=trigger,
                args=[group_key],
                id=f"rem_{group_key}",
                replace_existing=True)
            logging.info(f"Scheduled {group_key}: days={days}, time={hour:02d}:{minute:02d}")

    # Запускаем загрузку настроек
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_load_and_schedule())
    except RuntimeError:
        # Если цикла ещё нет — создадим позже
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