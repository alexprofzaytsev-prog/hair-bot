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
from datetime import date
from pathlib import Path

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
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
ORDER_BUTTON = "🛒 Оформить заказ"
SEMINAR_BUTTON = "🎓 Семинары"
PROMO_BUTTON = "🔥 Акции"
MANAGER_BUTTON = "📞 Связаться с менеджером"
CANCEL_BUTTON = "❌ Отменить"
PICKUP_BUTTON = "🏪 Самовывоз (ул. Спартака, 13)"
# Кому клиенты пишут по кнопке «Связаться с менеджером»
MANAGER_USERNAME = "Zalexpro1"
MANAGER_PHONE = "+7 (903) 632-71-80"
# Здесь бот запоминает file_id каталогов в Telegram. С этим файлом сами PDF на сервере не нужны.
PRICE_IDS_FILE = Path(__file__).with_name("price_ids.json")


def load_price_ids() -> dict:
    if PRICE_IDS_FILE.exists():
        return json.loads(PRICE_IDS_FILE.read_text(encoding="utf-8"))
    return {}


price_file_ids = load_price_ids()

# Афиши семинаров и акций: картинки в Telegram (file_id) и дата, до которой их показывать
SEMINARS_FILE = Path(__file__).with_name("seminars.json")
PROMOS_FILE = Path(__file__).with_name("promotions.json")


def load_posters(path: Path) -> list:
    """Возвращает file_id только актуальных афиш: прошедшие (по полю until) скрываются сами."""
    if not path.exists():
        return []
    today = date.today().isoformat()
    result = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        if isinstance(item, str):
            result.append(item)
        elif not item.get("until") or item["until"] >= today:
            result.append(item["file_id"])
    return result
NO_ANSWER = "Спасибо за ваше сообщение! Я передал его менеджеру — вам ответят в ближайшее время 🙌"
CANT_ANSWER_MARK = "[НЕ ЗНАЮ]"


def load_knowledge() -> str:
    return KNOWLEDGE_FILE.read_text(encoding="utf-8")


def load_faq(text: str) -> dict:
    """Достаёт пары «Вопрос: ... / Ответ: ...» из knowledge.txt."""
    pairs = re.findall(r"Вопрос:\s*(.+?)\s*\nОтвет:\s*(.+?)(?=\n\s*\n|\nВопрос:|\Z)", text, re.S)
    return {q.strip(): a.strip() for q, a in pairs}


def main_keyboard() -> ReplyKeyboardMarkup:
    faq = load_faq(load_knowledge())
    buttons = [
        [KeyboardButton(text=PRICE_BUTTON)],
        [KeyboardButton(text=PROMO_BUTTON), KeyboardButton(text=SEMINAR_BUTTON)],
        [KeyboardButton(text=ORDER_BUTTON), KeyboardButton(text=MANAGER_BUTTON)],
    ] + [[KeyboardButton(text=q)] for q in faq]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def cancel_keyboard(*extra: KeyboardButton) -> ReplyKeyboardMarkup:
    rows = [[b] for b in extra] + [[KeyboardButton(text=CANCEL_BUTTON)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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


async def notify_admin(bot: Bot, text: str) -> bool:
    """Отправляет уведомление владельцу. Ошибка здесь не должна ломать ответ клиенту."""
    if not ADMIN_ID:
        return False
    try:
        await bot.send_message(ADMIN_ID, text)
        return True
    except Exception as e:
        logging.error("Не удалось отправить уведомление на ADMIN_ID=%s: %s", ADMIN_ID, e)
        return False


dp = Dispatcher()


@dp.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш ID: {message.from_user.id}")


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Здравствуйте! 👋 Я помощник магазина профессиональной косметики для волос. "
        "Выберите вопрос ниже или напишите свой.",
        reply_markup=main_keyboard(),
    )


# ---------- Связь с менеджером ----------

@dp.message(F.text == MANAGER_BUTTON)
async def contact_manager(message: Message):
    button = InlineKeyboardButton(text="✍️ Написать в Telegram", url=f"https://t.me/{MANAGER_USERNAME}")
    await message.answer(
        f"Менеджер на связи с 9:00 до 18:00.\n📞 Телефон: {MANAGER_PHONE}\n\n"
        "Или напишите ему в Telegram 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


# ---------- Семинары и акции ----------

def manager_button(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, url=f"https://t.me/{MANAGER_USERNAME}")
    ]])


async def send_posters(message: Message, bot: Bot, photos: list):
    await bot.send_chat_action(message.chat.id, "upload_photo")
    # Telegram отправляет альбомом не больше 10 картинок за раз
    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        if len(chunk) == 1:
            await message.answer_photo(chunk[0])
        else:
            await message.answer_media_group([InputMediaPhoto(media=f) for f in chunk])


@dp.message(F.text == SEMINAR_BUTTON)
async def seminars(message: Message, bot: Bot):
    photos = load_posters(SEMINARS_FILE)
    signup = manager_button("✍️ Записаться на семинар")
    if not photos:
        await message.answer(
            "Сейчас нет запланированных семинаров. Следите за новостями — "
            f"или уточните у менеджера: {MANAGER_PHONE}",
            reply_markup=signup,
        )
        return
    await message.answer("🎓 Актуальные семинары 👇")
    await send_posters(message, bot, photos)
    await message.answer(
        f"Чтобы записаться, напишите менеджеру или позвоните: {MANAGER_PHONE}",
        reply_markup=signup,
    )


@dp.message(F.text == PROMO_BUTTON)
async def promotions(message: Message, bot: Bot):
    photos = load_posters(PROMOS_FILE)
    if not photos:
        await message.answer(
            "Сейчас действующих акций нет — загляните позже 🙂 "
            "Актуальные цены — в разделе «📄 Прайс-лист».",
        )
        return
    await message.answer("🔥 Действующие акции 👇")
    await send_posters(message, bot, photos)
    await message.answer(
        "Чтобы воспользоваться акцией, оформите заказ кнопкой «🛒 Оформить заказ» "
        "или напишите менеджеру.",
        reply_markup=manager_button("✍️ Написать менеджеру"),
    )


# ---------- Оформление заказа (по шагам) ----------

class Order(StatesGroup):
    salon = State()
    phone = State()
    address = State()
    items = State()
    confirm = State()


@dp.message(F.text == CANCEL_BUTTON)
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо, отменил. Чем ещё могу помочь?", reply_markup=main_keyboard())


@dp.message(F.text == ORDER_BUTTON)
async def order_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Order.salon)
    await message.answer(
        "Оформим заказ — это займёт минуту 🙂\n\n"
        "Шаг 1 из 4. Как называется ваш салон и как к вам обращаться?",
        reply_markup=cancel_keyboard(),
    )


@dp.message(Order.salon, F.text)
async def order_salon(message: Message, state: FSMContext):
    await state.update_data(salon=message.text)
    await state.set_state(Order.phone)
    await message.answer(
        "Шаг 2 из 4. Ваш номер телефона для связи? "
        "Нажмите кнопку ниже или напишите номер.",
        reply_markup=cancel_keyboard(KeyboardButton(text="📱 Отправить мой номер", request_contact=True)),
    )


@dp.message(Order.phone, F.contact | F.text)
async def order_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    await state.set_state(Order.address)
    await message.answer(
        "Шаг 3 из 4. Адрес доставки? Или выберите самовывоз.\n"
        "Доставка — при заказе от 5 000 ₽.",
        reply_markup=cancel_keyboard(KeyboardButton(text=PICKUP_BUTTON)),
    )


@dp.message(Order.address, F.text)
async def order_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await state.set_state(Order.items)
    await message.answer(
        "Шаг 4 из 4. Напишите, что хотите заказать: бренд, название, объём и количество. "
        "Можно списком в одном сообщении.\n\n"
        "Например:\nESTEL De Luxe 7/1 — 10 шт\nCouture шампунь 1000 мл — 2 шт",
        reply_markup=cancel_keyboard(),
    )


def order_text(data: dict) -> str:
    return (
        f"🏢 Салон: {data['salon']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"📍 Адрес: {data['address']}\n\n"
        f"🧴 Товары:\n{data['items']}"
    )


@dp.message(Order.items, F.text)
async def order_items(message: Message, state: FSMContext):
    await state.update_data(items=message.text)
    await state.set_state(Order.confirm)
    buttons = [
        [InlineKeyboardButton(text="✅ Отправить заказ", callback_data="order:send")],
        [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="order:restart")],
    ]
    await message.answer(
        "Проверьте, пожалуйста, ваш заказ:\n\n" + order_text(await state.get_data()),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(Order.confirm, F.data == "order:send")
async def order_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    user = callback.from_user
    delivered = await notify_admin(
        bot,
        f"🛒 НОВЫЙ ЗАКАЗ\nОт: {user.full_name} (@{user.username})\nID клиента: {user.id}\n\n"
        f"{order_text(data)}\n\n"
        "Чтобы ответить клиенту — сделайте «Ответить» (reply) на это сообщение.",
    )
    if delivered:
        text = ("✅ Спасибо! Заказ передан менеджеру. "
                "Мы свяжемся с вами для подтверждения в рабочее время (9:00–18:00).")
    else:
        text = f"Извините, не удалось отправить заказ. Пожалуйста, позвоните нам: {MANAGER_PHONE}"
    await callback.message.answer(text, reply_markup=main_keyboard())


@dp.callback_query(F.data == "order:restart")
async def order_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.set_state(Order.salon)
    await callback.message.answer(
        "Шаг 1 из 4. Как называется ваш салон и как к вам обращаться?",
        reply_markup=cancel_keyboard(),
    )


@dp.callback_query(F.data == "order:send")
async def order_expired(callback: CallbackQuery):
    await callback.answer("Этот заказ уже отправлен или устарел", show_alert=True)


@dp.message(Order.confirm)
async def order_confirm_hint(message: Message):
    await message.answer("Нажмите «✅ Отправить заказ» в сообщении выше или «❌ Отменить».")


def price_catalogs():
    """Список каталогов: файлы из папки price, а если её нет — запомненные file_id."""
    files = {p.name: p for p in sorted(PRICE_DIR.glob("*")) if p.is_file() and not p.name.startswith(".")}
    return files, list(files) or list(price_file_ids)


def catalog_title(name: str) -> str:
    """Catalogue_ESTEL_июль_26.pdf -> ESTEL июль 26"""
    stem = re.sub(r"^(catalogue|catalog|каталог|прайс|price)[_ ]*", "", Path(name).stem, flags=re.I)
    return stem.replace("_", " ").strip()


async def send_catalog(message: Message, bot: Bot, name: str, files: dict):
    await bot.send_chat_action(message.chat.id, "upload_document")
    # Файл загружается в Telegram один раз, дальше отправляется мгновенно по его file_id
    cached = price_file_ids.get(name)
    sent = await message.answer_document(cached or FSInputFile(files[name]))
    if not cached:
        price_file_ids[name] = sent.document.file_id
        PRICE_IDS_FILE.write_text(json.dumps(price_file_ids, ensure_ascii=False, indent=2), encoding="utf-8")


@dp.message(F.text == PRICE_BUTTON)
async def send_price(message: Message, bot: Bot):
    files, names = price_catalogs()
    if not names:
        await message.answer(NO_ANSWER)
        user = message.from_user
        await notify_admin(
            bot,
            f"📄 Клиент просит прайс-лист (в папке price нет файлов)\n"
            f"От: {user.full_name} (@{user.username})\nID клиента: {user.id}",
        )
        return
    if len(names) == 1:
        await send_catalog(message, bot, names[0], files)
        return
    # Несколько брендов — даём выбрать нужный каталог
    buttons = [[InlineKeyboardButton(text=f"📘 {catalog_title(n)}", callback_data=f"price:{i}")]
               for i, n in enumerate(names)]
    buttons.append([InlineKeyboardButton(text="📚 Все каталоги", callback_data="price:all")])
    await message.answer("Выберите бренд:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("price:"))
async def price_chosen(callback: CallbackQuery, bot: Bot):
    files, names = price_catalogs()
    choice = callback.data.split(":", 1)[1]
    if choice == "all":
        chosen = names
    elif choice.isdigit() and int(choice) < len(names):
        chosen = [names[int(choice)]]
    else:
        chosen = []
    await callback.answer()
    if not chosen:
        await callback.message.answer("Каталог обновился — нажмите «📄 Прайс-лист» ещё раз.")
        return
    for name in chosen:
        await send_catalog(callback.message, bot, name, files)


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

    # Спрашивают про семинары/обучение — показываем афиши
    if re.search(r"семинар|обучени|мастер-?класс", text.lower()):
        await seminars(message, bot)
        return
    if re.search(r"акци|скидк|распродаж|спецпредлож", text.lower()):
        await promotions(message, bot)
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
