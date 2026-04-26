import random
import string
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from database import AsyncSessionLocal
from models import TelegramCode

BOT_TOKEN = "8411695846:AAGJuXfzK-4rAS5MdXyKddikY7h62oN60W0"

# Бот без всяких прокси. Будет работать через твой VPN на компьютере.
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    print(f"🤖 Пользователь {message.from_user.first_name} нажал /start")
    
    # Генерируем код
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    
    # Сохраняем в БД
    async with AsyncSessionLocal() as db:
        new_code = TelegramCode(code=code, tg_id=message.from_user.id, tg_username=message.from_user.username)
        db.add(new_code)
        await db.commit()
        
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n\n"
        f"Твой код для регистрации на TradeFlow:\n"
        f"👉 `{code}` 👈\n\n"
        f"Скопируй его и вставь на сайте.",
        parse_mode="Markdown"
    )

async def start_bot():
    print("✅ Telegram Бот успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        print("Бот остановлен.")