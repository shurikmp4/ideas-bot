# Бот для идей

Присылаешь идею текстом или голосовым, нейросеть (gpt-oss через Groq, бесплатно) выбирает папку, идея попадает в базу Notion.
Если ни одна папка не подходит, бот спрашивает, создать ли новую.

## Настройка

1. **Telegram**: напиши @BotFather → `/newbot`, получи токен. Свой ID узнай у @userinfobot.
2. **Notion**: создай базу данных (Database) со свойствами:
   - `Name` (title, есть по умолчанию)
   - `Папка` (тип Select)

   Затем https://www.notion.so/profile/integrations → New integration → скопируй токен.
   В базе нажми `···` → Connections → добавь свою интеграцию.
   ID базы это 32 символа в её ссылке до `?v=`.
3. **Groq**: бесплатный ключ на console.groq.com, он нужен и для папок, и для голосовых.

```bash
cp .env.example .env   # заполни значения
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Папки лежат в `folders.json`. Команды бота: `/folders`, `/addfolder Название`.

Пока бот работает только пока запущен `python bot.py`. Для постоянной работы его можно поставить на VPS, Railway или Fly.io.
