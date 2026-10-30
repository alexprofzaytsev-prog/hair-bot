"""Телеграм-бот, который автоматически отвечает клиентам.

Как он отвечает:
1. Кнопки с частыми вопросами — ответы берутся из knowledge.txt.
2. Свободный текст — если задан ANTHROPIC_API_KEY, отвечает ИИ (Claude) строго по knowledge.txt.
3. Если ответа нет — бот пересылает вопрос вам (ADMIN_ID), а вы можете ответить
   клиенту прямо из Telegram: просто сделайте «Ответить» (reply) на пересланное сообщение.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# На Bothost токен приходит в переменной BOT_TOKEN (его вводят при создании бота)
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
# Ваш Telegram ID — сюда приходят вопросы клиентов
ADMIN_ID = int(os.getenv("ADMIN_ID") or 779342813)
USE_AI = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())

KNOWLEDGE_FILE = Path(__file__).with_name("knowledge.txt")
# Положите в эту папку файлы прайс-листов (PDF, Excel и т.д.) — бот отправит их по кнопке
PRICE_DIR = Path(__file__).with_name("price")
PRICE_BUTTON = "📄 Прайс-лист"
# Здесь бот запоминает file_id каталогов в Telegram. С этим файлом сами PDF на сервере не нужны.
PRICE_IDS_FILE = Path(__file__).with_name("price_ids.json")


def load_price_ids() -> dict:
    if PRICE_IDS_FILE.exists():
        return json.loads(PRICE_IDS_FILE.read_text(encoding="utf-8"))
    return {}


price_file_ids = load_price_ids()
NO_ANSWER = "Спасибо за ваше сообщение! Я передал его менеджеру — вам ответят в ближайшее время 🙌"
CANT_ANSWER_MARK = "[НЕ ЗНАЮ]"


def load_knowledge() -> str:
    return KNOWLEDGE_FILE.read_text(encoding="utf-8")


def load_faq(text: str) -> dict:
    """Достаёт пары «Вопрос: ... / Ответ: ...» из knowledge.txt."""
    pairs = re.findall(r"Вопрос:\s*(.+?)\s*\nОтвет:\s*(.+?)(?=\n\s*\n|\nВопрос:|\Z)", text, re.S)
    return {q.strip(): a.strip() for q, a in pairs}


def faq_keyboard(faq: dict) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text=PRICE_BUTTON)]] + [[KeyboardButton(text=q)] for q in faq]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def find_by_keywords(question: str, faq: dict):
    """Простой поиск: если в сообщении есть слово из вопроса FAQ — вернуть ответ."""
    words = set(re.findall(r"\w{4,}", question.lower()))
    best, best_score = None, 0
    for q, a in faq.items():
        score = len(words & set(re.findall(r"\w{4,}", q.lower())))
        if score > best_score:
            best, best_score = a, score
    return best


claude = anthropic.AsyncAnthropic() if USE_AI else None
# Короткая история переписки с каждым клиентом, чтобы ИИ понимал контекст
history: dict = {}


async def ask_claude(user_id: int, question: str):
    system = (
        "Ты — вежливый помощник службы поддержки клиентов. Всегда обращайся к клиенту на «вы». "
        "Отвечай коротко и по-русски, только на основе информации о бизнесе ниже. Ничего не выдумывай. "
        f"Если ответа в информации нет, или клиент присылает заказ, или просит связаться с менеджером, "
        f"ответь ровно: {CANT_ANSWER_MARK}\n\n"
        f"<информация>\n{load_knowledge()}\n</информация>"
    )
    messages = history.get(user_id, []) + [{"role": "user", "content": question}]
    response = await claude.messages.create(
        model="claude-opus-5-5",
        max_tokens=2000,
        system=system,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        return None
    answer = "".join(b.text for b in response.content if b.type == "text").strip()
    if not answer or CANT_ANSWER_MARK in answer:
        return None
    history[user_id] = (messages + [{"role": "assistant", "content": answer}])[-10:]
    return answer


async def notify_admin(bot: Bot, text: str):
    """Отправляет уведомление владельцу. Ошибка здесь не должна ломать ответ клиенту."""
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as e:
        logging.error("Не удалось отправить уведомление на ADMIN_ID=%s: %s", ADMIN_ID, e)


dp = Dispatcher()


@dp.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш ID: {message.from_user.id}")


@dp.message(CommandStart())
async def start(message: Message):
    faq = load_faq(load_knowledge())
    await message.answer(
        "Здравствуйте! 👋 Я помощник магазина профессиональной косметики для волос. "
        "Выберите вопрос ниже или напишите свой.",
        reply_markup=faq_keyboard(faq),
    )


@dp.message(F.text == PRICE_BUTTON)
async def send_price(message: Message, bot: Bot):
    # Если в папке price есть файлы — отправляем их, иначе берём запомненные file_id
    files = {p.name: p for p in sorted(PRICE_DIR.glob("*")) if p.is_file() and not p.name.startswith(".")}
    names = list(files) or list(price_file_ids)
    if not names:
        await message.answer(NO_ANSWER)
        user = message.from_user
        await notify_admin(
            bot,
            f"📄 Клиент просит прайс-лист (в папке price нет файлов)\n"
            f"От: {user.full_name} (@{user.username})\nID клиента: {user.id}",
        )
        return
    await message.answer("Отправляю актуальные каталоги с ценами 👇 Это может занять немного времени.")
    for name in names:
        await bot.send_chat_action(message.chat.id, "upload_document")
        # Файл загружается в Telegram один раз, дальше отправляется мгновенно по его file_id
        cached = price_file_ids.get(name)
        sent = await message.answer_document(cached or FSInputFile(files[name]))
        if not cached:
            price_file_ids[name] = sent.document.file_id
            PRICE_IDS_FILE.write_text(json.dumps(price_file_ids, ensure_ascii=False, indent=2), encoding="utf-8")


@dp.message(F.chat.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(message: Message, bot: Bot):
    """Вы отвечаете клиенту: делаете reply на пересланное ботом сообщение."""
    original = message.reply_to_message
    client_id = None
    if original.forward_origin and getattr(original.forward_origin, "sender_user", None):
        client_id = original.forward_origin.sender_user.id
    else:
        found = re.search(r"ID клиента: (\d+)", original.text or "")
        client_id = int(found.group(1)) if found else None
    if not client_id:
        await message.answer("Не понял, кому ответить. Сделайте reply на сообщение с «ID клиента».")
        return
    await bot.send_message(client_id, f"Ответ менеджера:\n{message.text}")
    await message.answer("✅ Отправлено клиенту")


@dp.message(F.text)
async def answer(message: Message, bot: Bot):
    text = message.text
    faq = load_faq(load_knowledge())

    # 1. Нажали кнопку — точное совпадение с вопросом
    if text in faq:
        await message.answer(faq[text])
        return

    # 2. Спрашиваем ИИ (если подключён)
    if USE_AI:
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            reply = await ask_claude(message.from_user.id, text)
        except anthropic.APIError as e:
            logging.error("Ошибка Claude API: %s", e)
            reply = None
        if reply:
            await message.answer(reply)
            return
    else:
        # 2б. Без ИИ — ищем по ключевым словам
        reply = find_by_keywords(text, faq)
        if reply:
            await message.answer(reply)
            return

    # 3. Не нашли ответ — передаём человеку
    await message.answer(NO_ANSWER)
    user = message.from_user
    await notify_admin(
        bot,
        f"❓ Вопрос без ответа\nОт: {user.full_name} (@{user.username})\n"
        f"ID клиента: {user.id}\n\n{text}\n\n"
        "Чтобы ответить — сделайте «Ответить» (reply) на это сообщение.",
    )


async def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN в файле .env")
    bot = Bot(TELEGRAM_TOKEN)
    print("Бот запущен! ИИ:", "включён" if USE_AI else "выключен", "| Остановить: Ctrl+C")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
