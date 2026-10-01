"""Сервер мини-приложения: отдаёт страницу и API со списком идей (данные лежат в Notion)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from aiohttp import web

log = logging.getLogger("ideas-web")
INDEX = Path(__file__).with_name("webapp") / "index.html"
MAX_AUTH_AGE = 24 * 3600


def valid_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает user или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(pairs["auth_date"]) > MAX_AUTH_AGE:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def _plain(rich: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich)


def make_app(*, owner_id: int, bot_token: str, notion_headers: dict, db_id: str, dev: bool = False) -> web.Application:
    db_url = f"https://api.notion.com/v1/databases/{db_id}"

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path.startswith("/api/"):
            if not dev:
                user = valid_init_data(request.headers.get("X-Telegram-Init-Data", ""), bot_token)
                if not user or user.get("id") != owner_id:
                    return web.json_response({"error": "forbidden"}, status=403)
        return await handler(request)

    async def index(request: web.Request):
        return web.FileResponse(INDEX)

    async def list_ideas(request: web.Request):
        ideas, cursor = [], None
        async with httpx.AsyncClient(timeout=30) as http:
            while True:
                body = {"page_size": 100, "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
                if cursor:
                    body["start_cursor"] = cursor
                r = await http.post(f"{db_url}/query", json=body, headers=notion_headers)
                r.raise_for_status()
                data = r.json()
                for p in data["results"]:
                    pr = p["properties"]
                    title = _plain(pr["Name"]["title"])
                    ideas.append(
                        {
                            "id": p["id"],
                            "title": title,
                            "text": _plain(pr.get("Текст", {}).get("rich_text", [])) or title,
                            "folder": (pr["Папка"]["select"] or {}).get("name", "Без папки"),
                            "done": pr.get("Готово", {}).get("checkbox", False),
                            "created": p["created_time"],
                        }
                    )
                if not data["has_more"]:
                    break
                cursor = data["next_cursor"]
        return web.json_response(ideas)

    async def set_done(request: web.Request):
        page_id = request.match_info["page_id"]
        done = bool((await request.json()).get("done"))
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                json={"properties": {"Готово": {"checkbox": done}}},
                headers=notion_headers,
            )
            r.raise_for_status()
        return web.json_response({"ok": True})

    async def delete_idea(request: web.Request):
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.patch(
                f"https://api.notion.com/v1/pages/{request.match_info['page_id']}",
                json={"archived": True},  # в корзину Notion, не насовсем
                headers=notion_headers,
            )
            r.raise_for_status()
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[auth])
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/ideas", list_ideas),
            web.post("/api/ideas/{page_id}/done", set_done),
            web.delete("/api/ideas/{page_id}", delete_idea),
        ]
    )
    return app
