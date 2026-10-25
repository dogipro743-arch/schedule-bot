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

Переменные: BOT_TOKEN (обязательно), GROUP (необязательно, по умолчанию НК-24-3)
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
RUN_SECONDS = 250  # сколько один запуск слушает Telegram

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
    for k, v in (("hashes", {}), ("offset", 0), ("groups", {}), ("chat_users", {}), ("subs", {}), ("sent", {})):
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
    """Строки таблицы -> блоки «Пара N» (по два урока), обед отдельно."""
    rows = sorted(rows, key=lambda r: r["n"])
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
def font(size):
    for p in [os.environ.get("FONT_PATH", ""), "DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        if p:
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def render(day: date, blocks, group: str) -> io.BytesIO:
    W, mx = 900, 40
    BLUE, BG, LUNCH = (82, 148, 211), (238, 245, 252), (242, 192, 64)
    DARK, LINE, TEXT, GREY, PAIR = (40, 40, 44), (60, 60, 70), (20, 20, 20), (110, 110, 120), (80, 150, 210)
    f_title, f_sub, f_main, f_small = font(30), font(26), font(22), font(17)
    img = Image.new("RGB", (W, 5000), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 120], fill=BLUE)
    d.text((mx, 24), f"РАСПИСАНИЕ - {day:%d.%m.%Y}", font=f_title, fill="white")
    d.text((mx, 70), f"{DAYS[day.weekday()]} | {group}", font=f_sub, fill=(225, 238, 250))
    y = 145
    for b in blocks:
        if "lunch" in b:
            l = b["lunch"]
            d.rectangle([mx, y, W - mx, y + 64], fill=LUNCH)
            d.text((mx + 40, y + 8), f"{l['n']} ({l['time']})", font=f_main, fill=TEXT)
            d.text((mx + 40, y + 34), "Обед", font=f_main, fill=TEXT)
            y += 80
            continue
        rows = b["rows"]
        h = 28 + len(rows) * 78
        d.rectangle([mx, y, W - mx, y + h], outline=LINE, width=3, fill=(244, 249, 254))
        d.text((mx + 50, y + 6), b["title"], font=f_small, fill=PAIR)
        ry = y + 30
        for i, r in enumerate(rows):
            d.text((mx + 50, ry + 4), f"{r['n']} ({r['time']})", font=f_main, fill=TEXT)
            d.text((mx + 50, ry + 36), r["name"], font=f_main, fill=TEXT)
            d.text((W * 0.62, ry + 10), f"Преп: {r['teacher']}", font=f_small, fill=GREY)
            d.text((W * 0.62, ry + 40), f"Каб: {r['room']}", font=f_small, fill=GREY)
            ry += 78
            if i < len(rows) - 1:
                d.line([mx, ry - 4, W - mx, ry - 4], fill=LINE, width=2)
        y += h + 16
    y += 10
    d.rectangle([0, y, W, y + 56], fill=DARK)
    d.text((mx, y + 18), f"{group} | РАСПИСАНИЕ", font=f_small, fill=(190, 190, 195))
    d.text((W // 2 - 20, y + 18), f"{datetime.now(TZ):%H:%M}", font=f_small, fill=(150, 150, 155))
    img = img.crop((0, 0, W, y + 56))
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
    res = tg("getUpdates", offset=st["offset"], timeout=wait).get("result", [])
    for u in res:
        st["offset"] = u["update_id"] + 1
        try:
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
            frm = m.get("from") or {}
            if not private and frm and not frm.get("is_bot"):
                st["chat_users"].setdefault(str(chat), {})[str(frm["id"])] = (frm.get("first_name") or "user")[:30]
            text = (m.get("text") or "").strip()
            cmd = BUTTONS.get(text)
            if not cmd and text.startswith("/"):
                cmd = text.split()[0].split("@")[0].lower()
            if cmd:
                handle_command(cmd, chat, private, st, today)
        except Exception as e:
            print("update error:", e)
    return len(res)


# ───────────────────────── автоматика (все подписанные чаты) ─────────────────────────
def auto_posts(st, now):
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
        try:
            auto_posts(st, now)
        except Exception as e:
            print("auto error:", e)
        save_state(st)
        while time.time() - started < RUN_SECONDS:
            if process_updates(st, datetime.now(TZ).date(), wait=20):
                save_state(st)
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
