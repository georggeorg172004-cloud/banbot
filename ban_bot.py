import asyncio
import csv
import io
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Document


def read_int_env(name: str, default: int, min_value: int, max_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < min_value:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


# ── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID   = 6771729867
KICK_ENABLED = os.getenv("KICK_ENABLED", "false").strip().lower() == "true"
MAX_USERS_PER_RUN = read_int_env("MAX_USERS_PER_RUN", default=50, min_value=1)
MAX_FILE_BYTES = read_int_env("MAX_FILE_BYTES", default=1_000_000, min_value=1)
OPERATION_TTL_SECONDS = read_int_env("OPERATION_TTL_SECONDS", default=600, min_value=1)
BAN_SECONDS = read_int_env("BAN_SECONDS", default=60, min_value=60, max_value=86_400)

CHAT_IDS = [
    -1003419518080,  # канал клуба
    -1003601055034,  # стандарт
    -1003464974443,  # чат про тс
    -1003729249728,  # чат про влт
]

DELAY_BETWEEN_USERS = 0.3   # секунд между юзерами (анти-флуд)


@dataclass
class PendingOperation:
    operation_id: str
    user_ids: list[int]
    created_at: datetime


@dataclass
class KickResult:
    user_id: int
    chat_id: int
    status: str


PENDING_OPERATIONS: dict[str, PendingOperation] = {}
LATEST_OPERATION_ID: str | None = None

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


def parse_user_ids(text: str) -> list[int]:
    reader = csv.reader(io.StringIO(text.replace("\r", "")))
    user_ids = []
    seen = set()

    for row in reader:
        if not row:
            continue

        raw = row[0].replace("=", "").strip()
        if not raw.isdigit():
            continue

        user_id = int(raw)
        if user_id <= 0 or user_id in seen:
            continue

        seen.add(user_id)
        user_ids.append(user_id)

    return user_ids


def validate_user_limit(user_ids: list[int], max_users: int = MAX_USERS_PER_RUN) -> None:
    if len(user_ids) > max_users:
        raise ValueError(f"Too many user_id: {len(user_ids)} > {max_users}")


def create_operation_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"


def is_operation_expired(operation: PendingOperation) -> bool:
    return datetime.now() - operation.created_at > timedelta(seconds=OPERATION_TTL_SECONDS)


def is_not_member_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        pattern in text
        for pattern in (
            "user not found",
            "member not found",
            "participant_id_invalid",
            "user_not_participant",
        )
    )


# ── Хелпер: кик из одного чата ───────────────────────────────────────────────
async def kick_user(user_id: int, chat_id: int, bot_client: Bot, operation_id: str) -> KickResult:
    try:
        await bot_client.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        if is_not_member_error(e):
            log.info(
                "audit operation_id=%s action=check status=not_member user_id=%s chat_id=%s",
                operation_id,
                user_id,
                chat_id,
            )
            return KickResult(user_id=user_id, chat_id=chat_id, status="not_member")

        log.warning(
            "audit operation_id=%s action=check status=error user_id=%s chat_id=%s error=%s",
            operation_id,
            user_id,
            chat_id,
            e,
        )
        return KickResult(user_id=user_id, chat_id=chat_id, status="error")

    if not KICK_ENABLED:
        log.info(
            "audit operation_id=%s action=dry-run user_id=%s chat_id=%s",
            operation_id,
            user_id,
            chat_id,
        )
        return KickResult(user_id=user_id, chat_id=chat_id, status="found")

    try:
        until_date = int(datetime.now().timestamp()) + BAN_SECONDS
        await bot_client.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=until_date,
        )
        log.info(
            "audit operation_id=%s action=ban status=success user_id=%s chat_id=%s until_date=%s",
            operation_id,
            user_id,
            chat_id,
            until_date,
        )
        return KickResult(user_id=user_id, chat_id=chat_id, status="removed")
    except Exception as e:
        log.warning(
            "audit operation_id=%s action=ban status=error user_id=%s chat_id=%s error=%s",
            operation_id,
            user_id,
            chat_id,
            e,
        )
        return KickResult(user_id=user_id, chat_id=chat_id, status="error")


async def run_operation(operation: PendingOperation, bot_client: Bot) -> str:
    results = []
    for uid in operation.user_ids:
        kicks = await asyncio.gather(
            *[
                kick_user(
                    user_id=uid,
                    chat_id=cid,
                    bot_client=bot_client,
                    operation_id=operation.operation_id,
                )
                for cid in CHAT_IDS
            ]
        )
        results.extend(kicks)
        await asyncio.sleep(DELAY_BETWEEN_USERS)

    found = sum(1 for r in results if r.status in {"found", "removed"})
    removed = sum(1 for r in results if r.status == "removed")
    not_member = sum(1 for r in results if r.status == "not_member")
    errors = sum(1 for r in results if r.status == "error")
    touched_users = len({r.user_id for r in results if r.status == "removed"})

    chat_lines = "\n".join(
        (
            f"— Чат {i+1}: "
            f"найдено {sum(1 for r in results if r.chat_id == chat_id and r.status in {'found', 'removed'})}, "
            f"удалено {sum(1 for r in results if r.chat_id == chat_id and r.status == 'removed')}, "
            f"не участники {sum(1 for r in results if r.chat_id == chat_id and r.status == 'not_member')}, "
            f"ошибок {sum(1 for r in results if r.chat_id == chat_id and r.status == 'error')}"
        )
        for i, chat_id in enumerate(CHAT_IDS)
    )

    return (
        f"📊 ОТЧЕТ О КИКЕ ПОЛЬЗОВАТЕЛЕЙ\n\n"
        f"🆔 Операция: {operation.operation_id}\n"
        f"📊 Пользователей в CSV: {len(operation.user_ids)}\n"
        f"🔎 Найдено в чатах: {found}\n"
        f"✅ Успешно удалено: {removed}\n"
        f"👤 Затронуто уникальных пользователей: {touched_users}\n"
        f"➖ Не были участниками: {not_member}\n"
        f"❌ Ошибки прав/API: {errors}\n\n"
        f"📋 По чатам:\n{chat_lines}\n\n"
        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )


# ── Основной хендлер: документ от админа ────────────────────────────────────
@dp.message(F.chat.type == "private", F.document, F.from_user.id == ADMIN_ID)
async def handle_csv(message: Message, bot: Bot):
    global LATEST_OPERATION_ID

    doc: Document = message.document

    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer(
            f"❌ Файл слишком большой: {doc.file_size} байт. Лимит: {MAX_FILE_BYTES} байт."
        )
        return

    # Скачиваем файл
    file = await bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    buf.seek(0)

    try:
        text = buf.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        await message.answer("❌ Не смог прочитать файл. Нужен CSV в UTF-8.")
        return

    user_ids = parse_user_ids(text)

    if not user_ids:
        await message.answer("❌ Не нашёл ни одного положительного user_id в файле.")
        return

    try:
        validate_user_limit(user_ids)
    except ValueError:
        await message.answer(
            f"❌ В файле {len(user_ids)} user_id, лимит за одну операцию: {MAX_USERS_PER_RUN}."
        )
        return

    operation = PendingOperation(
        operation_id=create_operation_id(),
        user_ids=user_ids,
        created_at=datetime.now(),
    )
    if KICK_ENABLED:
        PENDING_OPERATIONS[operation.operation_id] = operation
        LATEST_OPERATION_ID = operation.operation_id

    mode = "РЕАЛЬНЫЙ КИК ВКЛЮЧЕН" if KICK_ENABLED else "ТЕСТОВЫЙ РЕЖИМ"
    action = (
        f"Удалить {len(user_ids)} участников из {len(CHAT_IDS)} чатов?\n"
        f"Напиши yes для запуска или no для отмены."
        if KICK_ENABLED
        else "Реального удаления нет. Для включения нужен KICK_ENABLED=true в Railway."
    )

    await message.answer(
        f"⚠️ {mode}\n\n"
        f"🆔 Операция: {operation.operation_id}\n"
        f"Пользователей в CSV: {len(user_ids)}\n"
        f"Чатов в настройке: {len(CHAT_IDS)}\n\n"
        f"{action}"
    )


@dp.message(F.chat.type == "private", F.text, F.from_user.id == ADMIN_ID)
async def confirm_operation(message: Message, bot: Bot):
    global LATEST_OPERATION_ID

    text = (message.text or "").strip().lower()
    if text not in {"yes", "no"}:
        await message.answer("Пришли CSV-файл с user_id для кика.")
        return

    if LATEST_OPERATION_ID is None:
        await message.answer("❌ Нет операции для подтверждения. Пришли CSV заново.")
        return

    operation = PENDING_OPERATIONS.get(LATEST_OPERATION_ID)
    if operation is None:
        LATEST_OPERATION_ID = None
        await message.answer("❌ Операция не найдена или бот был перезапущен.")
        return

    if text == "no":
        PENDING_OPERATIONS.pop(operation.operation_id, None)
        LATEST_OPERATION_ID = None
        await message.answer("✅ Операция отменена.")
        return

    if is_operation_expired(operation):
        PENDING_OPERATIONS.pop(operation.operation_id, None)
        LATEST_OPERATION_ID = None
        await message.answer("❌ Операция устарела. Пришли CSV заново.")
        return

    if not KICK_ENABLED:
        await message.answer("🧪 Реальный кик отключен: KICK_ENABLED не равен true.")
        return

    PENDING_OPERATIONS.pop(operation.operation_id, None)
    LATEST_OPERATION_ID = None
    await message.answer(f"⏳ Подтверждено. Начинаю кик операции {operation.operation_id}...")
    await message.answer(await run_operation(operation, bot))


# ── Прочие сообщения от не-админов ───────────────────────────────────────────
@dp.message(F.chat.type == "private", ~(F.from_user.id == ADMIN_ID))
async def deny(message: Message):
    await message.answer("⛔ Нет доступа.")


@dp.message(F.chat.type == "private", F.from_user.id == ADMIN_ID)
async def admin_hint(message: Message):
    await message.answer("Пришли CSV-файл с user_id для кика.")


# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
