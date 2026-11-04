"""
Бот расписания ЮПК для Telegram. Работает в GitHub Actions (сервер не нужен).

Что умеет:
  • Понимает вопросы обычными словами («что завтра?», «где химия?»), темы картинки, запасной режим при падении сайта,
    личные сообщения админу об ошибках
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
import traceback
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
                         [{"text": "🔁 Сменить группу"}, {"text": "🔔 Уведомления"}],
                         [{"text": "⚙️ Настройки"}]],
            "resize_keyboard": True}
BUTTONS = {"📅 Сегодня": "/today", "📆 Завтра": "/tomorrow", "🗓 3 дня": "/r",
           "🌡 Погода": "/weather", "🔁 Сменить группу": "/group", "🔔 Уведомления": "/notify", "⚙️ Настройки": "/settings"}


# ───────────────────────── состояние ─────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in (("hashes", {}), ("offset", 0), ("groups", {}), ("chat_users", {}), ("subs", {}), ("sent", {}), ("credited", {}),
                 ("titles", {}), ("n_offset", 0), ("admin_ids", []), ("draft", None), ("subgroup", {}),
                 ("theme", {}), ("last", {}), ("health", {}), ("err_sent", {}), ("me", ""), ("hint", {}), ("notif", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def group_of(st, chat) -> str:
    return st["groups"].get(str(chat), GROUP)


_STATE = {"st": None}  # ссылка на состояние (для запасного режима и тем)


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


def build_blocks(rows, minutes=LESSON_MIN):
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
                    {"n": r["n"], "time": f"{add_min(r['start'], 0)} - {add_min(r['start'], minutes)}",
                     "name": r["name"], "teacher": r["teacher"], "room": r["room"]}
                    for r in sorted(g, key=lambda x: x["n"])]})
        seg = []

    for r in rows:
        if r["name"].strip().lower() == "обед":
            flush()
            blocks.append({"lunch": {"n": r["n"], "time": f"{add_min(r['start'], 0)} - {add_min(r['start'], minutes)}"}})
        else:
            seg.append(r)
    flush()
    return blocks


_cache = {}


def _fetch_site(day: date, group: str) -> dict:
    """{'2026-10-26': blocks, ...} — сайт отдаёт выбранный день + 2 следующих."""
    r = httpx.get(BASE, params={"teachername": 0, "group": group, "subgroup": 0, "kabinet": 0,
                                "date": day.isoformat()}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    if "Расписание ЮПК" not in r.text:
        raise RuntimeError("сайт вернул незнакомую страницу")
    soup = BeautifulSoup(r.text, "html.parser")
    rows_by_day, notes, cur = {}, {}, None
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "table"]):
        if el.name == "table":
            if cur:
                rows_by_day[cur].extend(parse_table(el))
            continue
        t = el.get_text(" ", strip=True)
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s*(.*)$", t)
        if m:
            cur = m.group(1)
            notes[cur] = m.group(2).strip()  # например «Уроки по 20 минут»
            rows_by_day.setdefault(cur, [])
    result = {}
    for d, rows in rows_by_day.items():
        note = notes.get(d, "")
        mm = re.search(r"(\d+)\s*мин", note)
        minutes = int(mm.group(1)) if mm else LESSON_MIN
        result[d] = ([{"note": note}] if note else []) + build_blocks(rows, minutes)
    return result


def save_last(st, group, result):
    """Запоминаем последнее удачное расписание группы — на случай, если сайт упадёт."""
    rec = st["last"].setdefault(group, {"ts": "", "data": {}})
    rec["data"].update({k: v for k, v in result.items() if not k.startswith("_")})
    rec["ts"] = datetime.now(TZ).isoformat(timespec="minutes")
    old = (datetime.now(TZ).date() - timedelta(days=3)).isoformat()
    for k in [k for k in rec["data"] if k < old]:
        del rec["data"][k]


def fetch(day: date, group: str) -> dict:
    """Расписание с сайта. Если сайт недоступен — последние сохранённые данные (ключ '_stale')."""
    key = (group, day.isoformat(), int(time.time() // 120))  # кэш на 2 минуты
    if key in _cache:
        return _cache[key]
    st = _STATE["st"]
    try:
        result = _fetch_site(day, group)
    except Exception as e:
        site_failed(st, e)
        rec = (st or {}).get("last", {}).get(group)
        if not rec or not rec.get("data"):
            raise
        result = dict(rec["data"])
        result["_stale"] = rec["ts"]
        _cache[key] = result
        return result
    site_ok(st)
    if st is not None:
        save_last(st, group, result)
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


THEMES = {
    "dark": dict(bg=(15, 17, 26), card=(26, 30, 44), line=(46, 52, 74), acc=(99, 102, 241), acc2=(56, 189, 248),
                 amber=(251, 191, 36), txt=(238, 240, 250), grey=(140, 146, 170), g1=(99, 102, 241), g2=(56, 189, 248),
                 hdr_txt=(235, 240, 255), lunch_fill=(46, 38, 12), lunch_txt=(251, 191, 36),
                 pill_fill=(255, 255, 255), pill_txt=(45, 45, 110), note_txt=(60, 44, 0),
                 foot=(140, 146, 170), upd=(95, 101, 125), stale_fill=(20, 24, 40), stale_txt=(252, 211, 77)),
    "light": dict(bg=(238, 241, 247), card=(255, 255, 255), line=(226, 230, 240), acc=(79, 70, 229), acc2=(37, 99, 235),
                  amber=(245, 158, 11), txt=(24, 28, 40), grey=(107, 114, 128), g1=(99, 102, 241), g2=(56, 189, 248),
                  hdr_txt=(240, 246, 255), lunch_fill=(254, 243, 199), lunch_txt=(146, 64, 14),
                  pill_fill=(255, 255, 255), pill_txt=(45, 45, 110), note_txt=(60, 44, 0),
                  foot=(120, 126, 145), upd=(160, 165, 180), stale_fill=(255, 255, 255), stale_txt=(180, 83, 9)),
    "color": dict(bg=(32, 14, 64), card=(58, 32, 104), line=(98, 66, 160), acc=(244, 114, 182), acc2=(253, 224, 71),
                  amber=(251, 191, 36), txt=(252, 247, 255), grey=(205, 190, 250), g1=(236, 72, 153), g2=(249, 115, 22),
                  hdr_txt=(255, 240, 250), lunch_fill=(92, 58, 8), lunch_txt=(253, 224, 71),
                  pill_fill=(255, 255, 255), pill_txt=(120, 30, 90), note_txt=(60, 44, 0),
                  foot=(205, 190, 250), upd=(150, 130, 210), stale_fill=(30, 10, 60), stale_txt=(253, 224, 71)),
}
THEME_NAMES = {"dark": "Тёмная", "light": "Светлая", "color": "Цветная"}
THEME_TEXT = "🎨 <b>Тема картинки</b>\nВыбери оформление расписания:"


def theme_of(st, chat) -> str:
    t = st.get("theme", {}).get(str(chat), "dark")
    return t if t in THEMES else "dark"


def stale_label(ts) -> str:
    try:
        t = datetime.fromisoformat(ts)
        if t.date() == datetime.now(TZ).date():
            return f"данные от {t:%H:%M}"
        return f"данные от {t:%d.%m %H:%M}"
    except Exception:
        return "сохранённые данные"


def render(day: date, blocks, group: str, theme: str = "dark", stale=None) -> io.BytesIO:
    """Карточки с цветной шапкой. Тема: dark / light / color. stale — метка времени, если данные не свежие."""
    P = THEMES.get(theme, THEMES["dark"])
    W, mx = 900, 36
    f_big, f_med = font(42, True), font(26)
    f_sub, f_small, f_tag = font(25, True), font(19), font(17, True)

    img = Image.new("RGB", (W, 8000), P["bg"])
    d = ImageDraw.Draw(img)

    HH = 190
    for x in range(W):
        t = x / W
        d.line([x, 0, x, HH], fill=tuple(int(P["g1"][i] + (P["g2"][i] - P["g1"][i]) * t) for i in range(3)))
    d.text((mx, 34), "Расписание", font=f_big, fill="white")
    d.text((mx, 100), f"{DAYS[day.weekday()]}, {day:%d.%m.%Y}", font=f_med, fill=P["hdr_txt"])
    pw = int(d.textlength(group, font=f_sub)) + 44
    d.rounded_rectangle([W - mx - pw, 38, W - mx, 86], radius=24, fill=P["pill_fill"])
    d.text((W - mx - pw + 22, 50), group, font=f_sub, fill=P["pill_txt"])

    note = next((b["note"] for b in blocks if "note" in b), "")
    if note:
        nw = int(d.textlength(note, font=f_tag)) + 36
        d.rounded_rectangle([mx, 142, mx + nw, 176], radius=17, fill=P["amber"])
        d.text((mx + 18, 149), note, font=f_tag, fill=P["note_txt"])
    if stale:
        label = stale_label(stale)
        sw = int(d.textlength(label, font=f_tag)) + 36
        d.rounded_rectangle([W - mx - sw, 142, W - mx, 176], radius=17, fill=P["stale_fill"])
        d.text((W - mx - sw + 18, 149), label, font=f_tag, fill=P["stale_txt"])

    y = HH + 28
    for b in blocks:
        if "note" in b:
            continue
        if "lunch" in b:
            l = b["lunch"]
            d.rounded_rectangle([mx, y, W - mx, y + 78], radius=22, fill=P["lunch_fill"], outline=P["amber"], width=2)
            d.text((mx + 28, y + 22), "Обед", font=f_sub, fill=P["lunch_txt"])
            tw = d.textlength(l["time"], font=f_small)
            d.text((W - mx - 28 - tw, y + 26), l["time"], font=f_small, fill=P["lunch_txt"])
            y += 98
            continue

        rows = b["rows"]
        h = 56 + len(rows) * 100 + 8
        d.rounded_rectangle([mx, y, W - mx, y + h], radius=22, fill=P["card"])
        d.text((mx + 28, y + 18), b["title"].upper(), font=f_tag, fill=P["acc2"])
        ry = y + 52
        for i, r in enumerate(rows):
            start, end = r["time"].split(" - ")
            is_lunch = r["name"].strip().lower().startswith("обед")
            d.text((mx + 28, ry + 8), start, font=f_sub, fill=P["txt"])
            d.text((mx + 28, ry + 44), end, font=f_small, fill=P["grey"])
            d.rounded_rectangle([mx + 132, ry + 10, mx + 138, ry + 80], radius=3,
                                fill=P["amber"] if is_lunch else P["acc"])
            tag = re.search(r"\((I|II)\)", r["name"])
            name = re.sub(r"\s*\((I|II)\)", "", r["name"]).strip()
            maxw = (W - mx - 28) - (mx + 160) - (120 if tag else 0)
            d.text((mx + 160, ry + 10), fit(d, name, f_sub, maxw), font=f_sub, fill=P["txt"])
            info = "" if is_lunch else (f"{r['teacher']}  •  каб. {r['room']}" if r["teacher"] else f"каб. {r['room']}")
            d.text((mx + 160, ry + 50), fit(d, info, f_small, (W - mx - 28) - (mx + 160)), font=f_small, fill=P["grey"])
            if tag:
                label = f"подгр. {tag.group(1)}"
                lw = int(d.textlength(label, font=f_tag)) + 28
                d.rounded_rectangle([W - mx - 28 - lw, ry + 12, W - mx - 28, ry + 42], radius=15,
                                    outline=P["acc"], width=2)
                d.text((W - mx - 28 - lw + 14, ry + 17), label, font=f_tag, fill=P["acc"])
            ry += 100
            if i < len(rows) - 1:
                d.line([mx + 28, ry - 10, W - mx - 28, ry - 10], fill=P["line"], width=2)
        y += h + 20

    y += 6
    foot = "ЮПК  •  Создатель бота @Kavasaki86"
    d.text(((W - d.textlength(foot, font=f_small)) / 2, y), foot, font=f_small, fill=P["foot"])
    upd = f"обновлено {datetime.now(TZ):%H:%M}"
    d.text(((W - d.textlength(upd, font=f_tag)) / 2, y + 30), upd, font=f_tag, fill=P["upd"])
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


SUB_NAMES = {"all": "Общее (все подгруппы вместе)", "I": "Только 1 подгруппа", "II": "Только 2 подгруппа",
             "split": "Отдельно по подгруппам (2 картинки)"}
SUB_RE = re.compile(r"\((I|II)\)")


def sub_of(st, chat) -> str:
    return st["subgroup"].get(str(chat), "all")


def filter_blocks(blocks, sub):
    """Оставляет занятия выбранной подгруппы (и общие без пометки), пары перенумеровываются."""
    if sub not in ("I", "II"):  # «all» и «split» — без фильтра
        return blocks
    out, renum, n = [], {}, 0
    for b in blocks:
        if "lunch" in b or "note" in b:
            out.append(b)
            continue
        rows = []
        for r in b["rows"]:
            tag = SUB_RE.search(r["name"])
            if not tag or tag.group(1) == sub:
                rows.append(r)
        if not rows:
            continue
        if b["title"] not in renum:
            n += 1
            renum[b["title"]] = n
        out.append({"title": f"Пара {renum[b['title']]}", "rows": rows})
    return out if any("rows" in b for b in out) else []


def has_lessons(blocks) -> bool:
    return any("rows" in b for b in blocks)


def send_day(chat, day: date, data: dict, group: str, new=False, sub="all"):
    if sub == "split":  # отдельно по подгруппам: две картинки (если в этот день есть деление на подгруппы)
        allb = data.get(day.isoformat()) or []
        divided = any(SUB_RE.search(r["name"]) for b in allb for r in b.get("rows", []))
        if divided and has_lessons(allb):
            for i, s_ in enumerate(("I", "II")):
                send_day(chat, day, data, group, new and i == 0, s_)
            return
        sub = "all"
    blocks = filter_blocks(data.get(day.isoformat()) or [], sub)
    label = group if sub == "all" else f"{group} · {sub} подгр."
    stale = data.get("_stale")
    if not has_lessons(blocks):
        extra = (f"\n\n⚠️ Сайт сейчас недоступен ({stale_label(stale)}), на эту дату сохранённых данных нет."
                 if stale else "")
        say(chat, f"{label}: на {day:%d.%m.%Y} ({DAYS[day.weekday()]}) расписания нет — "
                  f"оно ещё не загружено или занятий нет.{extra}")
        return
    head = "🔔 <b>ВНИМАНИЕ! НОВОЕ РАСПИСАНИЕ!</b>\n\n" if new else ""
    cap = f"{head}📅 <b>{day:%d.%m.%Y} ({DAYS[day.weekday()]})\n{label}</b>"
    if stale:
        cap += f"\n⚠️ Сайт недоступен, {stale_label(stale)}"
    theme = theme_of(_STATE["st"] or {}, chat)
    httpx.post(f"{API}/sendPhoto", data={"chat_id": chat, "caption": cap, "parse_mode": "HTML"},
               files={"photo": ("schedule.png", render(day, blocks, label, theme, stale), "image/png")}, timeout=60)


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
# ───────────────────────── настройки ─────────────────────────
NOTIF_TYPES = {"morning": "☀️ Утреннее расписание", "evening": "🌙 Расписание на завтра",
               "changes": "🔔 Об изменениях в расписании"}
NOTIF_SHORT = {"morning": "утро", "evening": "вечер", "changes": "изменения"}


def master_on(st, chat, private=False) -> bool:
    return st["subs"].get(str(chat), not private)


def notif_on(st, chat, kind) -> bool:
    """Включено ли уведомление этого вида. Сообщения от админа сюда не относятся — они приходят всегда."""
    return bool(st["subs"].get(str(chat), False)) and st["notif"].get(str(chat), {}).get(kind, True)


def notif_summary(st, chat, private) -> str:
    if not master_on(st, chat, private):
        return "выключены"
    on = [NOTIF_SHORT[k] for k in NOTIF_TYPES if st["notif"].get(str(chat), {}).get(k, True)]
    return "все" if len(on) == len(NOTIF_TYPES) else (", ".join(on) if on else "выключены")


def notif_text(st, chat, private) -> str:
    head = ("🔕 Сейчас все уведомления <b>выключены</b>." if not master_on(st, chat, private)
            else "Выбери, какие уведомления присылать:")
    return f"🔔 <b>Уведомления</b>\n\n{head}\n\n📢 Сообщения от администратора бота отключить нельзя — они приходят всегда."


def notif_markup(st, chat, private):
    master = master_on(st, chat, private)
    n = st["notif"].get(str(chat), {})
    rows = [[{"text": ("✅ " if master and n.get(k, True) else "⬜ ") + v, "callback_data": "s:nt:" + k}]
            for k, v in NOTIF_TYPES.items()]
    rows.append([{"text": "🔕 Выключить все уведомления" if master else "🔔 Включить все уведомления",
                  "callback_data": "s:nall"}])
    rows.append([{"text": "◀ Назад", "callback_data": "s:back"}])
    return {"inline_keyboard": rows}


def toggle_notif(st, chat, kind, private):
    cid = str(chat)
    n = st["notif"].setdefault(cid, {})
    if not master_on(st, chat, private):  # всё было выключено — включаем только выбранное
        for k in NOTIF_TYPES:
            n[k] = False
        n[kind] = True
        st["subs"][cid] = True
        return
    n[kind] = not n.get(kind, True)
    if not any(n.get(k, True) for k in NOTIF_TYPES):  # выключили последнее
        st["subs"][cid] = False
        for k in NOTIF_TYPES:
            n[k] = True


def toggle_all_notif(st, chat, private):
    cid = str(chat)
    if master_on(st, chat, private):
        st["subs"][cid] = False
    else:
        st["subs"][cid] = True
        st["notif"][cid] = {k: True for k in NOTIF_TYPES}


def settings_text(st, chat, private):
    return ("⚙️ <b>Настройки</b>\n\n"
            f"👥 Группа: <b>{group_of(st, chat)}</b>\n"
            f"🧩 Вид расписания: <b>{SUB_NAMES[sub_of(st, chat)]}</b>\n"
            f"🎨 Тема картинки: <b>{THEME_NAMES[theme_of(st, chat)]}</b>\n"
            f"🔔 Уведомления: <b>{notif_summary(st, chat, private)}</b>")


def settings_markup(st, chat, private):
    return {"inline_keyboard": [
        [{"text": "🧩 Вид расписания / подгруппа", "callback_data": "s:sub"}],
        [{"text": "🎨 Тема картинки", "callback_data": "s:theme"}],
        [{"text": "🔁 Сменить группу", "callback_data": "s:grp"}],
        [{"text": "🔔 Уведомления", "callback_data": "s:n"}]]}


SUB_TEXT = ("🧩 <b>Вид расписания</b>\nОбщее показывает всех вместе, «только 1/2 подгруппа» — одну подгруппу, "
            "«отдельно» присылает две картинки: для 1 и для 2 подгруппы.")


def sub_markup(st, chat):
    cur = sub_of(st, chat)
    rows = [[{"text": ("✅ " if cur == k else "") + v, "callback_data": "s:set:" + k}]
            for k, v in SUB_NAMES.items()]
    rows.append([{"text": "◀ Назад", "callback_data": "s:back"}])
    return {"inline_keyboard": rows}


def theme_markup(st, chat):
    cur = theme_of(st, chat)
    rows = [[{"text": ("✅ " if cur == k else "") + v, "callback_data": "s:tset:" + k}]
            for k, v in THEME_NAMES.items()]
    rows.append([{"text": "👁 Показать пример", "callback_data": "s:preview"}])
    rows.append([{"text": "◀ Назад", "callback_data": "s:back"}])
    return {"inline_keyboard": rows}


def edit_msg(chat, mid, text, markup):
    tg("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
       reply_markup=json.dumps(markup))


def handle_settings_cb(act, chat, private, mid, st, cbid):
    toast = None
    if act == "sub":
        edit_msg(chat, mid, SUB_TEXT, sub_markup(st, chat))
    elif act.startswith("set:") and act[4:] in SUB_NAMES:
        st["subgroup"][str(chat)] = act[4:]
        toast = f"Готово: {SUB_NAMES[act[4:]]}"
        edit_msg(chat, mid, SUB_TEXT, sub_markup(st, chat))
    elif act == "theme":
        edit_msg(chat, mid, THEME_TEXT, theme_markup(st, chat))
    elif act.startswith("tset:") and act[5:] in THEME_NAMES:
        st["theme"][str(chat)] = act[5:]
        toast = f"Тема: {THEME_NAMES[act[5:]]}"
        edit_msg(chat, mid, THEME_TEXT, theme_markup(st, chat))
    elif act == "preview":
        toast = "Отправляю пример…"
        try:
            today = datetime.now(TZ).date()
            send_day(chat, today, fetch(today, group_of(st, chat)), group_of(st, chat), sub=sub_of(st, chat))
        except Exception:
            say(chat, "Не получилось загрузить расписание для примера, попробуй позже.")
    elif act == "back":
        edit_msg(chat, mid, settings_text(st, chat, private), settings_markup(st, chat, private))
    elif act in ("n", "notify"):  # «notify» — для старых сообщений с кнопкой
        edit_msg(chat, mid, notif_text(st, chat, private), notif_markup(st, chat, private))
    elif act.startswith("nt:") and act[3:] in NOTIF_TYPES:
        toggle_notif(st, chat, act[3:], private)
        edit_msg(chat, mid, notif_text(st, chat, private), notif_markup(st, chat, private))
    elif act == "nall":
        toggle_all_notif(st, chat, private)
        toast = "Все уведомления выключены" if not master_on(st, chat, private) else "Уведомления включены"
        edit_msg(chat, mid, notif_text(st, chat, private), notif_markup(st, chat, private))
    elif act == "grp":
        group_picker(chat)
    tg("answerCallbackQuery", callback_query_id=cbid, **({"text": toast} if toast else {}))


def handle_command(cmd, chat, private, st, today):
    group = group_of(st, chat)
    kb = KEYBOARD if private else None
    if cmd == "/start":
        say(chat, f"Привет! Я показываю расписание ЮПК картинкой.\nТекущая группа: <b>{group}</b>\n\n"
                  "Можно просто спросить: «что завтра?», «когда первая пара в среду?», «где химия?»", kb)
        if private and str(chat) not in st["groups"]:
            group_picker(chat)
    elif cmd == "/group":
        group_picker(chat)
    elif cmd == "/help":
        say(chat, NL_HELP, kb)
    elif cmd == "/settings":
        say(chat, settings_text(st, chat, private), settings_markup(st, chat, private))
        if private:
            say(chat, "Кнопки внизу обновлены 👇", KEYBOARD)
    elif cmd == "/subgroup":
        say(chat, SUB_TEXT, sub_markup(st, chat))
    elif cmd in ("/today", "/tomorrow", "/r"):
        try:
            data = fetch(today, group)
        except Exception:
            say(chat, "😕 Сайт расписания сейчас не отвечает, а сохранённых данных у меня пока нет. "
                      "Попробуй чуть позже.", kb)
            return
        sub = sub_of(st, chat)
        if cmd == "/today":
            send_day(chat, today, data, group, sub=sub)
        elif cmd == "/tomorrow":
            send_day(chat, today + timedelta(days=1), data, group, sub=sub)
        else:
            for i in range(3):
                send_day(chat, today + timedelta(days=i), data, group, sub=sub)
    elif cmd == "/notify":
        say(chat, notif_text(st, chat, private), notif_markup(st, chat, private))
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
        report_error(st, "getUpdates", f"Telegram не отдаёт обновления бота расписания: {r}")
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
                if data.startswith("s:"):
                    handle_settings_cb(data[2:], chat, private, cb["message"]["message_id"], st, cb["id"])
                elif data.startswith("g:") and data[2:] in GROUPS:
                    st["groups"][str(chat)] = data[2:]
                    tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Группа: {data[2:]}")
                    say(chat, f"Готово! Группа: <b>{data[2:]}</b>", KEYBOARD if private else None)
                    if str(chat) not in st["subgroup"]:
                        say(chat, SUB_TEXT, sub_markup(st, chat))
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
            handled = False
            if cmd:
                handle_command(cmd, chat, private, st, today)
                handled = True
            elif text and (private or addressed(m, st)):
                handled = handle_text(strip_address(text, st.get("me", "")), chat, private, st, today)
            if handled and str(chat) not in st["credited"]:  # подпись автора — только при первом обращении в чате
                st["credited"][str(chat)] = True
                say(chat, CREDIT)
        except Exception as e:
            report_error(st, "update", "Ошибка при обработке сообщения: " + short_exc(e))
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
        report_error(st, "n_getUpdates", f"Telegram не отдаёт обновления бота рассылки: {r}")
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
            report_error(st, "n_update", "Ошибка в боте рассылки: " + short_exc(e))
    return len(res)


# ───────────────────────── вопросы обычным текстом ─────────────────────────
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
WD_STEMS = [("понедел", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5), ("воскресен", 6)]
WD_SHORT = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
ALIASES = [("физик", "физика"), ("физкульт", "физкульт"), ("физр", "физкульт"), ("физ", "физкульт"),
           ("матем", "математ"), ("матеш", "математ"), ("русск", "русяз"), ("иностран", "иняз"),
           ("английск", "иняз"), ("обществ", "обществ"), ("информат", "информат"), ("литерат", "литер")]
NL_HELP = ("Спроси обычными словами, например:\n"
           "• «что завтра?» или «расписание на пятницу»\n"
           "• «когда первая пара в среду?»\n"
           "• «во сколько заканчиваются пары?»\n"
           "• «где химия?» или «кто ведёт историю?»\n"
           "• «какая следующая пара?»\n"
           "• «сколько пар в пятницу?»\n\n"
           "Команды тоже работают: /today, /tomorrow, /settings")


def norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def plural(n: int, a: str, b: str, c: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return a
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return b
    return c


def parse_date(t: str, today: date):
    """Находит в тексте дату: сегодня/завтра, день недели, 03.11, «3 ноября»."""
    words = re.findall(r"[a-zа-я]+", t)
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    if "вчера" in t:
        return today - timedelta(days=1)
    if "сегодня" in t:
        return today
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")", t)
    if m:
        try:
            return date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})\b", t)
    if m:
        try:
            return date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    for w in words:
        wd = WD_SHORT.get(w)
        if wd is None:
            wd = next((v for k, v in WD_STEMS if w.startswith(k)), None)
        if wd is not None:
            return today + timedelta(days=(wd - today.weekday()) % 7)
    return None


def day_title(d: date, today: date) -> str:
    delta = (d - today).days
    pre = {0: "Сегодня", 1: "Завтра", 2: "Послезавтра", -1: "Вчера"}.get(delta)
    short = f"{DAYS_SHORT[d.weekday()]}, {d:%d.%m}"
    return f"{pre} ({short})" if pre else f"{DAYS[d.weekday()]}, {d:%d.%m}"


def hm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def clean_name(name: str) -> str:
    return SUB_RE.sub("", name).strip()


def day_lessons(data, d: date, sub: str) -> list:
    out = []
    for b in filter_blocks(data.get(d.isoformat()) or [], sub):
        for r in b.get("rows", []):
            if r["name"].strip().lower().startswith("обед"):
                continue
            s, e = r["time"].split(" - ")
            out.append({**r, "start": s, "end": e, "pair": b["title"]})
    out.sort(key=lambda r: (hm(r["start"]), r["name"]))
    return out


def lesson_line(l) -> str:
    s = f"{l['start']}–{l['end']} {l['name']}, каб. {l['room']}"
    return s + (f" ({l['teacher']})" if l["teacher"] else "")


def name_matches(word: str, name: str) -> bool:
    n = norm(clean_name(name))
    for key, val in ALIASES:
        if word.startswith(key):
            word = val
            break
    k = 0
    while k < min(len(word), len(n)) and word[k] == n[k]:
        k += 1
    return k >= 4 or (k >= 3 and k == len(n))


def stale_suffix(data) -> str:
    return f"\n\n⚠️ Сайт недоступен, {stale_label(data['_stale'])}." if data.get("_stale") else ""


def subject_answer(words, data, start: date, sub: str, today: date):
    cands = [w for w in words if len(w) >= 4]
    hits = []
    for i in range(3):
        d = start + timedelta(days=i)
        ls = [l for l in day_lessons(data, d, sub) if any(name_matches(w, l["name"]) for w in cands)]
        if ls:
            hits.append((d, ls))
    if not hits:
        return None
    lines = [f"🔎 <b>{esc(clean_name(hits[0][1][0]['name']))}</b>"]
    for d, ls in hits:
        rooms = list(dict.fromkeys(l["room"] for l in ls if l["room"]))
        teachers = list(dict.fromkeys(l["teacher"] for l in ls if l["teacher"]))
        lines.append(f"• <b>{day_title(d, today)}</b>: {ls[0]['start']}–{ls[-1]['end']}, каб. {', '.join(rooms)}"
                     + (f", {', '.join(teachers)}" if teachers else ""))
    return "\n".join(lines)


def next_answer(data, today: date, sub: str) -> str:
    now = datetime.now(TZ)
    nm = now.hour * 60 + now.minute
    ls = day_lessons(data, today, sub)
    cur = next((l for l in ls if hm(l["start"]) <= nm < hm(l["end"])), None)
    nxt = next((l for l in ls if hm(l["start"]) > nm), None)
    parts = []
    if cur:
        parts.append(f"▶️ Сейчас идёт: <b>{esc(cur['name'])}</b>, каб. {cur['room']} — до {cur['end']}")
    if nxt:
        wait = hm(nxt["start"]) - nm
        parts.append(f"⏭ Дальше: {esc(lesson_line(nxt))} — через {wait} мин")
    if not parts:
        for i in (1, 2):
            d = today + timedelta(days=i)
            nl = day_lessons(data, d, sub)
            if nl:
                parts.append("На сегодня всё. Ближайшая пара — "
                             f"<b>{day_title(d, today)}</b>: {esc(lesson_line(nl[0]))}")
                break
        else:
            parts.append("На сегодня всё, и в ближайшие дни занятий в расписании нет.")
    return "\n".join(parts)


def handle_text(text, chat, private, st, today) -> bool:
    """Понимает вопросы обычными словами. Возвращает True, если ответил."""
    t = norm(text)
    words = re.findall(r"[a-zа-я0-9]+", t)
    if not words:
        return False
    has = lambda *stems: any(w.startswith(s) for w in words for s in stems)
    kb = KEYBOARD if private else None
    group, sub = group_of(st, chat), sub_of(st, chat)

    if has("спасиб", "благодар"):
        say(chat, "Пожалуйста! 🙂", kb)
        return True
    if has("привет", "здравств", "хай", "добрый") and len(words) <= 3:
        say(chat, "Привет! 👋\n\n" + NL_HELP, kb)
        return True
    if has("помощ", "помоги", "умеешь", "команды"):
        say(chat, NL_HELP, kb)
        return True
    if has("погод", "температур", "мороз", "холодно"):
        say(chat, weather_text(), kb)
        return True

    day = parse_date(t, today)
    ask = has("расписан", "пар", "урок", "занят", "что", "как", "когда", "где", "кто", "кабинет", "аудитор",
              "сколько", "след", "ближайш", "начал", "начин", "закан", "конч", "домой", "перв", "последн",
              "сейчас") or day is not None
    if not ask:
        if private and time.time() - st["hint"].get(str(chat), 0) > 600:
            st["hint"][str(chat)] = time.time()
            say(chat, "Не совсем понял 🤔\n\n" + NL_HELP, kb)
            return True
        return False

    is_next = has("след", "ближайш") or "сейчас" in words
    base = today if is_next else (day or today)
    try:
        data = fetch(base, group)
    except Exception:
        say(chat, "😕 Сайт расписания сейчас не отвечает, а сохранённых данных у меня пока нет. "
                  "Попробуй чуть позже.", kb)
        return True
    d = day or today
    suffix = stale_suffix(data)

    if is_next and (has("пар", "урок", "занят", "что") or "сейчас" in words):
        say(chat, next_answer(data, today, sub) + suffix, kb)
        return True

    sa = subject_answer(words, data, d, sub, today)
    if sa:
        say(chat, sa + suffix, kb)
        return True

    ls = day_lessons(data, d, sub)
    title = day_title(d, today)
    first_q = (has("перв") and has("пар", "урок", "занят")) or has("начал", "начин")
    last_q = has("закан", "конч", "освобод", "домой", "последн")
    count_q = has("сколько") and has("пар", "урок", "занят")
    if first_q or last_q or count_q:
        if not ls:
            say(chat, f"📭 <b>{title}</b>: занятий нет (или расписание ещё не загружено).{suffix}", kb)
        elif first_q:
            l = ls[0]
            say(chat, f"🕗 <b>{title}</b>\nПервая пара в <b>{l['start']}</b>: {esc(l['name'])}, "
                      f"каб. {l['room']}" + (f" ({esc(l['teacher'])})" if l["teacher"] else "") + suffix, kb)
        elif last_q:
            l = ls[-1]
            say(chat, f"🏁 <b>{title}</b>\nЗанятия заканчиваются в <b>{l['end']}</b> "
                      f"(последний урок: {esc(l['name'])}, каб. {l['room']}){suffix}", kb)
        else:
            pairs = len({l["pair"] for l in ls})
            say(chat, f"📚 <b>{title}</b>: {pairs} {plural(pairs, 'пара', 'пары', 'пар')} "
                      f"({len(ls)} {plural(len(ls), 'урок', 'урока', 'уроков')}){suffix}", kb)
        return True

    send_day(chat, d, data, group, sub=sub)
    return True


def addressed(m, st) -> bool:
    """В группах отвечаем, только если обратились к боту: @имя, «бот, …» или ответ на его сообщение."""
    me = st.get("me") or ""
    low = (m.get("text") or "").lower()
    if me and f"@{me}" in low:
        return True
    if re.match(r"\s*бот\b", low):
        return True
    rt = m.get("reply_to_message") or {}
    frm = rt.get("from") or {}
    return bool(frm.get("is_bot")) and (frm.get("username") or "").lower() == me


def strip_address(text: str, me: str) -> str:
    text = re.sub(r"^\s*бот\b[\s,:-]*", "", text, flags=re.I)
    return re.sub(rf"@{re.escape(me)}", "", text, flags=re.I).strip() if me else text.strip()


# ───────────────────────── надёжность: сообщения админу об ошибках ─────────────────────────
def short_exc(e) -> str:
    try:
        fr = traceback.extract_tb(e.__traceback__)[-1]
        return f"{type(e).__name__}: {e} ({fr.name}, строка {fr.lineno})"
    except Exception:
        return f"{type(e).__name__}: {e}"


def alert_admin(st, text):
    """Личное сообщение админу: через бота рассылки, а если не вышло — через бота расписания."""
    for aid in set(st["admin_ids"]) | ADMIN_IDS_ENV:
        ok = False
        if NOTIFY_TOKEN:
            ok = tg_n("sendMessage", chat_id=aid, text=text, parse_mode="HTML").get("ok")
        if not ok:
            tg("sendMessage", chat_id=aid, text=text, parse_mode="HTML")


def report_error(st, key, text, every=3600):
    """Одинаковая ошибка не чаще раза в час, чтобы не заспамить."""
    print("ERROR:", key, text)
    if st is None:
        return
    now = time.time()
    if now - st["err_sent"].get(key, 0) < every:
        return
    st["err_sent"][key] = now
    alert_admin(st, f"⚠️ <b>Проблема в боте</b>\n{esc(text)[:700]}\n🕒 {datetime.now(TZ):%d.%m %H:%M}")


def site_failed(st, e):
    print("site error:", e)
    if st is None:
        return
    h = st["health"]
    h["fails"] = h.get("fails", 0) + 1
    h.setdefault("first_fail", time.time())
    if h["fails"] >= 3 and time.time() - h["first_fail"] >= 600 and not h.get("alerted"):
        h["alerted"] = True
        alert_admin(st, "⚠️ <b>Сайт расписания не отвечает</b> уже больше 10 минут.\n"
                        f"Ошибка: {esc(repr(e))[:300]}\n"
                        "Пока показываю последние сохранённые данные с пометкой.")


def site_ok(st):
    if st is None:
        return
    if st["health"].get("alerted"):
        alert_admin(st, "✅ Сайт расписания снова отвечает.")
    st["health"] = {}


# ───────────────────────── автоматика (все подписанные чаты) ─────────────────────────
def auto_posts(st, now):
    if st.get("hv") != 5:
        st["hashes"] = {}
        st["hv"] = 5
    today = now.date()
    tomorrow = today + timedelta(days=1)
    by_gs = {}
    for c, on in st["subs"].items():
        if on:
            by_gs.setdefault((group_of(st, c), sub_of(st, c)), []).append(int(c))
    weather = None
    for (group, sub), chats in by_gs.items():
        try:
            data = fetch(today, group)
        except Exception as e:
            print("fetch error:", group, e)
            continue
        # изменения расписания на 3 дня (для выбранной подгруппы); по запасным данным не сравниваем
        for i in range(0 if data.get("_stale") else 3):
            day = today + timedelta(days=i)
            key = f"{group}|{sub}|{day.isoformat()}"
            blocks = filter_blocks(data.get(day.isoformat(), []), sub)
            h = hashlib.md5(json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old = st["hashes"].get(key)
            if old is not None and old != h and has_lessons(blocks):
                for c in chats:
                    if notif_on(st, c, "changes"):
                        send_day(c, day, data, group, new=True, sub=sub)
            st["hashes"][key] = h
        for c in chats:
            # утро
            if now.weekday() != 6 and 6 <= now.hour < 12 and (now.hour, now.minute) >= (6, 50) \
                    and st["sent"].get(f"{c}|m") != today.isoformat() and notif_on(st, c, "morning"):
                st["sent"][f"{c}|m"] = today.isoformat()
                weather = weather or weather_text()
                say(c, f"🌅 <b>ДОБРОЕ УТРО! {group}</b>\n\n"
                       f"📅 <b>Сегодня: {now:%d.%m.%Y} ({DAYS[now.weekday()]})</b>\n"
                       f"⏰ <b>Время: {now:%H:%M}</b>\n\n" + weather)
                send_day(c, today, data, group, sub=sub)
            # вечер
            if now.hour >= 19 and tomorrow.weekday() != 6 and st["sent"].get(f"{c}|e") != today.isoformat() \
                    and notif_on(st, c, "evening"):
                st["sent"][f"{c}|e"] = today.isoformat()
                send_day(c, tomorrow, data, group, sub=sub)
    for k in [k for k in st["hashes"] if k.split("|")[-1] < today.isoformat()]:
        del st["hashes"][k]


def main():
    st = load_state()
    _STATE["st"] = st
    started = time.time()
    try:
        now = datetime.now(TZ)
        if not st.get("me"):
            me = tg("getMe")
            if me.get("ok"):
                st["me"] = (me["result"].get("username") or "").lower()
        if st.get("cmds_v") != 2:
            tg("setMyCommands", commands=json.dumps([
                {"command": "today", "description": "Расписание на сегодня"},
                {"command": "tomorrow", "description": "Расписание на завтра"},
                {"command": "r", "description": "Расписание на 3 дня"},
                {"command": "weather", "description": "Погода в Югорске"},
                {"command": "group", "description": "Сменить группу"},
                {"command": "settings", "description": "Настройки (подгруппа, тема, группа)"},
                {"command": "help", "description": "Что я умею спрашивать"},
                {"command": "notify", "description": "Уведомления вкл/выкл"},
                {"command": "all", "description": "Позвать всех"}]))
            st["cmds_v"] = 2
        tg("deleteWebhook")
        if NOTIFY_TOKEN:
            n_setup()
        try:
            auto_posts(st, now)
        except Exception as e:
            report_error(st, "auto", "Ошибка автоматических рассылок: " + short_exc(e))
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
                    report_error(st, "auto", "Ошибка автоматических рассылок: " + short_exc(e))
                save_state(st)
                last_auto = time.time()
    except Exception as e:
        report_error(st, "fatal", "Бот остановился с ошибкой: " + short_exc(e))
        raise
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
