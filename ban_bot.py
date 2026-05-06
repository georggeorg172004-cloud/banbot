import asyncio
import csv
import io
import logging
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Document

# ── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN = "8511607788:AAHLwDs9pQITCtuU37FwROFPsVLpyi3OX50"
ADMIN_ID   = 6771729867

CHAT_IDS = [
    -1003419518080,  # канал клуба
    -1003601055034,  # стандарт
    -1003464974443,  # чат про тс
    -1003729249728,  # чат про влт
]

DELAY_BETWEEN_USERS = 0.3   # секунд между юзерами (анти-флуд)

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── Хелпер: кик из одного чата ───────────────────────────────────────────────
async def kick_user(user_id: int, chat_id: int) -> bool:
    """ban + unban = кик без вечного бана"""
    until_date = int(datetime.now().timestamp()) + 60
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, until_date=until_date)
        return True
    except Exception as e:
        log.warning("ban %s from %s: %s", user_id, chat_id, e)
        return False


# ── Основной хендлер: документ от админа ────────────────────────────────────
@dp.message(F.document, F.from_user.id == ADMIN_ID)
async def handle_csv(message: Message, bot: Bot):
    doc: Document = message.document

    # Скачиваем файл
    file = await bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    buf.seek(0)

    # Парсим CSV (одна колонка с user_id)
    text = buf.read().decode("utf-8-sig").replace("\r", "")
    reader = csv.reader(io.StringIO(text))
    user_ids = []
    for row in reader:
        if not row:
            continue
        raw = row[0].replace("=", "").strip()
        if raw.lstrip("-").isdigit():
            user_ids.append(int(raw))

    if not user_ids:
        await message.answer("❌ Не нашёл ни одного user_id в файле.")
        return

    await message.answer(f"⏳ Начинаю кик {len(user_ids)} пользователей из {len(CHAT_IDS)} чатов...")

    results = []
    for uid in user_ids:
        kicks = await asyncio.gather(*[kick_user(uid, cid) for cid in CHAT_IDS])
        results.append({
            "user_id":     uid,
            "chat_kicks":  kicks,
            "any_success": any(kicks),
        })
        await asyncio.sleep(DELAY_BETWEEN_USERS)

    # Формируем отчёт
    total      = len(results)
    success    = sum(1 for r in results if r["any_success"])
    not_found  = total - success
    per_chat   = [sum(1 for r in results if r["chat_kicks"][i]) for i in range(len(CHAT_IDS))]

    chat_lines = "\n".join(
        f"— Чат {i+1}: {per_chat[i]} кикнуто" for i in range(len(CHAT_IDS))
    )

    report = (
        f"📊 ОТЧЕТ О КИКЕ ПОЛЬЗОВАТЕЛЕЙ\n\n"
        f"✅ Успешно кикнуто (хотя бы из 1 чата): {success}\n"
        f"❌ Не найдено ни в одном чате: {not_found}\n"
        f"📊 Всего обработано: {total}\n\n"
        f"📋 По чатам:\n{chat_lines}\n\n"
        f"⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    await message.answer(report)


# ── Прочие сообщения от не-админов ───────────────────────────────────────────
@dp.message(~(F.from_user.id == ADMIN_ID))
async def deny(message: Message):
    await message.answer("⛔ Нет доступа.")


@dp.message(F.from_user.id == ADMIN_ID)
async def admin_hint(message: Message):
    await message.answer("Пришли CSV-файл с user_id для кика.")


# ── Запуск ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
