from __future__ import annotations

import json
import logging
import os
import uuid

import httpx
from aiohttp import web as aioweb
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import web  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ideas-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["ALLOWED_USER_ID"])
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DATABASE_ID"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
WEBAPP_URL = os.environ.get("WEBAPP_URL")  # публичный https-адрес сервера (Railway)
PORT = int(os.environ.get("PORT", "8080"))
DEV_NO_AUTH = os.environ.get("DEV_NO_AUTH") == "1"  # только для локальной проверки

FALLBACK_FOLDER = "Разное"

NOTION_HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"}
NOTION_DB_URL = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}"


async def load_folders() -> list[str]:
    """Папки = варианты Select «Папка» в базе Notion (там же они создаются автоматически)."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(NOTION_DB_URL, headers=NOTION_HEADERS)
        r.raise_for_status()
    options = r.json()["properties"]["Папка"]["select"]["options"]
    return [o["name"] for o in options]


async def add_folder(name: str) -> None:
    folders = await load_folders()
    if name in folders:
        return
    options = [{"name": f} for f in folders] + [{"name": name}]
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.patch(
            NOTION_DB_URL,
            json={"properties": {"Папка": {"select": {"options": options}}}},
            headers=NOTION_HEADERS,
        )
        r.raise_for_status()


async def classify(text: str) -> dict:
    folders = await load_folders()
    system = (
        "Ты раскладываешь идеи пользователя по папкам. Существующие папки:\n"
        + "\n".join(f"- {f}" for f in folders)
        + "\n\nВыбирай существующую папку, если идея хоть как-то подходит. "
        "Новую предлагай только если идея явно не вписывается ни в одну.\n"
        'Ответь JSON-объектом: {"title": "заголовок идеи до 60 символов", '
        '"folder": "точное название существующей папки или название новой", '
        '"is_new_folder": true/false}'
    )
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            },
        )
        r.raise_for_status()
    result = json.loads(r.json()["choices"][0]["message"]["content"])
    result["is_new_folder"] = bool(result.get("is_new_folder"))
    # страховка: модель назвала «существующую» папку, которой нет в списке
    if not result["is_new_folder"] and result.get("folder") not in folders:
        result["is_new_folder"] = True
    result.setdefault("title", text[:60])
    result.setdefault("folder", FALLBACK_FOLDER)
    return result


async def save_to_notion(title: str, folder: str, text: str) -> None:
    body = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Папка": {"select": {"name": folder}},  # новые варианты Notion создаёт сам
            "Текст": {"rich_text": [{"text": {"content": text[:2000]}}]},
        },
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": text[i : i + 2000]}}]},
            }
            for i in range(0, len(text), 2000)
        ],
    }
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            "https://api.notion.com/v1/pages",
            json=body,
            headers=NOTION_HEADERS,
        )
        r.raise_for_status()


async def transcribe(audio: bytes) -> str:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-large-v3-turbo"},
        )
        r.raise_for_status()
        return r.json()["text"].strip()


async def ensure_schema() -> None:
    """Добавляет в базу поля для мини-приложения, если их ещё нет."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(NOTION_DB_URL, headers=NOTION_HEADERS)
        r.raise_for_status()
        props = r.json()["properties"]
        missing = {}
        if "Готово" not in props:
            missing["Готово"] = {"checkbox": {}}
        if "Текст" not in props:
            missing["Текст"] = {"rich_text": {}}
        if missing:
            r = await http.patch(NOTION_DB_URL, json={"properties": missing}, headers=NOTION_HEADERS)
            r.raise_for_status()


async def post_init(app: Application) -> None:
    await ensure_schema()
    site = web.make_app(
        owner_id=OWNER_ID,
        bot_token=TELEGRAM_TOKEN,
        notion_headers=NOTION_HEADERS,
        db_id=NOTION_DB_ID,
        dev=DEV_NO_AUTH,
    )
    runner = aioweb.AppRunner(site)
    await runner.setup()
    await aioweb.TCPSite(runner, "0.0.0.0", PORT).start()
    app.bot_data["web_runner"] = runner
    if WEBAPP_URL:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="💡 Идеи", web_app=WebAppInfo(WEBAPP_URL))
        )


async def post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def owner_only(handler):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id == OWNER_ID:
            return await handler(update, ctx)
    return wrapper


async def process_idea(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    msg = update.effective_message
    try:
        result = await classify(text)
    except Exception:
        log.exception("classify failed")
        await msg.reply_text("Не получилось определить папку, сохраняю в «%s»." % FALLBACK_FOLDER)
        result = {"title": text[:60], "folder": FALLBACK_FOLDER, "is_new_folder": False}

    if result["is_new_folder"]:
        # ждём решения пользователя: создать новую папку или бросить в «Разное»
        key = uuid.uuid4().hex[:8]
        ctx.bot_data[key] = {"text": text, **result}
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"Создать «{result['folder']}»", callback_data=f"new:{key}")],
                [InlineKeyboardButton(f"В «{FALLBACK_FOLDER}»", callback_data=f"misc:{key}")],
            ]
        )
        await msg.reply_text(
            f"💡 {result['title']}\nНи одна папка не подходит. Создать новую?", reply_markup=kb
        )
        return

    await save_to_notion(result["title"], result["folder"], text)
    await msg.reply_text(f"💡 Идея сохранена\n✅ {result['title']}\n📁 {result['folder']}")


@owner_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await process_idea(update, ctx, update.message.text)


@owner_only
async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        file = await update.message.voice.get_file()
        text = await transcribe(bytes(await file.download_as_bytearray()))
    except Exception as e:
        log.exception("transcribe failed")
        await update.message.reply_text(f"Не смог расшифровать голосовое: {e}")
        return
    await update.message.reply_text(f"🎙 {text}")
    await process_idea(update, ctx, text)


@owner_only
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action, key = q.data.split(":", 1)
    pending = ctx.bot_data.pop(key, None)
    if not pending:
        await q.edit_message_text("Эта идея уже обработана.")
        return
    folder = pending["folder"] if action == "new" else FALLBACK_FOLDER
    if action == "new":
        await add_folder(folder)
    await save_to_notion(pending["title"], folder, pending["text"])
    await q.edit_message_text(f"💡 Идея сохранена\n✅ {pending['title']}\n📁 {folder}")


@owner_only
async def cmd_folders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📁 Папки:\n" + "\n".join(f"• {f}" for f in await load_folders()))


@owner_only
async def cmd_addfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Использование: /addfolder Название папки")
        return
    await add_folder(name)
    await update.message.reply_text(f"Папка «{name}» добавлена.")


@owner_only
async def cmd_app(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        await update.message.reply_text("Мини-приложение пока не подключено (нет WEBAPP_URL).")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💡 Открыть идеи", web_app=WebAppInfo(WEBAPP_URL))]])
    await update.message.reply_text("💡 Твои идеи:", reply_markup=kb)


@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "💡 Присылай идею текстом или голосовым, я разложу её по папкам.\n"
        "/app — открыть список идей\n/folders — список папок\n/addfolder <название> — добавить папку"
    )


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("folders", cmd_folders))
    app.add_handler(CommandHandler("addfolder", cmd_addfolder))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
