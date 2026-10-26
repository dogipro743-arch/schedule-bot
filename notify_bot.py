"""
Второй бот — рассылка уведомлений в выбранные чаты.

Как работает:
  1. Добавь бота в группы (или пусть люди нажмут в нём /start) — он запомнит эти чаты.
  2. Админ пишет боту в личку любое сообщение (текст, фото, видео, файл...).
  3. Бот показывает список чатов с галочками. Отмечаешь куда слать -> «📤 Отправить».
  Сообщение уходит как есть (copyMessage), без пометки «переслано».

Команды: /start, /chats (список чатов), /cancel (сбросить черновик), /id (id этого чата)

Переменные окружения:
  NOTIFY_TOKEN     токен второго бота от @BotFather
  ADMIN_USERNAMES  кто может рассылать, через запятую, без @ (по умолчанию Kavasaki86)
Зависимости: httpx
"""
import os
import json
import time

import httpx

TOKEN = os.environ["NOTIFY_TOKEN"]
ADMINS = {u.strip().lstrip("@").lower()
          for u in (os.environ.get("ADMIN_USERNAMES") or "Kavasaki86").split(",") if u.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "notify_state.json"
RUN_SECONDS = 3300  # один запуск слушает Telegram ~55 минут


# ───────────────────────── состояние ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in (("offset", 0), ("chats", {}), ("admin_ids", []), ("draft", None)):
        st.setdefault(k, v)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ───────────────────────── Telegram ─────────────────────────
def tg(method, **data):
    try:
        return httpx.post(f"{API}/{method}", data=data, timeout=60).json()
    except Exception as e:
        print("tg error:", method, e)
        return {"ok": False, "description": str(e)}


def say(chat, text, markup=None, reply_to=None):
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = json.dumps(markup)
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return tg("sendMessage", **data)


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_admin(st, user) -> bool:
    if not user:
        return False
    uid = user.get("id")
    if uid in st["admin_ids"]:
        return True
    if (user.get("username") or "").lower() in ADMINS:
        st["admin_ids"].append(uid)
        return True
    return False


def chat_title(chat) -> str:
    if chat.get("title"):
        return chat["title"]
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if chat.get("username"):
        name = f"{name} (@{chat['username']})".strip()
    return name or str(chat["id"])


def register_chat(st, chat):
    st["chats"][str(chat["id"])] = {"title": chat_title(chat), "type": chat["type"]}


# ───────────────────────── панель выбора ─────────────────────────
def panel_markup(st):
    sel = set(st["draft"]["sel"])
    rows = []
    for cid, c in st["chats"].items():
        mark = "✅" if int(cid) in sel else "⬜"
        icon = "👤" if c["type"] == "private" else "👥"
        rows.append([{"text": f"{mark} {icon} {c['title'][:30]}", "callback_data": f"t:{cid}"}])
    rows.append([{"text": "☑️ Все", "callback_data": "all"}, {"text": "🧹 Сбросить", "callback_data": "none"}])
    rows.append([{"text": f"📤 Отправить ({len(sel)})", "callback_data": "send"},
                 {"text": "❌ Отмена", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def do_send(st, panel_chat, panel_msg):
    d = st["draft"]
    ok, fails = 0, []
    for cid in d["sel"]:
        r = {}
        for attempt in range(2):
            r = tg("copyMessage", chat_id=cid, from_chat_id=d["chat"], message_id=d["msg"])
            if r.get("ok"):
                break
            retry = (r.get("parameters") or {}).get("retry_after")
            if retry and attempt == 0:
                time.sleep(min(int(retry), 30))
                continue
            break
        if r.get("ok"):
            ok += 1
        else:
            desc = r.get("description", "ошибка")
            title = st["chats"].get(str(cid), {}).get("title", str(cid))
            fails.append(f"• {esc(title)}: {esc(desc)}")
            if any(x in desc.lower() for x in ("kicked", "blocked", "chat not found", "deactivated")):
                st["chats"].pop(str(cid), None)
        time.sleep(0.05)
    text = f"📤 Отправлено: <b>{ok}</b> из {len(d['sel'])}"
    if fails:
        text += "\n\nНе удалось:\n" + "\n".join(fails)
    tg("editMessageText", chat_id=panel_chat, message_id=panel_msg, text=text, parse_mode="HTML")
    st["draft"] = None


# ───────────────────────── обработка событий ─────────────────────────
def handle_message(st, m):
    chat = m["chat"]
    user = m.get("from") or {}
    private = chat["type"] == "private"
    text = (m.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None

    if not private:
        register_chat(st, chat)
        if cmd == "/id":
            say(chat["id"], f"id этого чата: <code>{chat['id']}</code>")
        return

    admin = is_admin(st, user)
    if cmd == "/id":
        say(chat["id"], f"id этого чата: <code>{chat['id']}</code>\nтвой id: <code>{user.get('id')}</code>")
        return
    if cmd == "/start":
        if admin:
            register_chat(st, chat)
            say(chat["id"], "Привет! Я рассылаю уведомления.\n\n"
                            "1. Добавь меня в группы (или пусть люди нажмут тут /start).\n"
                            "2. Напиши мне сообщение (текст, фото, файл).\n"
                            "3. Выбери, куда отправить, и нажми «Отправить».\n\n"
                            "/chats — список чатов, /cancel — сбросить черновик")
        else:
            register_chat(st, chat)
            say(chat["id"], "Привет! Теперь я смогу присылать тебе уведомления.")
        return
    if not admin:
        return  # остальных молча игнорируем
    register_chat(st, chat)

    if cmd == "/chats":
        if not st["chats"]:
            say(chat["id"], "Пока нет чатов. Добавь меня в группу или нажми /start у себя.")
        else:
            lines = [f"{'👤' if c['type'] == 'private' else '👥'} {esc(c['title'])} "
                     f"<code>{cid}</code>" for cid, c in st["chats"].items()]
            say(chat["id"], "Известные чаты:\n" + "\n".join(lines))
        return
    if cmd == "/cancel":
        st["draft"] = None
        say(chat["id"], "Черновик сброшен.")
        return
    if cmd:
        return

    # любое другое сообщение админа = черновик рассылки
    if not st["chats"]:
        say(chat["id"], "Пока некуда слать: добавь меня в группу или пусть кто-то нажмёт /start.")
        return
    st["draft"] = {"chat": chat["id"], "msg": m["message_id"], "sel": [], "panel": None}
    r = say(chat["id"], "Куда отправить это сообщение? Отметь чаты:", panel_markup(st),
            reply_to=m["message_id"])
    if r.get("ok"):
        st["draft"]["panel"] = r["result"]["message_id"]


def handle_callback(st, cb):
    user = cb.get("from") or {}
    if not is_admin(st, user):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Только для админа")
        return
    d = st["draft"]
    msg = cb["message"]
    if not d or d.get("panel") != msg["message_id"]:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Черновик устарел, напиши сообщение заново")
        return
    data = cb.get("data", "")
    if data.startswith("t:"):
        cid = int(data[2:])
        if cid in d["sel"]:
            d["sel"].remove(cid)
        else:
            d["sel"].append(cid)
    elif data == "all":
        d["sel"] = [int(c) for c in st["chats"]]
    elif data == "none":
        d["sel"] = []
    elif data == "cancel":
        st["draft"] = None
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отменено")
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text="❌ Отменено")
        return
    elif data == "send":
        if not d["sel"]:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Выбери хотя бы один чат")
            return
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Отправляю…")
        do_send(st, msg["chat"]["id"], msg["message_id"])
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
       reply_markup=json.dumps(panel_markup(st)))


def handle_my_chat_member(st, mc):
    chat = mc["chat"]
    status = mc["new_chat_member"]["status"]
    if status in ("member", "administrator"):
        known = str(chat["id"]) in st["chats"]
        register_chat(st, chat)
        if chat["type"] != "private" and not known:
            for aid in st["admin_ids"]:
                say(aid, f"➕ Добавлен чат: <b>{esc(chat_title(chat))}</b>")
    elif status in ("left", "kicked"):
        st["chats"].pop(str(chat["id"]), None)
        if st["draft"]:
            st["draft"]["sel"] = [c for c in st["draft"]["sel"] if c != chat["id"]]


def process_updates(st, wait):
    r = tg("getUpdates", offset=st["offset"], timeout=wait,
           allowed_updates=json.dumps(["message", "callback_query", "my_chat_member"]))
    if not r.get("ok"):
        print("getUpdates error:", r)
        time.sleep(5)
        return 0
    res = r.get("result", [])
    for u in res:
        st["offset"] = u["update_id"] + 1
        try:
            if u.get("my_chat_member"):
                handle_my_chat_member(st, u["my_chat_member"])
            elif u.get("callback_query"):
                handle_callback(st, u["callback_query"])
            elif u.get("message"):
                handle_message(st, u["message"])
        except Exception as e:
            print("update error:", e)
    return len(res)


def main():
    st = load_state()
    started = time.time()
    try:
        tg("deleteWebhook")
        tg("setMyCommands", commands=json.dumps([
            {"command": "chats", "description": "Список чатов"},
            {"command": "cancel", "description": "Сбросить черновик"},
            {"command": "id", "description": "Показать id чата"}]))
        while time.time() - started < RUN_SECONDS:
            if process_updates(st, wait=20):
                save_state(st)
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
