import asyncio
import logging
import os

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
DB_PATH: str = os.getenv("DB_PATH", "complaints.db")
LOG_CHAT_ID: int = int(os.getenv("LOG_CHAT_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

router = Router()


# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class ComplaintForm(StatesGroup):
    fio = State()
    officer_info = State()
    violation = State()
    media = State()


class EmployeeRegisterForm(StatesGroup):
    fio = State()
    position = State()
    rank = State()
    nickname = State()


class AddEmployeeForm(StatesGroup):
    username = State()


class RejectForm(StatesGroup):
    reason = State()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                username      TEXT,
                fio           TEXT NOT NULL,
                officer_info  TEXT NOT NULL,
                violation     TEXT NOT NULL,
                media_file_id TEXT,
                media_type    TEXT,
                status        TEXT DEFAULT 'pending',
                accepted_by   INTEGER,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                user_id    INTEGER,
                username   TEXT UNIQUE NOT NULL,
                fio        TEXT,
                position   TEXT,
                rank       TEXT,
                nickname   TEXT,
                registered INTEGER DEFAULT 0,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaint_messages (
                complaint_id INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                message_id   INTEGER NOT NULL
            )
        """)
        # Migrations for existing databases
        for col_sql in [
            "ALTER TABLE complaints ADD COLUMN officer_info TEXT NOT NULL DEFAULT '—'",
            "ALTER TABLE complaints ADD COLUMN accepted_by INTEGER",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass
        await db.commit()


async def is_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone() is not None


async def is_registered_employee(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM employees WHERE user_id=? AND registered=1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def is_staff(user_id: int) -> bool:
    return user_id == ADMIN_ID or await is_registered_employee(user_id)


async def get_all_recipient_ids(db) -> list[int]:
    """Admin + all registered employees."""
    ids = [ADMIN_ID]
    async with db.execute("SELECT user_id FROM employees WHERE registered=1 AND user_id IS NOT NULL") as cur:
        rows = await cur.fetchall()
    ids.extend(r[0] for r in rows if r[0])
    return ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def complaint_keyboard(complaint_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять",       callback_data=f"accept_{complaint_id}"),
        InlineKeyboardButton(text="❌ Отклонить",     callback_data=f"reject_{complaint_id}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{complaint_id}"),
    ]])


def build_complaint_text(complaint_id, uname, user_id, fio, officer_info, violation) -> str:
    return (
        f"📨 <b>Новая жалоба #{complaint_id}</b>\n\n"
        f"👤 <b>От:</b> {uname} (ID: <code>{user_id}</code>)\n"
        f"📋 <b>ФИО заявителя:</b> {fio}\n"
        f"👮 <b>Сотрудник / жетон:</b> {officer_info}\n"
        f"⚠️ <b>Нарушение:</b> {violation}"
    )


async def send_complaint_to_all(bot: Bot, complaint_id: int, text: str,
                                 media_file_id: str | None, media_type: str | None,
                                 recipients: list[int]) -> None:
    keyboard = complaint_keyboard(complaint_id)
    msg_rows = []
    for rid in recipients:
        try:
            if media_file_id:
                send_fn = {
                    "photo": bot.send_photo,
                    "video": bot.send_video,
                    "document": bot.send_document,
                }.get(media_type, bot.send_document)
                sent = await send_fn(rid, media_file_id, caption=text, parse_mode="HTML", reply_markup=keyboard)
            else:
                sent = await bot.send_message(rid, text, parse_mode="HTML", reply_markup=keyboard)
            msg_rows.append((complaint_id, rid, sent.message_id))
        except Exception as e:
            logger.warning("Could not send complaint to %s: %s", rid, e)

    if msg_rows:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "INSERT INTO complaint_messages (complaint_id, chat_id, message_id) VALUES (?,?,?)",
                msg_rows,
            )
            await db.commit()


async def invalidate_complaint_messages(bot: Bot, complaint_id: int) -> None:
    """Remove inline keyboards from all complaint notification messages."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id, message_id FROM complaint_messages WHERE complaint_id=?",
            (complaint_id,),
        ) as cur:
            rows = await cur.fetchall()
    for chat_id, message_id in rows:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    username = (message.from_user.username or "").lower()

    if uid == ADMIN_ID:
        await message.answer(
            "👮 <b>Добро пожаловать, Администратор!</b>\n\n"
            "Команды:\n"
            "/add_employee — добавить сотрудника\n"
            "/staff — список сотрудников\n"
            "/blocked — заблокированные пользователи\n"
            "/complaints — активные жалобы",
            parse_mode="HTML",
        )
        return

    if await is_blocked(uid):
        await message.answer("❌ Вы заблокированы и не можете использовать этого бота.")
        return

    # Auto-link employee by username on first /start
    if username:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT user_id, registered FROM employees WHERE username=?", (username,)
            ) as cur:
                row = await cur.fetchone()
        if row:
            emp_uid, registered = row
            if not emp_uid:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE employees SET user_id=? WHERE username=?", (uid, username))
                    await db.commit()
            if not registered:
                await message.answer(
                    "👋 Вы добавлены как сотрудник ОСБ ГАИ.\n"
                    "Пройдите регистрацию командой /register"
                )
            else:
                await message.answer(
                    "👮 <b>Добро пожаловать, сотрудник!</b>\n\n"
                    "Команды:\n"
                    "/complaints — активные жалобы\n"
                    "/register — пройти регистрацию заново",
                    parse_mode="HTML",
                )
            return

    await message.answer(
        "👋 Добро пожаловать в <b>Веб-приёмную жалоб ОСБ ГАИ</b>!\n\n"
        "Используйте /complaint чтобы подать жалобу на сотрудника.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /register  (employees)
# ---------------------------------------------------------------------------

@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    username = (message.from_user.username or "").lower()

    if uid == ADMIN_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM employees WHERE username=? OR user_id=?", (username, uid)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await message.answer("❌ Вы не добавлены как сотрудник. Обратитесь к администратору.")
        return

    if username:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE employees SET user_id=? WHERE username=? AND (user_id IS NULL OR user_id=0)",
                (uid, username),
            )
            await db.commit()

    await state.set_state(EmployeeRegisterForm.fio)
    await message.answer(
        "📝 <b>Регистрация сотрудника</b>\n\nШаг 1/4: Введите ваше ФИО:",
        parse_mode="HTML",
    )


@router.message(EmployeeRegisterForm.fio)
async def reg_fio(message: Message, state: FSMContext) -> None:
    await state.update_data(fio=message.text)
    await state.set_state(EmployeeRegisterForm.position)
    await message.answer("Шаг 2/4: Введите вашу должность:")


@router.message(EmployeeRegisterForm.position)
async def reg_position(message: Message, state: FSMContext) -> None:
    await state.update_data(position=message.text)
    await state.set_state(EmployeeRegisterForm.rank)
    await message.answer("Шаг 3/4: Введите ваше звание:")


@router.message(EmployeeRegisterForm.rank)
async def reg_rank(message: Message, state: FSMContext) -> None:
    await state.update_data(rank=message.text)
    await state.set_state(EmployeeRegisterForm.nickname)
    await message.answer("Шаг 4/4: Введите ваш никнейм (как вас называть):")


@router.message(EmployeeRegisterForm.nickname)
async def reg_nickname(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    username = (message.from_user.username or "").lower()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE employees SET fio=?, position=?, rank=?, nickname=?, registered=1, user_id=?"
            " WHERE username=? OR user_id=?",
            (data["fio"], data["position"], data["rank"], message.text, uid, username, uid),
        )
        await db.commit()

    await message.answer(
        f"✅ <b>Регистрация завершена!</b>\n\n"
        f"👤 ФИО: {data['fio']}\n"
        f"🏷 Должность: {data['position']}\n"
        f"⭐ Звание: {data['rank']}\n"
        f"📛 Никнейм: {message.text}\n\n"
        "Жалобы будут поступать к вам автоматически.\n"
        "Команды:\n/complaints — активные жалобы",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Admin: /add_employee
# ---------------------------------------------------------------------------

@router.message(Command("add_employee"))
async def cmd_add_employee(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddEmployeeForm.username)
    await message.answer("Введите Telegram username сотрудника (с @ или без):")


@router.message(AddEmployeeForm.username)
async def process_add_employee(message: Message, state: FSMContext) -> None:
    await state.clear()
    username = message.text.lstrip("@").lower().strip()
    if not username:
        await message.answer("❌ Некорректный username.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM employees WHERE username=?", (username,)) as cur:
            exists = await cur.fetchone()
        if exists:
            await message.answer(f"⚠️ Сотрудник @{username} уже добавлен.")
            return
        await db.execute("INSERT INTO employees (username) VALUES (?)", (username,))
        await db.commit()

    await message.answer(
        f"✅ Сотрудник @{username} добавлен.\n"
        "Когда он запустит бота и пройдёт /register, он сможет работать."
    )


# ---------------------------------------------------------------------------
# Admin: /staff
# ---------------------------------------------------------------------------

@router.message(Command("staff"))
async def cmd_staff(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, fio, position, rank, nickname, registered FROM employees ORDER BY added_at DESC"
        ) as cur:
            employees = await cur.fetchall()

    if not employees:
        await message.answer("📋 Список сотрудников пуст. Используйте /add_employee")
        return

    await message.answer(f"👥 <b>Сотрудники ({len(employees)}):</b>", parse_mode="HTML")
    for emp in employees:
        emp_uid, username, fio, position, rank, nickname, registered = emp
        status = "✅ Зарегистрирован" if registered else "⏳ Ожидает регистрации"
        text = (
            f"@{username}\n"
            f"📋 ФИО: {fio or '—'}\n"
            f"🏷 Должность: {position or '—'}\n"
            f"⭐ Звание: {rank or '—'}\n"
            f"📛 Никнейм: {nickname or '—'}\n"
            f"Статус: {status}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"demp_{username}"),
        ]])
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("demp_"))
async def delete_employee(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    username = callback.data[5:]  # strip "demp_"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM employees WHERE username=?", (username,))
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🗑 Сотрудник @{username} удалён.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Admin: /blocked  with unblock
# ---------------------------------------------------------------------------

@router.message(Command("blocked"))
async def cmd_blocked(message: Message) -> None:
    if message.from_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, blocked_at FROM blocked_users ORDER BY blocked_at DESC"
        ) as cur:
            users = await cur.fetchall()

    if not users:
        await message.answer("📋 Список заблокированных пользователей пуст.")
        return

    await message.answer("🚫 <b>Заблокированные пользователи:</b>", parse_mode="HTML")
    for user_id, username, blocked_at in users:
        uname = f"@{username}" if username else f"ID: {user_id}"
        text = f"<code>{user_id}</code> ({uname})\n🕐 {str(blocked_at)[:16]}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔓 Разблокировать", callback_data=f"unblock_{user_id}"),
        ]])
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("unblock_"))
async def unblock_user(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    user_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"🔓 Пользователь <code>{user_id}</code> разблокирован.", parse_mode="HTML"
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# /complaint  (users)
# ---------------------------------------------------------------------------

@router.message(Command("complaint"))
async def cmd_complaint(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    if await is_blocked(uid):
        await message.answer("❌ Вы заблокированы и не можете использовать этого бота.")
        return
    await state.set_state(ComplaintForm.fio)
    await message.answer(
        "📝 <b>Подача жалобы</b>\n\nШаг 1/4: Введите ваше ФИО ((Никнейм)):",
        parse_mode="HTML",
    )


@router.message(ComplaintForm.fio)
async def process_fio(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return
    await state.update_data(fio=message.text)
    await state.set_state(ComplaintForm.officer_info)
    await message.answer("Шаг 2/4: Введите ФИО ((Никнейм)) или номер жетона ((Номер маски)) сотрудника, совершившего нарушение:")


@router.message(ComplaintForm.officer_info)
async def process_officer_info(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return
    await state.update_data(officer_info=message.text)
    await state.set_state(ComplaintForm.violation)
    await message.answer("Шаг 3/4: Опишите, что нарушил сотрудник:")


@router.message(ComplaintForm.violation)
async def process_violation(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return
    await state.update_data(violation=message.text)
    await state.set_state(ComplaintForm.media)
    await message.answer(
        "Шаг 4/4: Прикрепите фото или видео в качестве доказательства\n"
        "((Разрешение минимум 720p, должно быть видно Ваш никнейм и никнейм сотрудника, дату и время.))\n"
        "(или /skip чтобы пропустить):"
    )


@router.message(ComplaintForm.media, Command("skip"))
async def skip_media(message: Message, state: FSMContext) -> None:
    await _submit_complaint(message, state, None, None)


@router.message(ComplaintForm.media, F.photo | F.video | F.document)
async def process_media(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return
    if message.photo:
        fid, ftype = message.photo[-1].file_id, "photo"
    elif message.video:
        fid, ftype = message.video.file_id, "video"
    else:
        fid, ftype = message.document.file_id, "document"
    await _submit_complaint(message, state, fid, ftype)


async def _submit_complaint(
    message: Message, state: FSMContext,
    media_file_id: str | None, media_type: str | None,
) -> None:
    data = await state.get_data()
    await state.clear()

    uid = message.from_user.id
    username = message.from_user.username
    fio = data.get("fio", "")
    officer_info = data.get("officer_info", "")
    violation = data.get("violation", "")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO complaints (user_id, username, fio, officer_info, violation, media_file_id, media_type)"
            " VALUES (?,?,?,?,?,?,?)",
            (uid, username, fio, officer_info, violation, media_file_id, media_type),
        )
        complaint_id = cur.lastrowid
        await db.commit()
        recipients = await get_all_recipient_ids(db)

    await message.answer(f"✅ Ваша жалоба №{complaint_id} успешно отправлена на рассмотрение.")

    uname = f"@{username}" if username else "без username"
    text = build_complaint_text(complaint_id, uname, uid, fio, officer_info, violation)
    await send_complaint_to_all(message.bot, complaint_id, text, media_file_id, media_type, recipients)


# ---------------------------------------------------------------------------
# /complaints  (admin + employees)
# ---------------------------------------------------------------------------

@router.message(Command("complaints"))
async def cmd_complaints(message: Message) -> None:
    uid = message.from_user.id
    if not await is_staff(uid):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, username, fio, officer_info, violation, media_file_id, media_type"
            " FROM complaints WHERE status='pending' ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await message.answer("📋 Нет активных жалоб.")
        return

    await message.answer(f"📋 <b>Активные жалобы ({len(rows)}):</b>", parse_mode="HTML")
    bot: Bot = message.bot
    for row in rows:
        cid, user_id, username, fio, officer_info, violation, fid, ftype = row
        uname = f"@{username}" if username else "без username"
        text = build_complaint_text(cid, uname, user_id, fio, officer_info, violation)
        keyboard = complaint_keyboard(cid)
        try:
            if fid:
                send_fn = {
                    "photo": bot.send_photo,
                    "video": bot.send_video,
                    "document": bot.send_document,
                }.get(ftype, bot.send_document)
                await send_fn(message.chat.id, fid, caption=text, parse_mode="HTML", reply_markup=keyboard)
            else:
                await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.warning("Error sending complaint %s: %s", cid, e)


# ---------------------------------------------------------------------------
# Callback: accept
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("accept_"))
async def accept_complaint(callback: CallbackQuery) -> None:
    if not await is_staff(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    complaint_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, status FROM complaints WHERE id=?", (complaint_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await callback.answer("Жалоба не найдена.", show_alert=True)
            return
        user_id, status = row
        if status != "pending":
            await callback.answer("Эта жалоба уже обработана.", show_alert=True)
            return
        await db.execute(
            "UPDATE complaints SET status='accepted', accepted_by=? WHERE id=?",
            (callback.from_user.id, complaint_id),
        )
        await db.commit()

    try:
        await callback.bot.send_message(user_id, f"✅ Ваша жалоба №{complaint_id} принята.")
    except Exception as e:
        logger.warning("Could not notify user %s: %s", user_id, e)

    await invalidate_complaint_messages(callback.bot, complaint_id)
    actor = callback.from_user.username or str(callback.from_user.id)
    await callback.message.reply(f"✅ Жалоба #{complaint_id} принята (@{actor}). Пользователь уведомлён.")
    await log_complaint_to_group(callback.bot, complaint_id, "принята",
                                  callback.from_user.id, callback.from_user.username)
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: block
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("block_"))
async def block_user_callback(callback: CallbackQuery) -> None:
    if not await is_staff(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    complaint_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, status FROM complaints WHERE id=?", (complaint_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await callback.answer("Жалоба не найдена.", show_alert=True)
            return
        user_id, username, status = row
        if status != "pending":
            await callback.answer("Эта жалоба уже обработана.", show_alert=True)
            return
        await db.execute(
            "INSERT OR IGNORE INTO blocked_users (user_id, username) VALUES (?,?)",
            (user_id, username),
        )
        await db.execute(
            "UPDATE complaints SET status='blocked', accepted_by=? WHERE id=?",
            (callback.from_user.id, complaint_id),
        )
        await db.commit()

    uname = f"@{username}" if username else f"ID: {user_id}"
    await invalidate_complaint_messages(callback.bot, complaint_id)
    actor = callback.from_user.username or str(callback.from_user.id)
    await callback.message.reply(f"🚫 Пользователь {uname} заблокирован (@{actor}).")
    await callback.answer()


# ---------------------------------------------------------------------------
# Callback: reject  (ask for reason)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("reject_"))
async def reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_staff(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    complaint_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT status FROM complaints WHERE id=?", (complaint_id,)) as cur:
            row = await cur.fetchone()

    if not row:
        await callback.answer("Жалоба не найдена.", show_alert=True)
        return
    if row[0] != "pending":
        await callback.answer("Эта жалоба уже обработана.", show_alert=True)
        return

    await state.set_state(RejectForm.reason)
    await state.update_data(complaint_id=complaint_id)
    await callback.message.reply(f"✍️ Введите причину отклонения жалобы #{complaint_id}:")
    await callback.answer()


@router.message(RejectForm.reason)
async def reject_reason(message: Message, state: FSMContext) -> None:
    if not await is_staff(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    complaint_id = data.get("complaint_id")
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, status FROM complaints WHERE id=?", (complaint_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await message.answer("❌ Жалоба не найдена.")
            return
        user_id, status = row
        if status != "pending":
            await message.answer("⚠️ Эта жалоба уже обработана.")
            return
        await db.execute(
            "UPDATE complaints SET status='rejected', accepted_by=? WHERE id=?",
            (message.from_user.id, complaint_id),
        )
        await db.commit()

    try:
        await message.bot.send_message(
            user_id,
            f"❌ Ваша жалоба №{complaint_id} отклонена.\n\n📝 <b>Причина:</b> {message.text}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Could not notify user %s: %s", user_id, e)

    await invalidate_complaint_messages(message.bot, complaint_id)
    actor = message.from_user.username or str(message.from_user.id)
    await message.answer(f"❌ Жалоба #{complaint_id} отклонена (@{actor}). Пользователь уведомлён.")
    await log_complaint_to_group(message.bot, complaint_id, "отклонена",
                                  message.from_user.id, message.from_user.username,
                                  reason=message.text)


# ---------------------------------------------------------------------------
# Group logging
# ---------------------------------------------------------------------------

async def log_complaint_to_group(
    bot: Bot,
    complaint_id: int,
    action: str,          # "принята" | "отклонена"
    actor_id: int,
    actor_username: str | None,
    reason: str | None = None,
) -> None:
    if not LOG_CHAT_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, fio, officer_info, violation, media_file_id, media_type"
            " FROM complaints WHERE id=?", (complaint_id,)
        ) as cur:
            c = await cur.fetchone()
        async with db.execute(
            "SELECT fio, position, rank, nickname FROM employees WHERE user_id=?", (actor_id,)
        ) as cur:
            emp = await cur.fetchone()

    if not c:
        return

    user_id, username, fio, officer_info, violation, media_file_id, media_type = c
    uname = f"@{username}" if username else f"ID: {user_id}"
    actor_uname = f"@{actor_username}" if actor_username else f"ID: {actor_id}"
    action_emoji = "✅" if action == "принята" else "❌"

    # Message 1: complaint card
    complaint_text = (
        f"{action_emoji} <b>Жалоба №{complaint_id} {action}</b> ({actor_uname})\n\n"
        f"👤 <b>От:</b> {uname}\n"
        f"📋 <b>ФИО заявителя:</b> {fio}\n"
        f"👮 <b>Сотрудник / жетон:</b> {officer_info}\n"
        f"⚠️ <b>Нарушение:</b> {violation}"
    )
    if reason:
        complaint_text += f"\n📝 <b>Причина отказа:</b> {reason}"

    try:
        if media_file_id:
            send_fn = {
                "photo": bot.send_photo,
                "video": bot.send_video,
                "document": bot.send_document,
            }.get(media_type, bot.send_document)
            await send_fn(LOG_CHAT_ID, media_file_id, caption=complaint_text, parse_mode="HTML")
        else:
            await bot.send_message(LOG_CHAT_ID, complaint_text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Could not send complaint card to log group: %s", e)
        return

    # Message 2: staff card
    if emp:
        emp_fio, emp_position, emp_rank, emp_nickname = emp
        staff_text = (
            f"👮 <b>Карточка сотрудника</b>\n\n"
            f"📛 Никнейм: {emp_nickname or '—'}\n"
            f"📋 ФИО: {emp_fio or '—'}\n"
            f"🏷 Должность: {emp_position or '—'}\n"
            f"⭐ Звание: {emp_rank or '—'}\n"
            f"🔗 Telegram: {actor_uname}"
        )
    else:
        staff_text = (
            f"👮 <b>Карточка сотрудника</b>\n\n"
            f"🔗 Telegram: {actor_uname}\n"
            f"🆔 ID: <code>{actor_id}</code>\n"
            f"(Администратор)"
        )
    try:
        await bot.send_message(LOG_CHAT_ID, staff_text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Could not send staff card to log group: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env")
    if not ADMIN_ID:
        raise ValueError("ADMIN_ID не задан в .env")

    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен. Admin ID: %s", ADMIN_ID)
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
