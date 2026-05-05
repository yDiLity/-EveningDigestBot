import sys
print("Python version:", sys.version, flush=True)
print("Starting script...", flush=True)

import os
import re
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

def log(msg):
    import sys
    print(f"LOG: {msg}", file=sys.stderr, flush=True)

log("Loading aiogram...")

try:
    from aiogram import Bot, Dispatcher, Router
    from aiogram.filters import Command
    from aiogram.types import Message, CallbackQuery
    from aiogram.enums import ChatType
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
    log("aiogram imported OK")
except Exception as e:
    log(f"Import error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Продолжаем
log("Continuing...")

# Читаем переменные
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
log(f"BOT_TOKEN found: {bool(BOT_TOKEN)}")

if not BOT_TOKEN:
    log("ERROR: No BOT_TOKEN!")
    sys.exit(1)

# Пробуем из os.environ напрямую (Render может не передавать в dotenv)
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.environ.get("BOT_TOKEN")
log(f"BOT_TOKEN from env: {BOT_TOKEN[:20] if BOT_TOKEN else 'NOT FOUND'}")

if not BOT_TOKEN:
    log("ERROR: BOT_TOKEN not found!")
    log(f"All env var keys: {list(os.environ.keys())[:50]}")  # Первые 50
    # Не выходим - для отладки
    BOT_TOKEN = "dummy_token"

GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID") or os.environ.get("GROUP_CHAT_ID")
if not GROUP_CHAT_ID:
    GROUP_CHAT_ID = "-1001234567890"  # дефолт
log(f"GROUP_CHAT_ID: {GROUP_CHAT_ID}")

GROUP_CHAT_ID = int(GROUP_CHAT_ID)
ADMIN_IDS_STR = os.getenv("ADMIN_IDS") or os.environ.get("ADMIN_IDS", "") or "123456789"
ADMIN_IDS = set(int(x) for x in ADMIN_IDS_STR.split(",") if x)
TIMEZONE = os.getenv("TIMEZONE") or os.environ.get("TIMEZONE", "Europe/Moscow")

log(f"GROUP_CHAT_ID: {GROUP_CHAT_ID}")
log(f"ADMIN_IDS: {ADMIN_IDS}")
log(f"TIMEZONE: {TIMEZONE}")

bot = Bot(token=BOT_TOKEN)
router = Router()
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(router)


class UserState(StatesGroup):
    waiting_for_name = State()
    waiting_for_time_vote = State()
    editing_name = State()
    editing_send_time = State()


@dataclass
class User:
    id: int
    telegram_id: int
    username: str = ""
    first_name: str = ""
    display_name: str = ""
    timezone: str = "Europe/Moscow"
    send_time: str = "21:00"
    is_active: bool = True


@dataclass
class DailyDraft:
    id: int = 0
    user_id: int = 0
    draft_date: date = None
    steps: Optional[int] = None
    water: Optional[float] = None
    pages: Optional[int] = None
    training_text: str = ""
    meals_text: str = ""
    work_text: str = ""
    other_text: str = ""
    is_submitted: bool = False
    submitted_at: datetime = None
    skipped: bool = False
    last_action_snapshot: dict = field(default_factory=dict)


users_db: dict[int, User] = {}
drafts_db: dict[int, DailyDraft] = {}
message_history: dict[int, list[dict]] = {}


def get_today() -> date:
    return date.today()


def normalize_steps(value: float, unit: str) -> int:
    if unit in ("km", "километр", "километров"):
        return int(value * 1300)
    return int(value)


def normalize_water(value: float, unit: str) -> float:
    if unit in ("стакан", "стакана", "стаканов"):
        return round(value * 0.25, 2)
    if unit in ("мл",):
        return round(value / 1000, 2)
    return round(value, 2)


def parse_metrics(text: str) -> dict:
    text_lower = text.lower()
    result = {
        "steps": None,
        "water": None,
        "pages": None,
        "training": "",
        "meals": "",
        "work": "",
        "other": ""
    }

    steps_patterns = [
        r"прошел[а]?\s+(\d+)\s*(км|километр|километров|шаг|шагов|шага)?",
        r"(\d+)\s*(км|километр|километров)\s*(шаг|шагов|шага)?",
        r"шагов[а]?\s*(\d+)",
        r"(\d+)\s*шаг",
    ]
    for pattern in steps_patterns:
        match = re.search(pattern, text_lower)
        if match:
            value = int(match.group(1))
            unit = match.group(2) if match.group(2) else "steps"
            if value < 0 or value > 40000:
                result["other"] = text
                return result
            result["steps"] = normalize_steps(value, unit)
            text_lower = text_lower.replace(match.group(0), "")
            break

    water_patterns = [
        r"выпил[а]?\s*(\d+\.?\d*)\s*(л|литр|литра|литров|мл|стакан|стакана|стаканов|кружка|кружки)?",
        r"(\d+\.?\d*)\s*(л|литр|литра|литров)\s*воды",
        r"воды[а]?\s*(\d+\.?\d*)",
    ]
    for pattern in water_patterns:
        match = re.search(pattern, text_lower)
        if match:
            value = float(match.group(1))
            unit = match.group(2) if match.group(2) else "liters"
            if value < 0 or value > 40:
                result["other"] = text
                return result
            result["water"] = normalize_water(value, unit)
            text_lower = text_lower.replace(match.group(0), "")
            break


    pages_patterns = [
        r"прочитал[а]?\s*(\d+)\s*(страниц|стр|страницы|страница|глав|главу|главы)?",
        r"(\d+)\s*(страниц|стр|страницы)",
    ]
    for pattern in pages_patterns:
        match = re.search(pattern, text_lower)
        if match:
            value = int(match.group(1))
            if value < 0 or value > 10000:
                result["other"] = text
                return result
            result["pages"] = value
            text_lower = text_lower.replace(match.group(0), "")
            break

    # Тренировки с числами: присел 15 раз, отжался 20 раз
    training_num_patterns = [
        r"присел[а]?\s*(\d+)\s*(раз|поз|повторени|повторений)",
        r"отжал[а]?[сь]?\s*(\d+)\s*(раз|поз|повторени|повторений)",
        r"подтянул[а]?[сь]?\s*(\d+)\s*(раз|поз|повторени|повторений)",
        r"пресс[а]?\s*(\d+)\s*(раз|поз|повторени|повторений)",
    ]
    for pattern in training_num_patterns:
        match = re.search(pattern, text_lower)
        if match:
            value = int(match.group(1))
            result["training"] = f"{text} ({value} раз)"
            text_lower = text_lower.replace(match.group(0), "")
            break
    
    training_keywords = ["пришел", "отжался", "жим", "подход", "тренировка", "турник", "подтянулся", "пресс", "сходил на тренировку"]
    if any(kw in text_lower for kw in training_keywords) and not result["training"]:
        result["training"] = text

    meals_keywords = ["завтрак", "обед", "ужин", "съел", "поел", "покушал", "еда", "ел"]
    if any(kw in text_lower for kw in meals_keywords):
        result["meals"] = text

    work_keywords = ["сделал", "запушил", "закончил", "отчет", "задачу", "работа", "работал", "офис"]
    if any(kw in text_lower for kw in work_keywords):
        result["work"] = text

    if not any([result["steps"], result["water"], result["pages"], result["training"], result["meals"], result["work"]]):
        result["other"] = text

    return result


def format_draft(draft: DailyDraft, user: User) -> str:
    lines = []
    if draft.meals_text:
        lines.append(f"🍳 Еда: {draft.meals_text}")
    if draft.work_text:
        lines.append(f"💼 Работа: {draft.work_text}")
    if draft.training_text:
        lines.append(f"💪 Тренировка: {draft.training_text}")
    if draft.steps is not None:
        lines.append(f"🚶‍♂️ Шаги: {draft.steps:,}")
    if draft.water is not None:
        lines.append(f"💧 Вода: {draft.water} л")
    if draft.pages is not None:
        lines.append(f"📖 Страницы: {draft.pages}")
    if draft.other_text:
        lines.append(f"📝 Другое: {draft.other_text}")

    if not lines:
        return "📋 Пока ничего не добавлено"

    display = user.display_name or user.first_name or user.username or "Участник"
    header = f"📋 Черновик для {display}:\n"
    return header + "\n".join(lines)


def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    for user in users_db.values():
        if user.telegram_id == telegram_id:
            return user
    return None


def get_draft(user_id: int, draft_date: date) -> DailyDraft:
    key = (user_id, draft_date)
    if key not in drafts_db:
        drafts_db[key] = DailyDraft(user_id=user_id, draft_date=draft_date)
    return drafts_db[key]


def get_or_create_user(telegram_id: int, first_name: str = "", username: str = "") -> User:
    user = get_user_by_telegram_id(telegram_id)
    if user:
        return user
    new_id = max(users_db.keys(), default=0) + 1
    user = User(id=new_id, telegram_id=telegram_id, first_name=first_name, username=username or "")
    users_db[new_id] = user
    return user


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    await state.update_data(user_id=user.id)
    
    await message.answer(
        f"Привет! 👋\n\n"
        f"Я — бот «Итоги дня». Буду собирать твои достижения и вечером публиковать в группу.\n\n"
        f"Давай познакомимся!\n"
        f"Как к тебе обращаться? Напиши своё имя или прозвище."
    )
    await state.set_state(UserState.waiting_for_name)


@router.message(UserState.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("user_id")
    if user_id:
        user = users_db.get(user_id)
        if user:
            user.display_name = message.text.strip()
    await state.clear()
    await message.answer(
        f"Отлично, {message.text.strip()}! ✅\n\n"
        f"Теперь ты можешь писать мне что ты сделал за день:\n"
        f"• «прошел 5000 шагов»\n"
        f"• «выпил 2 литра воды»\n"
        f"• «съел борщ на обед»\n"
        f"• «прочитал 30 страниц»\n\n"
        f"Я запомню всё и вечером опубликую в группу. В 21:00 — автоматическая отправка.\n\n"
        f"Также доступны команды:\n"
        f"/mydigest — посмотреть черновик\n"
        f"/sendnow — отправить сейчас\n"
        f"/undo — удалить последнее\n"
        f"/skip — пропустить день\n"
        f"/profile — профиль и настройки"
    )


@router.message(Command("mydigest"))
async def cmd_mydigest(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    await message.answer(format_draft(draft, user))


@router.message(Command("sendnow"))
async def cmd_sendnow(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    
    if not any([draft.steps, draft.water, draft.pages, draft.meals_text, draft.work_text, draft.training_text, draft.other_text]):
        await message.answer("Твой черновик пустой. Нечего отправлять.")
        return
    
    if draft.is_submitted:
        await message.answer("Ты уже отправил отчет за сегодня. Завтра начнем новый!")
        return
    
    draft.is_submitted = True
    draft.submitted_at = datetime.now()
    
    personal_post = format_personal_post(draft, user)
    await bot.send_message(GROUP_CHAT_ID, personal_post)
    await message.answer("✅ Отчет отправлен в группу!")


@router.message(Command("undo"))
async def cmd_undo(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    
    if not draft.last_action_snapshot:
        await message.answer("Нечего удалять — черновик пустой.")
        return
    
    draft.steps = draft.last_action_snapshot.get("steps")
    draft.water = draft.last_action_snapshot.get("water")
    draft.pages = draft.last_action_snapshot.get("pages")
    draft.training_text = draft.last_action_snapshot.get("training_text", "")
    draft.meals_text = draft.last_action_snapshot.get("meals_text", "")
    draft.work_text = draft.last_action_snapshot.get("work_text", "")
    draft.other_text = draft.last_action_snapshot.get("other_text", "")
    draft.last_action_snapshot = {}
    
    await message.answer("✅ Последнее действие удалено.\n\n" + format_draft(draft, user))


@router.message(Command("skip"))
async def cmd_skip(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    draft.skipped = True
    
    await message.answer("✅ Понял, сегодня ты пропускаешь. Завтра жду тебя!")


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    draft.steps = None
    draft.water = None
    draft.pages = None
    draft.training_text = ""
    draft.meals_text = ""
    draft.work_text = ""
    draft.other_text = ""
    draft.last_action_snapshot = {}
    draft.is_submitted = False
    draft.skipped = False
    
    await message.answer("✅ Черновик полностью очищен!")


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    today = get_today()
    steps_leaders = []
    
    for uid, draft in drafts_db.items():
        if uid[1] == today and draft.steps and draft.is_submitted:
            u = users_db.get(uid[0])
            if u:
                steps_leaders.append((u.display_name or u.first_name, draft.steps))
    
    if not steps_leaders:
        await message.answer("Пока никто не отправил отчеты с шагами.")
        return
    
    steps_leaders.sort(key=lambda x: x[1], reverse=True)
    lines = ["🏆 Лидеры по шагам за сегодня:\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, steps) in enumerate(steps_leaders[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {name} — {steps:,}")
    
    await message.answer("\n".join(lines))


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="⏰ Изменить время отправки", callback_data="edit_send_time")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    
    await message.answer(
        f"👤 Твой профиль:\n\n"
        f"Имя: {user.display_name or user.first_name or 'Не задано'}\n"
        f"Время отправки: {user.send_time}\n"
        f"Часовой пояс: {user.timezone}",
        reply_markup=keyboard
    )


@router.callback_query()
async def handle_callback(callback: CallbackQuery):
    await callback.answer()
    
    user = get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.message.answer("Ты еще не настроил бота. Нажми /start")
        return
    
    if callback.data == "edit_name":
        await callback.message.answer("✏️ Введи новое имя:")
        await callback.message.answer_state(UserState.editing_name)
    
    elif callback.data == "edit_send_time":
        await callback.message.answer("⏰ Во сколько отправлять отчёт? (например: 21:00)")
        await callback.message.answer_state(UserState.editing_send_time)
    
    elif callback.data == "back_to_menu":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="edit_name")],
            [InlineKeyboardButton(text="⏰ Изменить время отправки", callback_data="edit_send_time")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
        ])
        await callback.message.edit_text(
            f"👤 Твой профиль:\n\n"
            f"Имя: {user.display_name or user.first_name or 'Не задано'}\n"
            f"Время отправки: {user.send_time}\n"
            f"Часовой пояс: {user.timezone}"
        )
        await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.message(UserState.editing_name)
async def process_edit_name(message: Message, state: FSMContext):
    user = get_user_by_telegram_id(message.from_user.id)
    if user:
        user.display_name = message.text.strip()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="⏰ Изменить время отправки", callback_data="edit_send_time")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    
    await message.answer(
        f"✅ Имя изменено на: {message.text.strip()}\n\n"
        f"👤 Твой профиль:\n\n"
        f"Имя: {user.display_name or user.first_name or 'Не задано'}\n"
        f"В��емя отправки: {user.send_time}\n"
        f"Часовой пояс: {user.timezone}",
        reply_markup=keyboard
    )
    await state.clear()


@router.message(UserState.editing_send_time)
async def process_edit_send_time(message: Message, state: FSMContext):
    user = get_user_by_telegram_id(message.from_user.id)
    
    try:
        time_obj = datetime.strptime(message.text.strip(), "%H:%M").time()
        user.send_time = message.text.strip()
    except ValueError:
        await message.answer("Не понял формат. Напишите время в формате ЧЧ:ММ (например 21:00)")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton(text="⏰ Изменить время отправки", callback_data="edit_send_time")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")],
    ])
    
    await message.answer(
        f"✅ Время отправки изменено на: {message.text.strip()}\n\n"
        f"👤 Твой профиль:\n\n"
        f"Имя: {user.display_name or user.first_name or 'Не задано'}\n"
        f"Время отправки: {user.send_time}\n"
        f"Часовой пояс: {user.timezone}",
        reply_markup=keyboard
    )
    await state.clear()


@router.message(Command("post_all"))
async def cmd_post_all(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Эта команда только для админа.")
        return
    
    await publish_all_reports()
    await message.answer("✅ Все отчеты опубликованы!")


@router.message(Command("timezone"))
async def cmd_timezone(message: Message, state: FSMContext):
    await message.answer("Давай определим удобное время для группы!\nСколько у тебя сейчас времени? (например: 18:30)")
    await state.set_state(UserState.waiting_for_time_vote)


@router.message(UserState.waiting_for_time_vote)
async def process_time_vote(message: Message, state: FSMContext):
    try:
        user_time = datetime.strptime(message.text.strip(), "%H:%M").time()
        
        await bot.send_message(
            GROUP_CHAT_ID,
            f"⏰ Голосование за время автоматической отправки!\n\n"
            f"Участник {message.from_user.first_name} предлагает {message.text.strip()} (МСК).\n\n"
            f"Ответьте в этом чате, какое время вам удобно."
        )
        await message.answer("Спасибо! Я спросил время в группе.")
    except ValueError:
        await message.answer("Не понял формат. Напишите время в формате ЧЧ:ММ (например 18:30)")
        return
    
    await state.clear()


def format_personal_post(draft: DailyDraft, user: User) -> str:
    lines = []
    
    if draft.steps is not None:
        lines.append(f"🚶‍♂️ Шаги: {draft.steps:,}")
    if draft.water is not None:
        lines.append(f"💧 Вода: {draft.water} л")
    if draft.pages is not None:
        lines.append(f"📖 Страницы: {draft.pages}")
    if draft.training_text:
        lines.append(f"💪 Тренировка: {draft.training_text}")
    if draft.meals_text:
        lines.append(f"🍳 Еда: {draft.meals_text}")
    if draft.work_text:
        lines.append(f"💼 Работа: {draft.work_text}")
    
    time_str = draft.submitted_at.strftime("%H:%M") if draft.submitted_at else "—"
    lines.append(f"🕒 Отчитался в {time_str}")
    
    display = user.display_name or user.first_name or user.username or "Участник"
    return f"👤 {display}, твой день:\n" + "\n".join(lines)


async def publish_all_reports():
    today = get_today()
    submitted_users = []
    
    for uid, draft in drafts_db.items():
        if uid[1] == today and draft.is_submitted and not draft.skipped:
            user = users_db.get(uid[0])
            if user:
                submitted_users.append((user, draft))
    
    if not submitted_users:
        return
    
    for user, draft in submitted_users:
        personal_post = format_personal_post(draft, user)
        await bot.send_message(GROUP_CHAT_ID, personal_post)
        await asyncio.sleep(0.5)
    
    steps_data = {}
    water_data = {}
    pages_data = {}
    
    for user, draft in submitted_users:
        if draft.steps is not None:
            name = user.display_name or user.first_name or user.username or "Участник"
            steps_data[name] = draft.steps
        if draft.water is not None:
            name = user.display_name or user.first_name or user.username or "Участник"
            water_data[name] = draft.water
        if draft.pages is not None:
            name = user.display_name or user.first_name or user.username or "Участник"
            pages_data[name] = draft.pages
    
    comparison_blocks = []
    
    if len(steps_data) >= 2:
        sorted_steps = sorted(steps_data.items(), key=lambda x: x[1], reverse=True)
        block = ["🚶‍♂️ Шаги:"]
        for i, (name, steps) in enumerate(sorted_steps):
            block.append(f"{i+1}. {name} — {steps:,}")
        comparison_blocks.append("\n".join(block))
    
    if len(water_data) >= 2:
        sorted_water = sorted(water_data.items(), key=lambda x: x[1], reverse=True)
        block = ["💧 Вода:"]
        for i, (name, water) in enumerate(sorted_water):
            block.append(f"{i+1}. {name} — {water} л")
        comparison_blocks.append("\n".join(block))
    
    if len(pages_data) >= 2:
        sorted_pages = sorted(pages_data.items(), key=lambda x: x[1], reverse=True)
        block = ["📖 Страницы:"]
        for i, (name, pages) in enumerate(sorted_pages):
            block.append(f"{i+1}. {name} — {pages}")
        comparison_blocks.append("\n".join(block))
    
    if comparison_blocks:
        await bot.send_message(GROUP_CHAT_ID, "🏆 Сравнение за день:\n\n" + "\n\n".join(comparison_blocks))


async def send_reminders():
    today = get_today()
    pending_users = []
    
    for uid, draft in drafts_db.items():
        if uid[1] == today and not draft.is_submitted and not draft.skipped:
            user = users_db.get(uid[0])
            if user:
                pending_users.append(user)
    
    for user in pending_users:
        try:
            await bot.send_message(
                user.telegram_id,
                f"Напоминание! 🌟\n\n"
                f"Ты еще не отправил отчет за сегодня. Самое время вспомнить, что ты сделал!\n\n"
                f"Напиши мне: /mydigest"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание {user.telegram_id}: {e}")


async def daily_reset():
    today = get_today()
    for draft_key in list(drafts_db.keys()):
        if draft_key[1] < today:
            del drafts_db[draft_key]


@router.message()
async def handle_message(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            f"Пожалуйста, пишите мне в личные сообщения: @{(await bot.me()).username}"
        )
        return
    
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Нажми /start чтобы начать")
        return
    
    today = get_today()
    draft = get_draft(user.id, today)
    
    if draft.is_submitted:
        await message.answer("Ты уже отправил отчет за сегодня. Завтра начнем новый!")
        return
    
    parsed = parse_metrics(message.text)
    
    draft.last_action_snapshot = {
        "steps": draft.steps,
        "water": draft.water,
        "pages": draft.pages,
        "training_text": draft.training_text,
        "meals_text": draft.meals_text,
        "work_text": draft.work_text,
        "other_text": draft.other_text,
    }
    
    if parsed["steps"] is not None:
        draft.steps = parsed["steps"]
    if parsed["water"] is not None:
        draft.water = parsed["water"]
    if parsed["pages"] is not None:
        draft.pages = parsed["pages"]
    if parsed["training"]:
        draft.training_text = parsed["training"]
    if parsed["meals"]:
        draft.meals_text = parsed["meals"]
    if parsed["work"]:
        draft.work_text = parsed["work"]
    if parsed["other"]:
        draft.other_text = parsed["other"]
    
    response = f"✅ Добавлено!\n\n"
    response += format_draft(draft, user)
    response += f"\n\n🕒 Автоотправка в 21:00\n"
    response += f"/sendnow — отправить сейчас\n"
    response += f"/undo — удалить последнее"
    
    await message.answer(response)


async def scheduled_tasks():
    while True:
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        
        for user in users_db.values():
            if not user.is_active:
                continue
            
            if user.send_time == now_str:
                user_id = user.id
                today = get_today()
                draft = drafts_db.get((user_id, today))
                if draft and draft.is_submitted and not draft.skipped:
                    continue
                
                if draft and any([draft.steps, draft.water, draft.pages, draft.meals_text, draft.work_text, draft.training_text, draft.other_text]):
                    draft.is_submitted = True
                    draft.submitted_at = datetime.now()
                    personal_post = format_personal_post(draft, user)
                    await bot.send_message(GROUP_CHAT_ID, personal_post)
        
        if now.hour == 23 and now.minute == 0:
            await publish_all_reports()
        
        reminder_hour = 18
        reminder_minute = 0
        
        if now.hour == reminder_hour and now.minute == 0:
            await send_reminders()
        
        await asyncio.sleep(30)


async def keep_alive():
    """Ping self every 5 minutes to keep Glitch project awake"""
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(os.getenv("KEEP_ALIVE_URL", "https://ydilite.glitch.me")) as resp:
                    logger.info(f"Keep-alive ping: {resp.status}")
        except Exception as e:
            logger.info(f"Keep-alive: {e}")
        await asyncio.sleep(300)


import asyncio
import os

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем фоновые задачи
    asyncio.create_task(scheduled_tasks())
    
    # Простой polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    log("Starting bot...")
    try:
        loop.run_until_complete(main())
    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()