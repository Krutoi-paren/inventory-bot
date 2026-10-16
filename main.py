import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8917698584:AAGyjwsbdX7oP8MyUDz8qzsjXM8EKZeFZ24"  # ← можно заменить на новый

ADMIN_USERNAMES = {"kitt069", "jar_yo", "rudiment01"}
ADMIN_IDS = set()  # заполнится автоматически после /start

DB_PATH = Path(__file__).parent / "inventory.db"

# ================== НАЧАЛЬНЫЕ ДАННЫЕ ==================
INITIAL_ITEMS = {
    "Овощи": [
        "лук", "перец болгарский", "капуста пекинская", "томаты", "огурцы"
    ],
    "Лаваш": [
        "лаваш обычный", "лаваш сырный", "лаваш зеленый (сельдерей)",
        "лаваш розовый (томатный)", "лаваш черный", "булки для донера",
        "пита", "булочки для хот-дога"
    ],
    "Мясо+Ингредиенты+Соусы+Специи+Фритюр": [
        "куриное филе", "сосиски",
        "ветчина", "сыр чеддер", "сыр голландский", "огурцы маринованные",
        "лук криспи", "халапеньо", "острый соус",
        "паприка", "соль", "приправа для курицы", "приправа для шашлыка",
        "масло для фритюра (бутыль)",
        "майонез", "сметана", "барбекю соус", "бургер соус", "сырный соус",
        "чесночный соус", "гриль соус", "кетчуп", "горчица",
        "картофель-фри", "картофель по-деревенски", "наггетсы",
        "стрипсы", "сырные палочки"
    ],
    "Лимонады": [
        "основа персиковая", "основа личи", "основа бабл гам",
        "основа груша-жасмин", "основа лаванда-лимон", "сушенный грейпфрут",
        "краситель золотой", "краситель розовый", "краситель серый",
        "маршмеллоу", "сушеная лаванда", "персики консервированные (банка)",
        "пюре манго/абрикос (банка)"
    ],
    "Упаковка": [
        "конверт для шаурмы", "конверт для сэндвича", "коробка для лодочки",
        "коробка для фритюра (квадратная)", "фольга", "пленка", "пакеты",
        "стаканы для лимонада", "стаканы для кофе", "стаканы для чая",
        "крышки для стаканов лимонада", "крышки для стаканов кофе",
        "трубочки", "палочки размешивания", "сахар порционный",
        "вилки одноразовые", "подстаканники"
    ],
}

REMINDER_GROUPS = {
    "frequent": ["Овощи", "Лаваш", "Мясо+Ингредиенты+Соусы+Специи+Фритюр"],
    "weekly": ["Лимонады", "Упаковка"],
}

# ================== БАЗА ДАННЫХ ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (category_id) REFERENCES categories(id),
                UNIQUE(category_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS report_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES reports(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminder_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT UNIQUE NOT NULL,
                days TEXT NOT NULL,
                time TEXT NOT NULL DEFAULT '10:00',
                enabled INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_admin INTEGER DEFAULT 0,
                last_seen TEXT
            )
        """)
        await db.commit()

        for cat_name, items in INITIAL_ITEMS.items():
            await db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_name,))
            cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,))
            row = await cursor.fetchone()
            cat_id = row[0]
            for item in items:
                await db.execute(
                    "INSERT OR IGNORE INTO items (category_id, name) VALUES (?, ?)",
                    (cat_id, item)
                )

        await db.execute(
            "INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)",
            ("frequent", "tue,fri", "10:00")
        )
        await db.execute(
            "INSERT OR IGNORE INTO reminder_settings (group_key, days, time) VALUES (?, ?, ?)",
            ("weekly", "mon", "10:00")
        )
        await db.commit()


async def get_categories() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT name FROM categories ORDER BY id")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_items_by_category(category_name: str) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT i.name FROM items i
            JOIN categories c ON c.id = i.category_id
            WHERE c.name = ? AND i.is_active = 1
            ORDER BY i.id
        """, (category_name,))
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def add_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM categories WHERE name = ?", (category_name,))
        row = await cursor.fetchone()
        if not row:
            return False
        try:
            await db.execute(
                "INSERT INTO items (category_id, name) VALUES (?, ?)",
                (row[0], item_name.strip())
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def deactivate_item(category_name: str, item_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE items SET is_active = 0
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)
        """, (item_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


async def rename_item(category_name: str, old_name: str, new_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE items SET name = ?
            WHERE name = ? AND category_id = (SELECT id FROM categories WHERE name = ?)
        """, (new_name, old_name, category_name))
        await db.commit()
        return cursor.rowcount > 0


async def save_report(user_id: int, username: Optional[str], full_name: str,
                      category: str, items: Dict[str, str]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat(timespec="seconds")
        cursor = await db.execute(
            "INSERT INTO reports (user_id, username, full_name, category, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, category, now)
        )
        report_id = cursor.lastrowid
        for item_name, qty in items.items():
            await db.execute(
                "INSERT INTO report_items (report_id, item_name, quantity) VALUES (?, ?, ?)",
                (report_id, item_name, qty)
            )
        await db.commit()
        return report_id


async def get_last_reports(limit: int = 20) -> List[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT id, user_id, username, full_name, category, created_at
            FROM reports ORDER BY created_at DESC LIMIT ?
        """, (limit,))
        return [dict(r) for r in await cursor.fetchall()]


async def get_report_details(report_id: int) -> Tuple[Optional[dict], List[dict]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        report = await cursor.fetchone()
        if not report:
            return None, []
        cursor = await db.execute(
            "SELECT item_name, quantity FROM report_items WHERE report_id = ?", (report_id,)
        )
        return dict(report), [dict(i) for i in await cursor.fetchall()]


async def upsert_user(user_id: int, username: Optional[str], full_name: str, is_admin: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, is_admin, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen = excluded.last_seen,
                is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE users.is_admin END
        """, (user_id, username, full_name, 1 if is_admin else 0, now))
        await db.commit()


async def get_reminder_settings() -> Dict[str, dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM reminder_settings")
        return {r["group_key"]: dict(r) for r in await cursor.fetchall()}


async def update_reminder(group_key: str, days: str, time: str, enabled: int = 1):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE reminder_settings SET days = ?, time = ?, enabled = ? WHERE group_key = ?
        """, (days, time, enabled, group_key))
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
    waiting_add_item = State()
    waiting_del_item = State()
    waiting_rename_old = State()
    waiting_rename_new = State()
    waiting_reminder = State()


# ================== КЛАВИАТУРЫ ==================
def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="📋 Начать подсчёт остатков")
    b.button(text="ℹ️ Помощь")
    if is_admin:
        b.button(text="🛠 Админ-панель")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def categories_kb(categories: list) -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    mapping = {
        "Овощи": "🥬 Овощи",
        "Лаваш": "🌯 Лаваш",
        "Мясо+Ингредиенты+Соусы+Специи+Фритюр": "🥩 Мясо + Ингредиенты + Соусы + Специи + Фритюр",
        "Лимонады": "🥤 Лимонады",
        "Упаковка": "📦 Упаковка",
    }
    for cat in categories:
        b.button(text=mapping.get(cat, cat))
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def quantity_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="⏭ Пропустить (0)")
    b.button(text="❌ Отменить подсчёт")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def confirm_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="✅ Отправить отчёт")
    b.button(text="🔄 Начать заново")
    b.button(text="❌ Отмена")
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def admin_menu() -> ReplyKeyboardMarkup:
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


def categories_inline(categories: list, action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cat in categories:
        b.button(text=cat[:40], callback_data=f"{action}:{cat}")
    b.adjust(1)
    return b.as_markup()


def cancel_kb() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="❌ Отмена")
    return b.as_markup(resize_keyboard=True)


def short_to_full(text: str) -> Optional[str]:
    mapping = {
        "🥬 Овощи": "Овощи",
        "🌯 Лаваш": "Лаваш",
        "🥩 Мясо + Ингредиенты + Соусы + Специи + Фритюр": "Мясо+Ингредиенты+Соусы+Специи+Фритюр",
        "🥤 Лимонады": "Лимонады",
        "📦 Упаковка": "Упаковка",
    }
    return mapping.get(text)


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

    text = (
        "👋 Привет! Это бот для подсчёта остатков.\n\n"
        "Нажми «📋 Начать подсчёт остатков», выбери категорию "
        "и по очереди укажи количество каждой позиции.\n\n"
        "Если позиции нет — напиши 0 или нажми «Пропустить»."
    )
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
        "3. Вводи количество по каждой позиции\n"
        "4. Подтверди и отправь отчёт\n\n"
        "Можно написать <code>0</code> или нажать «Пропустить».\n"
        "В любой момент — «Отмена».",
        parse_mode="HTML"
    )


@router.message(F.text == "◀️ Назад в меню")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu(is_admin(message.from_user)))


# ---------- Подсчёт остатков ----------
@router.message(F.text == "📋 Начать подсчёт остатков")
async def start_inventory(message: Message, state: FSMContext):
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
        f"<b>{data['category']}</b>\n"
        f"Позиция {idx + 1} из {len(items)}:\n\n"
        f"👉 <b>{item}</b>\n\n"
        f"Введи количество или нажми «Пропустить»:",
        parse_mode="HTML",
        reply_markup=quantity_kb()
    )


@router.message(InventoryStates.entering_quantity, F.text == "⏭ Пропустить (0)")
async def skip_item(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data["items"]
    idx = data["current_index"]
    quantities = data.get("quantities", {})
    quantities[items[idx]] = "0"
    await state.update_data(quantities=quantities, current_index=idx + 1)
    await ask_next_item(message, state)


@router.message(InventoryStates.entering_quantity)
async def quantity_entered(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer("Введи количество или нажми «Пропустить».")
        return

    data = await state.get_data()
    items = data["items"]
    idx = data["current_index"]
    quantities = data.get("quantities", {})
    quantities[items[idx]] = text
    await state.update_data(quantities=quantities, current_index=idx + 1)
    await ask_next_item(message, state)


async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    lines = [f"📋 <b>Отчёт: {data['category']}</b>\n"]
    for item, qty in data.get("quantities", {}).items():
        lines.append(f"• {item}: <b>{qty}</b>")
    lines.append("\nВсё верно? Отправляем отчёт?")
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
        user.id, user.username, user.full_name or "",
        data["category"], data.get("quantities", {})
    )

    lines = [
        f"📥 <b>Новый отчёт #{report_id}</b>",
        f"Категория: <b>{data['category']}</b>",
        f"От: {user.full_name or 'Без имени'}" + (f" (@{user.username})" if user.username else ""),
        f"ID: <code>{user.id}</code>\n"
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
    await message.answer(
        f"✅ Отчёт #{report_id} отправлен! Спасибо 🙌",
        reply_markup=main_menu(is_admin(user))
    )


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


@router.message(F.text == "➕ Добавить позицию")
async def start_add_item(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    cats = await get_categories()
    await state.set_state(AdminStates.waiting_add_item)
    await message.answer("Выбери категорию:", reply_markup=categories_inline(cats, "addcat"))


@router.callback_query(F.data.startswith("addcat:"))
async def add_item_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Нет доступа")
        return
    cat = callback.data.split(":", 1)[1]
    await state.update_data(add_category=cat)
    await callback.message.answer(
        f"Категория: <b>{cat}</b>\n\nВведи название новой позиции:",
        parse_mode="HTML", reply_markup=cancel_kb()
    )
    await callback.answer()


@router.message(AdminStates.waiting_add_item, F.text == "❌ Отмена")
async def cancel_add(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_add_item)
async def process_add_item(message: Message, state: FSMContext):
    data = await state.get_data()
    cat = data.get("add_category")
    name = message.text.strip()
    if not name or not cat:
        await message.answer("Ошибка. Попробуй снова.")
        return
    ok = await add_item(cat, name)
    await state.clear()
    await message.answer(
        f"✅ «{name}» добавлена." if ok else "Не удалось добавить (уже есть?).",
        reply_markup=admin_menu()
    )


@router.message(F.text == "🗑 Удалить позицию")
async def start_del_item(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    cats = await get_categories()
    await state.set_state(AdminStates.waiting_del_item)
    await message.answer("Выбери категорию:", reply_markup=categories_inline(cats, "delcat"))


@router.callback_query(F.data.startswith("delcat:"))
async def del_item_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Нет доступа")
        return
    cat = callback.data.split(":", 1)[1]
    items = await get_items_by_category(cat)
    if not items:
        await callback.message.answer("Пусто.")
        await callback.answer()
        return
    await state.update_data(del_category=cat)
    text = f"Категория: <b>{cat}</b>\n\nВведи точное название для удаления:\n\n" + "\n".join(f"• {i}" for i in items)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cancel_kb())
    await callback.answer()


@router.message(AdminStates.waiting_del_item, F.text == "❌ Отмена")
async def cancel_del(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_del_item)
async def process_del_item(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await deactivate_item(data.get("del_category"), message.text.strip())
    await state.clear()
    await message.answer(
        "✅ Удалено." if ok else "Не найдено.",
        reply_markup=admin_menu()
    )


@router.message(F.text == "✏️ Переименовать позицию")
async def start_rename(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    cats = await get_categories()
    await state.set_state(AdminStates.waiting_rename_old)
    await message.answer("Выбери категорию:", reply_markup=categories_inline(cats, "rencat"))


@router.callback_query(F.data.startswith("rencat:"))
async def rename_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        await callback.answer("Нет доступа")
        return
    cat = callback.data.split(":", 1)[1]
    items = await get_items_by_category(cat)
    await state.update_data(ren_category=cat)
    text = f"Категория: <b>{cat}</b>\n\nВведи <b>старое</b> название:\n\n" + "\n".join(f"• {i}" for i in items)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=cancel_kb())
    await callback.answer()


@router.message(AdminStates.waiting_rename_old, F.text == "❌ Отмена")
@router.message(AdminStates.waiting_rename_new, F.text == "❌ Отмена")
async def cancel_rename(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_rename_old)
async def process_rename_old(message: Message, state: FSMContext):
    await state.update_data(old_name=message.text.strip())
    await state.set_state(AdminStates.waiting_rename_new)
    await message.answer("Теперь введи <b>новое</b> название:", parse_mode="HTML")


@router.message(AdminStates.waiting_rename_new)
async def process_rename_new(message: Message, state: FSMContext):
    data = await state.get_data()
    ok = await rename_item(data.get("ren_category"), data.get("old_name"), message.text.strip())
    await state.clear()
    await message.answer(
        f"✅ Переименовано." if ok else "Не удалось.",
        reply_markup=admin_menu()
    )


@router.message(F.text == "📊 Последние отчёты")
async def last_reports(message: Message):
    if not is_admin(message.from_user):
        return
    reports = await get_last_reports(15)
    if not reports:
        await message.answer("Отчётов пока нет.")
        return
    lines = ["📊 <b>Последние отчёты:</b>\n"]
    for r in reports:
        uname = f"@{r['username']}" if r["username"] else r["full_name"]
        lines.append(f"#{r['id']} | {r['category'][:22]} | {uname} | {r['created_at'][:16]}")
    lines.append("\nДетали: /report ID")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("report"))
async def show_report(message: Message):
    if not is_admin(message.from_user):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /report 123")
        return
    report, items = await get_report_details(int(parts[1]))
    if not report:
        await message.answer("Не найден.")
        return
    lines = [
        f"📄 <b>Отчёт #{report['id']}</b>",
        f"Категория: {report['category']}",
        f"От: {report['full_name']} (@{report['username'] or '—'})",
        f"Время: {report['created_at']}\n"
    ]
    for it in items:
        lines.append(f"• {it['item_name']}: <b>{it['quantity']}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text == "⏰ Настройки напоминаний")
async def reminder_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    settings = await get_reminder_settings()
    text = (
        "⏰ <b>Настройки напоминаний</b>\n\n"
        f"<b>Частые</b> (Овощи, Лаваш, Мясо-группа):\n"
        f"  Дни: <code>{settings.get('frequent', {}).get('days', '—')}</code>\n"
        f"  Время: <code>{settings.get('frequent', {}).get('time', '—')}</code>\n\n"
        f"<b>Раз в неделю</b> (Лимонады, Упаковка):\n"
        f"  Дни: <code>{settings.get('weekly', {}).get('days', '—')}</code>\n"
        f"  Время: <code>{settings.get('weekly', {}).get('time', '—')}</code>\n\n"
        "Изменить:\n"
        "<code>frequent tue,fri 10:00</code>\n"
        "<code>weekly mon 09:30</code>\n"
        "<code>frequent off</code>\n\n"
        "Дни: mon tue wed thu fri sat sun"
    )
    await state.set_state(AdminStates.waiting_reminder)
    await message.answer(text, parse_mode="HTML", reply_markup=cancel_kb())


@router.message(AdminStates.waiting_reminder, F.text == "❌ Отмена")
async def cancel_reminder(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_menu())


@router.message(AdminStates.waiting_reminder)
async def process_reminder(message: Message, state: FSMContext):
    parts = message.text.strip().lower().split()
    if len(parts) < 2:
        await message.answer("Формат: frequent tue,fri 10:00")
        return
    group = parts[0]
    if group not in ("frequent", "weekly"):
        await message.answer("Только frequent или weekly")
        return
    if parts[1] == "off":
        await update_reminder(group, "mon", "10:00", 0)
        await state.clear()
        await message.answer(f"✅ {group} выключены.", reply_markup=admin_menu())
        return
    if len(parts) < 3:
        await message.answer("Нужны дни и время")
        return
    await update_reminder(group, parts[1], parts[2], 1)
    await state.clear()
    await message.answer(f"✅ {group} обновлены.", reply_markup=admin_menu())


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
        except Exception:
            pass


def setup_scheduler():
    scheduler.remove_all_jobs()

    async def _schedule():
        settings = await get_reminder_settings()
        for group_key, conf in settings.items():
            if not conf.get("enabled"):
                continue
            days_str = conf.get("days", "")
            time_str = conf.get("time", "10:00")
            try:
                hour, minute = map(int, time_str.split(":"))
            except Exception:
                hour, minute = 10, 0
            days = [d.strip() for d in days_str.split(",") if d.strip()]
            if not days:
                continue
            trigger = CronTrigger(day_of_week=",".join(days), hour=hour, minute=minute, timezone="Europe/Moscow")
            scheduler.add_job(send_reminder, trigger=trigger, args=[group_key], id=f"rem_{group_key}", replace_existing=True)
            logging.info(f"Reminder {group_key}: {days} at {time_str}")

    asyncio.create_task(_schedule())


async def on_startup():
    await init_db()
    setup_scheduler()
    if not scheduler.running:
        scheduler.start()
    logging.info("Bot started")


async def main():
    logging.basicConfig(level=logging.INFO)
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())