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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()


class ComplaintForm(StatesGroup):
    fio = State()
    violation = State()
    media = State()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                fio         TEXT NOT NULL,
                violation   TEXT NOT NULL,
                media_file_id TEXT,
                media_type  TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def is_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👮 Добро пожаловать, Администратор!\n\n"
            "Жалобы от пользователей будут поступать сюда автоматически.\n"
            "/blocked — список заблокированных пользователей"
        )
        return

    if await is_blocked(message.from_user.id):
        await message.answer("❌ Вы заблокированы и не можете использовать этого бота.")
        return

    await message.answer(
        "👋 Добро пожаловать в Веб-приёмную жалоб ОСБ ГАИ!\n\n"
        "Используйте /complaint чтобы подать жалобу на сотрудника."
    )


# ---------------------------------------------------------------------------
# /blocked  (admin only)
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

    lines = ["🚫 <b>Заблокированные пользователи:</b>\n"]
    for user_id, username, blocked_at in users:
        uname = f"@{username}" if username else "без username"
        lines.append(f"• <code>{user_id}</code> ({uname}) — {str(blocked_at)[:16]}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /complaint  (users)
# ---------------------------------------------------------------------------

@router.message(Command("complaint"))
async def cmd_complaint(message: Message, state: FSMContext) -> None:
    if message.from_user.id == ADMIN_ID:
        await message.answer("Вы администратор.")

    if await is_blocked(message.from_user.id):
        await message.answer("❌ Вы заблокированы и не можете использовать этого бота.")
        return

    await state.set_state(ComplaintForm.fio)
    await message.answer(
        "📝 <b>Подача жалобы</b>\n\nШаг 1/3: Введите ваше ФИО:",
        parse_mode="HTML",
    )


@router.message(ComplaintForm.fio)
async def process_fio(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return

    await state.update_data(fio=message.text)
    await state.set_state(ComplaintForm.violation)
    await message.answer("Шаг 2/3: Опишите, что нарушил сотрудник:")


@router.message(ComplaintForm.violation)
async def process_violation(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return

    await state.update_data(violation=message.text)
    await state.set_state(ComplaintForm.media)
    await message.answer(
        "Шаг 3/3: Прикрепите фото или видео в качестве доказательства\n"
        "(или отправьте /skip чтобы пропустить этот шаг):"
    )


@router.message(ComplaintForm.media, Command("skip"))
async def skip_media(message: Message, state: FSMContext) -> None:
    await _submit_complaint(message, state, media_file_id=None, media_type=None)


@router.message(ComplaintForm.media, F.photo | F.video | F.document)
async def process_media(message: Message, state: FSMContext) -> None:
    if await is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("❌ Вы заблокированы.")
        return

    if message.photo:
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_file_id = message.video.file_id
        media_type = "video"
    else:
        media_file_id = message.document.file_id
        media_type = "document"

    await _submit_complaint(message, state, media_file_id, media_type)


async def _submit_complaint(
    message: Message,
    state: FSMContext,
    media_file_id: str | None,
    media_type: str | None,
) -> None:
    data = await state.get_data()
    await state.clear()

    fio = data.get("fio", "")
    violation = data.get("violation", "")
    user_id = message.from_user.id
    username = message.from_user.username

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO complaints (user_id, username, fio, violation, media_file_id, media_type)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, fio, violation, media_file_id, media_type),
        )
        complaint_id = cur.lastrowid
        await db.commit()

    await message.answer(
        f"✅ Ваша жалоба №{complaint_id} успешно отправлена на рассмотрение."
    )

    uname = f"@{username}" if username else "без username"
    admin_text = (
        f"📨 <b>Новая жалоба #{complaint_id}</b>\n\n"
        f"👤 <b>От:</b> {uname} (ID: <code>{user_id}</code>)\n"
        f"📋 <b>ФИО заявителя:</b> {fio}\n"
        f"⚠️ <b>Нарушение:</b> {violation}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{complaint_id}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{complaint_id}"),
    ]])

    bot: Bot = message.bot
    if media_file_id:
        send = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
        }.get(media_type, bot.send_document)
        await send(ADMIN_ID, media_file_id, caption=admin_text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Admin callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("accept_"))
async def accept_complaint(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    complaint_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM complaints WHERE id = ?", (complaint_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await callback.answer("Жалоба не найдена.", show_alert=True)
            return

        user_id = row[0]
        await db.execute(
            "UPDATE complaints SET status = 'accepted' WHERE id = ?", (complaint_id,)
        )
        await db.commit()

    try:
        await callback.bot.send_message(user_id, f"✅ Ваша жалоба №{complaint_id} принята.")
    except Exception as e:
        logger.warning("Could not notify user %s: %s", user_id, e)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Жалоба #{complaint_id} принята. Пользователь уведомлён.")
    await callback.answer()


@router.callback_query(F.data.startswith("block_"))
async def block_user(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    complaint_id = int(callback.data.split("_")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username FROM complaints WHERE id = ?", (complaint_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await callback.answer("Жалоба не найдена.", show_alert=True)
            return

        user_id, username = row
        await db.execute(
            "INSERT OR IGNORE INTO blocked_users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )
        await db.execute(
            "UPDATE complaints SET status = 'blocked' WHERE id = ?", (complaint_id,)
        )
        await db.commit()

    uname = f"@{username}" if username else f"ID: {user_id}"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"🚫 Пользователь {uname} заблокирован.")
    await callback.answer()


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
