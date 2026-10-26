"""
Бот расписания ЮПК для Telegram. Работает в GitHub Actions (сервер не нужен).

Что умеет:
  • В личке — кнопки: Сегодня / Завтра / 3 дня / Погода / Сменить группу
  • В любом групповом чате — команды /today /tomorrow /r /weather /group /all
  • У каждого чата/человека своя группа (выбирается кнопкой)
  • Уведомления (кнопка 🔔): «Доброе утро» после 6:50 (пн-сб), расписание на завтра
    после 19:00 (вс-пт) и «ВНИМАНИЕ! НОВОЕ РАСПИСАНИЕ!» при изменениях.
    В группах включены сразу, в личке — по кнопке.

Каждый запуск живёт ~4.5 минуты и всё это время слушает Telegram (long polling),
поэтому ответы на кнопки почти мгновенные. Следующий запуск стартует сразу после.

Переменные: BOT_TOKEN (обязательно), GROUP (необязательно, по умолчанию НК-24-3),
            NOTIFY_TOKEN (токен второго бота-рассылки, необязательно),
            ADMIN_USERNAMES / ADMIN_IDS (кто может рассылать; по умолчанию ник Kavasaki86)
Зависимости: httpx beautifulsoup4 pillow
"""
import io
import os
import re
import json
import time
import hashlib
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.environ["BOT_TOKEN"]
GROUP = os.environ.get("GROUP") or "НК-24-3"
BASE = "https://raspisanie.doyupk.ru/"
TZ = ZoneInfo("Asia/Yekaterinburg")
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"
LAT, LON = 61.31, 63.34  # Югорск
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
LESSON_MIN = 45
CREDIT = "👨‍💻 Создатель бота: @Kavasaki86"
RUN_SECONDS = 3300  # один запуск слушает Telegram ~55 минут

GROUPS = ("А-252 А-261 АТ-24-3 АТ-26-1 ГД-243 ГД-252 ГД-261 ДОШ-23-4 ДОШ-25-2 ЖКХ-252 ИСиП-23-4 "
          "ИСиП-25-2 ИСиП-26-1 КИП-243 КИП-252 КИП-261 МАОР-252 МАОР-261 МКС-26-1 МН-24-3 НК-24-3 "
          "НК-26-1 ОМД-24-3 РЗХ-252 СД-24-3озк СД-25-2 СЭГ-23-41 СЭГ-24-3 СЭГ-25-2 СЭГ-26-1 СЭЗ-23-4 "
          "СЭЗ-24-3 СЭЗ-25-2 СЭЗ-26-1 ТД-25-2 ТО-23-4 ТО-25-2 ТО-26-1 ЦВ-261 Э-24-3 Э-26-1 ЭГС-252 "
          "ЭГС-261 ЭЛ-261 ЭМ-23-4 ЭМ-25-2 ЭТР-23-4").split()

KEYBOARD = {"keyboard": [[{"text": "📅 Сегодня"}, {"text": "📆 Завтра"}],
                         [{"text": "🗓 3 дня"}, {"text": "🌡 Погода"}],
                         [{"text": "🔁 Сменить группу"}, {"text": "🔔 Уведомления"}]],
            "resize_keyboard": True}
BUTTONS = {"📅 Сегодня": "/today", "📆 Завтра": "/tomorrow", "🗓 3 дня": "/r",
           "🌡 Погода": "/weather", "🔁 Сменить группу": "/group", "🔔 Уведомления": "/notify"}


# ───────────────────────── состояние ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in (("hashes", {}), ("offset", 0), ("groups", {}), ("chat_users", {}), ("subs", {}), ("sent", {}), ("credited", {}),
                 ("titles", {}), ("n_offset", 0), ("admin_ids", []), ("draft", None)):
        st.setdefault(k, v)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def group_of(st, chat) -> str:
    return st["groups"].get(str(chat), GROUP)


# ───────────────────────── парсинг сайта ─────────────────────────
def add_min(hhmm: str, mins: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + mins
    return f"{t // 60:02d}:{t % 60:02d}"


def parse_table(table):
    rows = []
    for tr in table.find_all("tr"):
        c = [x.get_text(" ", strip=True) for x in tr.find_all("td")]
        if len(c) < 4:
            continue
        m = re.match(r"(\d+)\s*\((\d{1,2}:\d{2})\)", c[1])
        if not m:
            continue
        rows.append({"teacher": c[0], "n": int(m[1]), "start": m[2], "name": c[2], "room": c[3]})
    return rows


def build_blocks(rows):
    """Строки таблицы -> блоки «Пара N» (по два урока), обед отдельно.
    Точные повторы одной и той же строки (урок, предмет, препод, кабинет) убираются."""
    seen, uniq = set(), []
    for r in rows:
        k = (r["n"], r["start"], r["name"], r["teacher"], r["room"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    rows = sorted(uniq, key=lambda r: r["n"])
    blocks, seg, pair = [], [], 0

    def flush():
        nonlocal pair, seg
        nums = sorted({r["n"] for r in seg})
        for i in range(0, len(nums), 2):
            pair += 1
            chunk = set(nums[i:i + 2])
            groups = {}
            for r in seg:
                if r["n"] in chunk:
                    groups.setdefault((r["teacher"], r["name"]), []).append(r)
            for g in groups.values():
                blocks.append({"title": f"Пара {pair}", "rows": [
                    {"n": r["n"], "time": f"{r['start']} - {add_min(r['start'], LESSON_MIN)}",
                     "name": r["name"], "teacher": r["teacher"], "room": r["room"]}
                    for r in sorted(g, key=lambda x: x["n"])]})
        seg = []

    for r in rows:
        if r["name"].strip().lower() == "обед":
            flush()
            blocks.append({"lunch": {"n": r["n"], "time": f"{r['start']} - {add_min(r['start'], LESSON_MIN)}"}})
        else:
            seg.append(r)
    flush()
    return blocks


_cache = {}


def fetch(day: date, group: str) -> dict:
    """{'2026-10-26': blocks, ...} — сайт отдаёт выбранный день + 2 следующих."""
    key = (group, day.isoformat(), int(time.time() // 120))  # кэш на 2 минуты
    if key in _cache:
        return _cache[key]
    r = httpx.get(BASE, params={"teachername": 0, "group": group, "subgroup": 0, "kabinet": 0,
                                "date": day.isoformat()}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows_by_day, cur = {}, None
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "table"]):
        if el.name == "table":
            if cur:
                rows_by_day[cur].extend(parse_table(el))
            continue
        t = el.get_text(strip=True)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
            cur = t
            rows_by_day.setdefault(cur, [])
    result = {d: build_blocks(rows) for d, rows in rows_by_day.items()}
    _cache[key] = result
    return result


# ───────────────────────── картинка ─────────────────────────
def font(size, bold=False):
    paths = []
    if bold:
        paths += ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
    paths += [os.environ.get("FONT_PATH", ""), "DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]
    for p in paths:
        if p:
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def fit(d, text, f, maxw):
    if d.textlength(text, font=f) <= maxw:
        return text
    while len(text) > 1 and d.textlength(text + "…", font=f) > maxw:
        text = text[:-1]
    return text + "…"


def render(day: date, blocks, group: str) -> io.BytesIO:
    """Тёмный стиль: карточки с цветной шапкой-градиентом."""
    W, mx = 900, 36
    BG, CARD, LINEC = (15, 17, 26), (26, 30, 44), (46, 52, 74)
    ACC, ACC2, AMBER = (99, 102, 241), (56, 189, 248), (251, 191, 36)
    TXT, GREY = (238, 240, 250), (140, 146, 170)
    f_big, f_med = font(42, True), font(26)
    f_sub, f_small, f_tag = font(25, True), font(19), font(17, True)

    img = Image.new("RGB", (W, 8000), BG)
    d = ImageDraw.Draw(img)

    HH = 190
    for x in range(W):
        t = x / W
        d.line([x, 0, x, HH], fill=tuple(int(ACC[i] + (ACC2[i] - ACC[i]) * t) for i in range(3)))
    d.text((mx, 34), "Расписание", font=f_big, fill="white")
    d.text((mx, 100), f"{DAYS[day.weekday()]}, {day:%d.%m.%Y}", font=f_med, fill=(235, 240, 255))
    pw = int(d.textlength(group, font=f_sub)) + 44
    d.rounded_rectangle([W - mx - pw, 38, W - mx, 86], radius=24, fill=(255, 255, 255))
    d.text((W - mx - pw + 22, 50), group, font=f_sub, fill=(45, 45, 110))

    y = HH + 28
    for b in blocks:
        if "lunch" in b:
            l = b["lunch"]
            d.rounded_rectangle([mx, y, W - mx, y + 78], radius=22, fill=(46, 38, 12), outline=AMBER, width=2)
            d.text((mx + 28, y + 22), "Обед", font=f_sub, fill=AMBER)
            tw = d.textlength(l["time"], font=f_small)
            d.text((W - mx - 28 - tw, y + 26), l["time"], font=f_small, fill=AMBER)
            y += 98
            continue

        rows = b["rows"]
        h = 56 + len(rows) * 100 + 8
        d.rounded_rectangle([mx, y, W - mx, y + h], radius=22, fill=CARD)
        d.text((mx + 28, y + 18), b["title"].upper(), font=f_tag, fill=ACC2)
        ry = y + 52
        for i, r in enumerate(rows):
            start, end = r["time"].split(" - ")
            d.text((mx + 28, ry + 8), start, font=f_sub, fill=TXT)
            d.text((mx + 28, ry + 44), end, font=f_small, fill=GREY)
            d.rounded_rectangle([mx + 132, ry + 10, mx + 138, ry + 80], radius=3, fill=ACC)
            tag = re.search(r"\((I|II)\)", r["name"])
            name = re.sub(r"\s*\((I|II)\)", "", r["name"]).strip()
            maxw = (W - mx - 28) - (mx + 160) - (120 if tag else 0)
            d.text((mx + 160, ry + 10), fit(d, name, f_sub, maxw), font=f_sub, fill=TXT)
            info = f"{r['teacher']}  •  каб. {r['room']}" if r["teacher"] else f"каб. {r['room']}"
            d.text((mx + 160, ry + 50), fit(d, info, f_small, (W - mx - 28) - (mx + 160)), font=f_small, fill=GREY)
            if tag:
                label = f"подгр. {tag.group(1)}"
                lw = int(d.textlength(label, font=f_tag)) + 28
                d.rounded_rectangle([W - mx - 28 - lw, ry + 12, W - mx - 28, ry + 42], radius=15, outline=ACC, width=2)
                d.text((W - mx - 28 - lw + 14, ry + 17), label, font=f_tag, fill=ACC)
            ry += 100
            if i < len(rows) - 1:
                d.line([mx + 28, ry - 10, W - mx - 28, ry - 10], fill=LINEC, width=2)
        y += h + 20

    y += 6
    foot = "ЮПК  •  Создатель бота @Kavasaki86"
    d.text(((W - d.textlength(foot, font=f_small)) / 2, y), foot, font=f_small, fill=GREY)
    upd = f"обновлено {datetime.now(TZ):%H:%M}"
    d.text(((W - d.textlength(upd, font=f_tag)) / 2, y + 30), upd, font=f_tag, fill=(95, 101, 125))
    img = img.crop((0, 0, W, y + 70))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


# ───────────────────────── Telegram ─────────────────────────
def tg(method, **data):
    try:
        return httpx.post(f"{API}/{method}", data=data, timeout=60).json()
    except Exception as e:
        print("tg error:", method, e)
        return {}


def say(chat, text, markup=None):
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = json.dumps(markup)
    tg("sendMessage", **data)


def send_day(chat, day: date, data: dict, group: str, new=False):
    blocks = data.get(day.isoformat())
    if not blocks:
        say(chat, f"{group}: на {day:%d.%m.%Y} ({DAYS[day.weekday()]}) расписания нет — "
                  "оно ещё не загружено или занятий нет.")
        return
    head = "🔔 <b>ВНИМАНИЕ! НОВОЕ РАСПИСАНИЕ!</b>\n\n" if new else ""
    cap = f"{head}📅 <b>{day:%d.%m.%Y} ({DAYS[day.weekday()]})\n{group}</b>"
    httpx.post(f"{API}/sendPhoto", data={"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
               files={"photo": ("schedule.png", render(day, blocks, group), "image/png")}, timeout=60)


def wind_dir(deg):
    n = ["северный", "северо-восточный", "восточный", "юго-восточный",
         "южный", "юго-западный", "западный", "северо-западный"]
    return n[int((deg + 22.5) // 45) % 8]


def weather_text():
    try:
        cur = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
            "latitude": LAT, "longitude": LON, "wind_speed_unit": "ms",
            "current": "temperature_2m,wind_speed_10m,wind_direction_10m"}).json()["current"]
    except Exception:
        return "🌡 Погоду получить не удалось."
    return (f"🌡 <b>Погода в Югорске</b>\n⏰ Обновлено: {datetime.now(TZ):%H:%M}\n"
            f"🌡 Температура: <b>{cur['temperature_2m']:+.0f}°C</b>\n"
            f"💨 Ветер: <b>{cur['wind_speed_10m']:.1f} м/с</b>, {wind_dir(cur['wind_direction_10m'])}")


def group_picker(chat):
    rows = [GROUPS[i:i + 3] for i in range(0, len(GROUPS), 3)]
    markup = {"inline_keyboard": [[{"text": g, "callback_data": "g:" + g} for g in row] for row in rows]}
    say(chat, "Выбери свою группу:", markup)


# ───────────────────────── команды ─────────────────────────
def handle_command(cmd, chat, private, st, today):
    group = group_of(st, chat)
    kb = KEYBOARD if private else None
    if cmd == "/start":
        say(chat, f"Привет! Я показываю расписание ЮПК картинкой.\nТекущая группа: <b>{group}</b>", kb)
        if private and str(chat) not in st["groups"]:
            group_picker(chat)
    elif cmd == "/group":
        group_picker(chat)
    elif cmd in ("/today", "/tomorrow", "/r"):
        data = fetch(today, group)
        if cmd == "/today":
            send_day(chat, today, data, group)
        elif cmd == "/tomorrow":
            send_day(chat, today + timedelta(days=1), data, group)
        else:
            for i in range(3):
                send_day(chat, today + timedelta(days=i), data, group)
    elif cmd == "/notify":
        on = not st["subs"].get(str(chat), not private)
        st["subs"][str(chat)] = on
        say(chat, "🔔 Уведомления включены: утреннее расписание, расписание на завтра "
                  "и сообщения об изменениях." if on else "🔕 Уведомления выключены.", kb)
    elif cmd == "/weather":
        say(chat, weather_text(), kb)
    elif cmd == "/all":
        if private:
            say(chat, "Эта команда работает только в групповом чате.", kb)
            return
        users = st["chat_users"].get(str(chat), {})
        if not users:
            say(chat, "Пока никого не знаю — пусть сначала напишут в чат.")
            return
        items = [f'<a href="tg://user?id={uid}">{name}</a>' for uid, name in users.items()]
        for i in range(0, len(items), 20):
            say(chat, "📢 Все сюда!\n" + " ".join(items[i:i + 20]))


def process_updates(st, today, wait):
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
            mc = u.get("my_chat_member")
            if mc:
                if mc["chat"]["type"] != "private" and mc["new_chat_member"]["status"] in ("member", "administrator"):
                    cid = mc["chat"]["id"]
                    st["subs"].setdefault(str(cid), True)
                    st["titles"][str(cid)] = {"title": chat_title(mc["chat"]), "type": mc["chat"]["type"]}
                    say(cid, "Привет! Я показываю расписание ЮПК.\n"
                             f"Группа сейчас: <b>{group_of(st, cid)}</b>. Сменить: /group\n"
                             "Команды: /today, /tomorrow, /r, /weather")
                continue
            cb = u.get("callback_query")
            if cb:
                data = cb.get("data", "")
                chat = cb["message"]["chat"]["id"]
                private = cb["message"]["chat"]["type"] == "private"
                if data.startswith("g:") and data[2:] in GROUPS:
                    st["groups"][str(chat)] = data[2:]
                    tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Группа: {data[2:]}")
                    say(chat, f"Готово! Группа: <b>{data[2:]}</b>", KEYBOARD if private else None)
                else:
                    tg("answerCallbackQuery", callback_query_id=cb["id"])
                continue
            m = u.get("message")
            if not m:
                continue
            chat = m["chat"]["id"]
            private = m["chat"]["type"] == "private"
            st["subs"].setdefault(str(chat), not private)
            st["titles"][str(chat)] = {"title": chat_title(m["chat"]), "type": m["chat"]["type"]}
            frm = m.get("from") or {}
            if not private and frm and not frm.get("is_bot"):
                st["chat_users"].setdefault(str(chat), {})[str(frm["id"])] = (frm.get("first_name") or "user")[:30]
            text = (m.get("text") or "").strip()
            cmd = BUTTONS.get(text)
            if not cmd and text.startswith("/"):
                cmd = text.split()[0].split("@")[0].lower()
            if cmd:
                handle_command(cmd, chat, private, st, today)
                if str(chat) not in st["credited"]:  # подпись автора — только при первом обращении в чате
                    st["credited"][str(chat)] = True
                    say(chat, CREDIT)
        except Exception as e:
            print("update error:", e)
    return len(res)


# ───────────────────────── рассылка: второй бот пишет через бота расписания ─────────────────────────
# Владелец пишет второму боту (NOTIFY_TOKEN), выбирает чаты бота расписания,
# а само сообщение доставляется от имени бота расписания (его чаты и личные диалоги).
NOTIFY_TOKEN = os.environ.get("NOTIFY_TOKEN") or ""
NAPI = f"https://api.telegram.org/bot{NOTIFY_TOKEN}"
ADMIN_NAMES = {u.strip().lstrip("@").lower()
               for u in (os.environ.get("ADMIN_USERNAMES") or "Kavasaki86").split(",") if u.strip()}
ADMIN_IDS_ENV = {int(x) for x in (os.environ.get("ADMIN_IDS") or "").replace(" ", "").split(",")
                 if x.lstrip("-").isdigit()}
ICONS = {"private": "👤", "group": "👥", "supergroup": "👥", "channel": "📣"}
PAGE = 8
MEDIA = {"photo": ("sendPhoto", "photo"), "document": ("sendDocument", "document"),
         "video": ("sendVideo", "video"), "animation": ("sendAnimation", "animation")}


def tg_n(method, **data):
    try:
        return httpx.post(f"{NAPI}/{method}", data=data, timeout=60).json()
    except Exception as e:
        print("notify tg error:", method, e)
        return {"ok": False, "description": str(e)}


def n_say(chat, text, markup=None, reply_to=None):
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        data["reply_markup"] = json.dumps(markup)
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return tg_n("sendMessage", **data)


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chat_title(chat: dict) -> str:
    if chat.get("title"):
        return chat["title"]
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    if chat.get("username"):
        name = f"{name} (@{chat['username']})".strip()
    return name or str(chat["id"])


def is_admin(st, user) -> bool:
    uid = (user or {}).get("id")
    if not uid:
        return False
    if uid in st["admin_ids"] or uid in ADMIN_IDS_ENV:
        return True
    if ((user.get("username") or "").lower()) in ADMIN_NAMES:
        st["admin_ids"].append(uid)  # запоминаем id, чтобы не зависеть от смены ника
        return True
    return False


def known_chats(st) -> dict:
    """Все чаты бота расписания: {chat_id: {title, type}}."""
    out = {}
    for cid in set(st["subs"]) | set(st["titles"]) | set(st["groups"]):
        t = st["titles"].get(cid, {})
        typ = t.get("type") or ("private" if int(cid) > 0 else "group")
        out[cid] = {"title": t.get("title") or f"чат {cid}", "type": typ}
    return out


def n_keyboard(st):
    d = st["draft"]
    chats = known_chats(st)
    sel = set(d["sel"])
    items = sorted(chats.items(), key=lambda kv: (kv[1]["type"] == "private", kv[1]["title"].lower()))
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = d["page"] = max(0, min(d.get("page", 0), pages - 1))
    rows = [[{"text": "☑️ Все", "callback_data": "n:all"}, {"text": "👥 Группы", "callback_data": "n:grp"},
             {"text": "👤 Личные", "callback_data": "n:prv"}, {"text": "🧹", "callback_data": "n:clr"}]]
    for cid, c in items[page * PAGE:(page + 1) * PAGE]:
        mark = "✅" if cid in sel else "⬜"
        rows.append([{"text": f"{mark} {ICONS.get(c['type'], '💬')} {c['title'][:36]}",
                      "callback_data": "n:t:" + cid}])
    if pages > 1:
        rows.append([{"text": "◀", "callback_data": f"n:p:{max(0, page - 1)}"},
                     {"text": f"{page + 1}/{pages}", "callback_data": f"n:p:{page}"},
                     {"text": "▶", "callback_data": f"n:p:{min(pages - 1, page + 1)}"}])
    rows.append([{"text": f"📨 Отправить ({len(sel)})", "callback_data": "n:go"},
                 {"text": "❌ Отмена", "callback_data": "n:no"}])
    return {"inline_keyboard": rows}


def n_new_draft(st, owner_chat, m):
    if not any(k in m for k in ("text", *MEDIA)):
        n_say(owner_chat, "Такой тип сообщения пока не поддерживается. Пришли текст, фото, видео или файл.")
        return
    if not known_chats(st):
        n_say(owner_chat, "Пока нет ни одного чата: боту расписания ещё никто не писал.")
        return
    keep = {k: m[k] for k in ("text", "entities", "caption", "caption_entities", *MEDIA) if k in m}
    st["draft"] = {"msg": keep, "sel": [], "panel": None, "page": 0}
    r = n_say(owner_chat, "Кому отправить? Отметь чаты бота расписания и нажми «Отправить».",
              n_keyboard(st), reply_to=m["message_id"])
    if r.get("ok"):
        st["draft"]["panel"] = r["result"]["message_id"]


def n_fetch_media(m):
    for kind in MEDIA:
        if kind in m:
            obj = m[kind][-1] if kind == "photo" else m[kind]
            r = tg_n("getFile", file_id=obj["file_id"])
            if not r.get("ok"):
                return None
            path = r["result"]["file_path"]
            data = httpx.get(f"https://api.telegram.org/file/bot{NOTIFY_TOKEN}/{path}", timeout=120).content
            return kind, obj.get("file_name") or os.path.basename(path), data
    return None


def n_deliver(chat, m, media):
    """Отправка сообщения владельца в чат ОТ ИМЕНИ БОТА РАСПИСАНИЯ."""
    if media:
        kind, name, data = media
        method, field = MEDIA[kind]
        form = {"chat_id": chat}
        if m.get("caption"):
            form["caption"] = m["caption"]
            if m.get("caption_entities"):
                form["caption_entities"] = json.dumps(m["caption_entities"])
        try:
            return httpx.post(f"{API}/{method}", data=form, files={field: (name, data)}, timeout=120).json()
        except Exception as e:
            return {"ok": False, "description": str(e)}
    form = {"chat_id": chat, "text": m.get("text", "")}
    if m.get("entities"):
        form["entities"] = json.dumps(m["entities"])
    return tg("sendMessage", **form)


def n_do_send(st, msg):
    d = st["draft"]
    chats = known_chats(st)
    m = d["msg"]
    media = n_fetch_media(m) if any(k in m for k in MEDIA) else None
    ok, fails = 0, []
    for cid in d["sel"]:
        r = n_deliver(cid, m, media)
        if r.get("ok"):
            ok += 1
        else:
            desc = r.get("description", "ошибка")
            fails.append(f"• {esc(chats.get(cid, {}).get('title', cid))}: {esc(desc)}")
            if any(x in desc.lower() for x in ("kicked", "blocked", "chat not found", "deactivated")):
                st["subs"].pop(cid, None)
                st["titles"].pop(cid, None)
        time.sleep(0.06)
    text = f"📨 Отправлено: <b>{ok}</b> из {len(d['sel'])}"
    if fails:
        text += "\n\nНе удалось:\n" + "\n".join(fails[:15])
    tg_n("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=text, parse_mode="HTML")
    st["draft"] = None


def n_handle_cb(st, cb):
    if not is_admin(st, cb.get("from")):
        tg_n("answerCallbackQuery", callback_query_id=cb["id"], text="Только для админа")
        return
    d, msg = st["draft"], cb.get("message")
    if not d or not msg or d.get("panel") != msg["message_id"]:
        tg_n("answerCallbackQuery", callback_query_id=cb["id"], text="Устарело — пришли сообщение заново")
        return
    data = cb.get("data", "")[2:]
    chats = known_chats(st)
    if data.startswith("t:"):
        cid = data[2:]
        if cid in chats:
            d["sel"] = [c for c in d["sel"] if c != cid] if cid in d["sel"] else d["sel"] + [cid]
    elif data.startswith("p:"):
        d["page"] = int(data[2:])
    elif data == "all":
        d["sel"] = [] if len(d["sel"]) == len(chats) else list(chats)
    elif data == "grp":
        d["sel"] = [c for c, v in chats.items() if v["type"] != "private"]
    elif data == "prv":
        d["sel"] = [c for c, v in chats.items() if v["type"] == "private"]
    elif data == "clr":
        d["sel"] = []
    elif data == "no":
        st["draft"] = None
        tg_n("answerCallbackQuery", callback_query_id=cb["id"], text="Отменено")
        tg_n("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text="❌ Отменено")
        return
    elif data == "go":
        if not d["sel"]:
            tg_n("answerCallbackQuery", callback_query_id=cb["id"], text="Выбери хотя бы один чат")
            return
        tg_n("answerCallbackQuery", callback_query_id=cb["id"], text="Отправляю…")
        n_do_send(st, msg)
        return
    tg_n("answerCallbackQuery", callback_query_id=cb["id"])
    tg_n("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
         reply_markup=json.dumps(n_keyboard(st)))


def n_handle_message(st, m):
    chat = m["chat"]
    if chat["type"] != "private":
        return
    cid, user = chat["id"], m.get("from") or {}
    text = (m.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    admin = is_admin(st, user)
    if cmd in ("/start", "/id", "/help"):
        if admin:
            n_say(cid, "👋 Это бот рассылки. Напиши мне сообщение (текст, фото, видео, файл), "
                       "выбери чаты бота расписания — и оно уйдёт туда <b>от имени бота расписания</b>.\n\n"
                       "/chats — сколько чатов\n/cancel — отмена\n"
                       f"Твой id: <code>{user.get('id')}</code>")
        else:
            n_say(cid, f"Это служебный бот. Твой id: <code>{user.get('id')}</code>")
        return
    if not admin:
        return
    if cmd == "/cancel":
        st["draft"] = None
        n_say(cid, "Отменено.")
    elif cmd == "/chats":
        ch = known_chats(st)
        groups = sum(1 for v in ch.values() if v["type"] != "private")
        n_say(cid, f"Чатов бота расписания: <b>{len(ch)}</b> (групп: {groups}, личных: {len(ch) - groups})")
    elif cmd:
        n_say(cid, "Не знаю такую команду. Просто напиши сообщение для рассылки.")
    else:
        n_new_draft(st, cid, m)


def n_setup():
    tg_n("deleteWebhook")
    tg_n("setMyCommands", commands=json.dumps([
        {"command": "chats", "description": "Сколько чатов"},
        {"command": "cancel", "description": "Отменить рассылку"},
        {"command": "id", "description": "Мой id"}]))


def process_n_updates(st, wait):
    r = tg_n("getUpdates", offset=st["n_offset"], timeout=wait,
             allowed_updates=json.dumps(["message", "callback_query"]))
    if not r.get("ok"):
        print("notify getUpdates error:", r)
        time.sleep(5)
        return 0
    res = r.get("result", [])
    for u in res:
        st["n_offset"] = u["update_id"] + 1
        try:
            if u.get("callback_query"):
                n_handle_cb(st, u["callback_query"])
            elif u.get("message"):
                n_handle_message(st, u["message"])
        except Exception as e:
            print("notify update error:", e)
    return len(res)


# ───────────────────────── автоматика (все подписанные чаты) ─────────────────────────
def auto_posts(st, now):
    if st.get("hv") != 3:
        st["hashes"] = {}
        st["hv"] = 3
    today = now.date()
    tomorrow = today + timedelta(days=1)
    by_group = {}
    for c, on in st["subs"].items():
        if on:
            by_group.setdefault(group_of(st, c), []).append(int(c))
    weather = None
    for group, chats in by_group.items():
        try:
            data = fetch(today, group)
        except Exception as e:
            print("fetch error:", group, e)
            continue
        # изменения расписания на 3 дня
        for i in range(3):
            day = today + timedelta(days=i)
            key = f"{group}|{day.isoformat()}"
            blocks = data.get(day.isoformat(), [])
            h = hashlib.md5(json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = st["hashes"].get(key)
            if old is not None and old != h and blocks:
                for c in chats:
                    send_day(c, day, data, group, new=True)
            st["hashes"][key] = h
        for c in chats:
            # утро
            if now.weekday() != 6 and 6 <= now.hour < 12 and (now.hour, now.minute) >= (6, 50) \
                    and st["sent"].get(f"{c}|m") != today.isoformat():
                st["sent"][f"{c}|m"] = today.isoformat()
                weather = weather or weather_text()
                say(c, f"🌅 <b>ДОБРОЕ УТРО! {group}</b>\n\n"
                       f"📅 <b>Сегодня: {now:%d.%m.%Y} ({DAYS[now.weekday()]})</b>\n"
                       f"⏰ <b>Время: {now:%H:%M}</b>\n\n" + weather)
                send_day(c, today, data, group)
            # вечер
            if now.hour >= 19 and tomorrow.weekday() != 6 and st["sent"].get(f"{c}|e") != today.isoformat():
                st["sent"][f"{c}|e"] = today.isoformat()
                send_day(c, tomorrow, data, group)
    for k in [k for k in st["hashes"] if k.split("|")[-1] < today.isoformat()]:
        del st["hashes"][k]


def main():
    st = load_state()
    started = time.time()
    try:
        now = datetime.now(TZ)
        if not st.get("cmds_set"):
            tg("setMyCommands", commands=json.dumps([
                {"command": "today", "description": "Расписание на сегодня"},
                {"command": "tomorrow", "description": "Расписание на завтра"},
                {"command": "r", "description": "Расписание на 3 дня"},
                {"command": "weather", "description": "Погода в Югорске"},
                {"command": "group", "description": "Сменить группу"},
                {"command": "notify", "description": "Уведомления вкл/выкл"},
                {"command": "all", "description": "Позвать всех"}]))
            st["cmds_set"] = True
        tg("deleteWebhook")
        if NOTIFY_TOKEN:
            n_setup()
        try:
            auto_posts(st, now)
        except Exception as e:
            print("auto error:", e)
        save_state(st)
        last_auto = time.time()
        while time.time() - started < RUN_SECONDS:
            wait = 5 if NOTIFY_TOKEN else 20  # два бота опрашиваем по очереди
            got = process_updates(st, datetime.now(TZ).date(), wait=wait)
            if NOTIFY_TOKEN:
                got += process_n_updates(st, wait=wait)
            if got:
                save_state(st)
            if time.time() - last_auto > 300:  # раз в 5 минут: утро/вечер/изменения
                try:
                    auto_posts(st, datetime.now(TZ))
                except Exception as e:
                    print("auto error:", e)
                save_state(st)
                last_auto = time.time()
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
