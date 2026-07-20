#!/usr/bin/env python3
"""
Өдөр тутмын AI мэдээ + бичлэгийн санаа илгээдэг Telegram бот.

Хоёр горимтой (MODE орчны хувьсагчаар):
  digest   - Өглөө 9:00-д бүтэн дайжест (мэдээ + fact + бичлэгийн санаа)
  breaking - 10 минут тутамд: шинэ мэдээ байвал 1-ийг нийтэлнэ,
             байхгүй бол сонирхолтой fact нийтэлнэ. Мэдээний нийтлэлээс
             бичлэг (YouTube г.м) олдвол хавсаргана.

Шаардлагатай орчны хувьсагчид:
  TELEGRAM_BOT_TOKEN  - BotFather-ээс авсан токен
  TELEGRAM_CHAT_ID    - Таслалаар тусгаарласан хүлээн авагчид
                        (эхнийх нь суваг: breaking зөвхөн түүнд очно)
  GEMINI_API_KEY      - https://aistudio.google.com/apikey
  GEMINI_MODEL        - (заавал биш) хоосон бол автоматаар олно
  MODE                - digest | breaking (default: digest)
"""

import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
MODE = os.environ.get("MODE", "digest").strip().lower()

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = "state.json"

# AI мэдээний RSS эх сурвалжууд (содон, шинэлэг мэдээ гаргадаг эхүүд орсон)
FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/"),
    ("Wired AI", "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("New Atlas", "https://newatlas.com/index.rss"),
    ("Hacker News (AI hits)", "https://hnrss.org/newest?q=AI&points=150"),
]

MAX_ITEMS = 18          # Gemini-д өгөх мэдээний дээд тоо
LOOKBACK_HOURS = 36     # Сүүлийн хэдэн цагийн мэдээг авах


def _clean(text):
    """Токен зэрэг нууц утгыг логоос нуух."""
    text = str(text)
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "***TOKEN***")
    if GEMINI_API_KEY:
        text = text.replace(GEMINI_API_KEY, "***KEY***")
    return text


def _parse_date(text):
    """RFC822 (RSS) болон ISO8601 (Atom) огноог хоёуланг нь ойлгоно."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _parse_feed(xml_text):
    """RSS 2.0 болон Atom фийдийг стандарт сангаар parse хийнэ."""
    xml_text = re.sub(r'xmlns="[^"]+"', "", xml_text, count=1)
    root = ET.fromstring(xml_text)
    entries = []
    for item in root.iter("item"):
        entries.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "summary": item.findtext("description") or "",
            "date": _parse_date(item.findtext("pubDate")),
        })
    if not entries:
        for entry in root.iter("entry"):
            link_el = entry.find("link")
            link = link_el.get("href", "") if link_el is not None else ""
            entries.append({
                "title": (entry.findtext("title") or "").strip(),
                "link": link.strip(),
                "summary": entry.findtext("summary") or entry.findtext("content") or "",
                "date": _parse_date(entry.findtext("published") or entry.findtext("updated")),
            })
    return entries


def fetch_news():
    """RSS фийдүүдээс сүүлийн үеийн мэдээг цуглуулна."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items = []
    headers = {"User-Agent": "Mozilla/5.0 (AI-News-Bot)"}
    for source, url in FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            entries = _parse_feed(resp.text)
        except Exception as e:
            print(f"[WARN] {source} татаж чадсангүй: {_clean(e)}")
            continue
        for entry in entries[:15]:
            dt = entry["date"] or datetime.now(timezone.utc)
            if dt < cutoff:
                continue
            items.append({
                "source": source,
                "title": entry["title"],
                "summary": _strip_tags(entry["summary"])[:300],
                "link": entry["link"],
                "date": dt,
            })
    items.sort(key=lambda x: x["date"], reverse=True)
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:MAX_ITEMS]


def _candidate_models():
    """Боломжтой Gemini моделиудыг API-гаас асууж, тохирохыг нь эрэмбэлнэ."""
    candidates = [GEMINI_MODEL] if GEMINI_MODEL else []
    try:
        resp = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={GEMINI_API_KEY}&pageSize=200",
            timeout=30,
        )
        models = resp.json().get("models", [])
        names = []
        for m in models:
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            if not name.startswith("gemini"):
                continue
            bad = ("embedding", "image", "tts", "audio", "live", "vision",
                   "imagen", "veo", "exp", "preview")
            if any(b in name for b in bad):
                continue
            names.append(name)
        flash = sorted([n for n in names if "flash" in n], reverse=True)
        other = sorted([n for n in names if "flash" not in n], reverse=True)
        candidates += flash + other
    except Exception as e:
        print(f"[WARN] Моделийн жагсаалт авч чадсангүй: {_clean(e)}")
        candidates += ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:8]


def _gemini(prompt, max_tokens=4000, temperature=0.8):
    """Gemini API дуудаж текст буцаана (боломжтой моделиудыг ээлжлэн турших)."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    for model in _candidate_models():
        u = (f"https://generativelanguage.googleapis.com/v1beta/models/"
             f"{model}:generateContent?key={GEMINI_API_KEY}")
        try:
            resp = requests.post(u, json=body, timeout=120)
        except Exception as e:
            print(f"[WARN] Gemini {model} холболтын алдаа: {_clean(e)}")
            continue
        if resp.status_code == 200:
            data = resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                print(f"[WARN] Gemini хариу хоосон: {_clean(str(data)[:300])}")
        else:
            print(f"[WARN] Gemini {model} алдаа {resp.status_code}: {_clean(resp.text[:200])}")
    raise RuntimeError("Gemini API ажилласангүй. GEMINI_API_KEY-ээ шалгана уу.")


def generate_digest(news_items):
    """Өглөөний бүтэн дайжест үүсгэнэ."""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    news_text = "\n\n".join(
        f"[{i+1}] ({it['source']}) {it['title']}\n{it['summary']}\nLink: {it['link']}"
        for i, it in enumerate(news_items)
    )

    prompt = f"""Чи монгол контент бүтээгчид зориулсан өдөр тутмын AI мэдээний туслах.
Өнөөдрийн огноо: {today}

Доорх англи хэл дээрх AI мэдээнүүдээс хамгийн СОДОН, ШИНЭЛЭГ, ГАЙХАЛТАЙ 5-г сонгож
(хөрөнгө оруулалт, компанийн албан ёсны зарлал гэх мэт уйтгартай мэдээг АЛГАСАЖ,
"Пөөх!" гэмээр, хүн шэйрлэмээр мэдээг сонго), дараах форматаар МОНГОЛ ХЭЛЭЭР
Telegram мессеж бичээрэй. Telegram-ын HTML форматыг ашигла
(<b>bold</b>, <a href="...">линк</a> гэх мэт; <br> болон markdown БҮҮ ашигла).

Формат:
🤖 <b>Өнөөдрийн AI мэдээ</b> — {today}

1️⃣ <b>[мэдээний сонирхол татам гарчиг монголоор]</b>
[1-2 өгүүлбэрээр яагаад гайхалтай болохыг тайлбарла]
<a href="[link]">Дэлгэрэнгүй</a>

... (нийт 5 мэдээ)

🧠 <b>Мэдэхэд илүүдэхгүй fact</b>

⚡️ [AI/технологитой холбоотой, хүн гайхаад шэйрлэмээр 2 fact — тус бүр 1-2
өгүүлбэр. Жинхэнэ баримт байх ёстой, зохиож болохгүй. Аль болох өнөөдрийн
мэдээтэй холбоотой эсвэл цаг үеийн байвал сайн]

🎬 <b>Өнөөдрийн бичлэгийн санаанууд</b>

💡 <b>Санаа 1 (мэдээний бичлэг): [гарчиг]</b> — [дээрх мэдээнээс хамгийн содон
нэгээр TikTok/Reels бичлэг хийх санаа + hook буюу эхний өгүүлбэр]

💡 <b>Санаа 2 (fact бичлэг): [гарчиг]</b> — [богино fact бичлэгийн бэлэн бүтэц:
Hook (1 өгүүлбэр) → Fact 1 → Fact 2 → Fact 3 → Төгсгөлийн өгүүлбэр. Бүгдийг
бичиж өг, шууд уншаад бичлэг хийхэд бэлэн байхаар]

💡 <b>Санаа 3: ...</b> — [чөлөөт сэдэв: туршилт, харьцуулалт, топ жагсаалт г.м]

Шаардлага:
- Нийт 3500 тэмдэгтээс хэтрэхгүй
- Энгийн ойлгомжтой монгол хэлээр, техник нэр томьёог хэвээр үлдээж болно
- Fact болон санаанууд нь монгол үзэгчдэд сонирхолтой, шэйрлэгдэхүйц байхаар бод

Мэдээнүүд:
{news_text}"""
    return _gemini(prompt)


def find_video_in_article(url):
    """Мэдээний нийтлэлээс бичлэгийн линк (YouTube г.м) хайж олно."""
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        page = resp.text[:400000]
    except Exception as e:
        print(f"[WARN] Нийтлэл нээж чадсангүй: {_clean(e)}")
        return None
    patterns = [
        r'https?://(?:www\.)?youtube\.com/embed/[\w\-]+',
        r'https?://(?:www\.)?youtube\.com/watch\?v=[\w\-]+',
        r'https?://youtu\.be/[\w\-]+',
        r'<meta[^>]+property="og:video[^"]*"[^>]+content="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, page)
        if m:
            link = m.group(1) if m.groups() else m.group(0)
            link = link.replace("/embed/", "/watch?v=")
            print(f"[INFO] Бичлэг олдлоо: {link}")
            return link
    return None


def generate_single_post(item, video_link):
    """Нэг мэдээгээр богино сувгийн пост бичнэ."""
    video_note = (
        f"\nЭнэ мэдээнд хамаарах бичлэг: {video_link} — постын төгсгөлд "
        f'🎥 <a href="{video_link}">Бичлэг үзэх</a> гэсэн мөр нэм.'
        if video_link else ""
    )
    prompt = f"""Чи монгол Telegram сувгийн AI мэдээний редактор. Доорх нэг мэдээгээр
богино, сонирхол татам пост МОНГОЛ ХЭЛЭЭР бич. Telegram HTML формат ашигла
(<b>bold</b>, <a href="...">линк</a>; markdown БҮҮ ашигла).

Формат:
⚡️ <b>[сонирхол татам гарчиг]</b>

[2-3 өгүүлбэр — юу болсон, яагаад сонирхолтой вэ. Энгийн ярианы хэлээр.]

<a href="{item['link']}">Дэлгэрэнгүй унших</a>{video_note}

Шаардлага: 800 тэмдэгтээс хэтрэхгүй. Зохиомол мэдээлэл бүү нэм.

Мэдээ:
({item['source']}) {item['title']}
{item['summary']}"""
    return _gemini(prompt, max_tokens=1000)


def generate_fact_post(avoid_facts):
    """Шинэ мэдээ байхгүй үед сонирхолтой fact пост бичнэ."""
    avoid = "\n".join(f"- {f}" for f in avoid_facts[-40:]) or "- (хоосон)"
    prompt = f"""Чи монгол Telegram сувагт AI, технологи, шинжлэх ухааны гайхалтай
баримт нийтэлдэг редактор. НЭГ шинэ, үнэн, гайхшруулам fact-ыг МОНГОЛ ХЭЛЭЭР бич.
Telegram HTML формат (<b>bold</b>; markdown БҮҮ ашигла).

Формат:
🧠 <b>Мэдэхэд илүүдэхгүй fact</b>

[3-4 өгүүлбэрээр баримтаа сонирхолтой тайлбарла. Жинхэнэ баримт байх ёстой,
зохиож болохгүй. Тоо баримттай бол илүү сайн.]

Шаардлага: 700 тэмдэгтээс хэтрэхгүй. Дараах өмнө нийтэлсэн сэдвүүдийг ДАВТАЖ
БОЛОХГҮЙ:
{avoid}"""
    return _gemini(prompt, max_tokens=800, temperature=1.0)


def detect_chat_id():
    """CHAT_ID өгөөгүй бол getUpdates-ээс сүүлийн чатыг олно."""
    resp = requests.get(f"{TELEGRAM_API}/getUpdates", timeout=30).json()
    for update in reversed(resp.get("result", [])):
        msg = update.get("message") or update.get("channel_post")
        if msg:
            cid = msg["chat"]["id"]
            print(f"[INFO] Chat ID автоматаар олдлоо: {cid}")
            return str(cid)
    me = requests.get(f"{TELEGRAM_API}/getMe", timeout=30).json()
    username = me.get("result", {}).get("username", "?")
    raise RuntimeError(
        f"Chat ID олдсонгүй. Эхлээд Telegram дээр @{username} бот руугаа /start "
        "гэж бичээд, дараа нь дахин ажиллуулна уу."
    )


def send_message(chat_id, text, preview=False):
    """Telegram руу илгээнэ (4096 тэмдэгтээс урт бол хуваана)."""
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, 4000)
        if cut == -1:
            cut = 4000
        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    for chunk in chunks:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[WARN] HTML илгээлт алдаа: {_clean(resp.text[:200])} — plain text-ээр оролдож байна")
            plain = re.sub(r"<[^>]+>", "", chunk)
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": plain,
                      "disable_web_page_preview": not preview},
                timeout=30,
            )
            resp.raise_for_status()
    print("[OK] Мессеж амжилттай илгээгдлээ!")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posted": [], "recent_facts": []}


def save_state(state):
    state["posted"] = state["posted"][-600:]
    state["recent_facts"] = state["recent_facts"][-100:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def run_digest(chat_ids):
    print("[1/3] Мэдээ татаж байна...")
    news = fetch_news()
    print(f"      {len(news)} мэдээ олдлоо")
    if not news:
        for cid in chat_ids:
            send_message(cid, "🤖 Өнөөдөр шинэ AI мэдээ олдсонгүй. Маргааш дахин шалгана!")
        return
    print("[2/3] Gemini-гээр дайжест үүсгэж байна...")
    digest = generate_digest(news)
    print("[3/3] Telegram руу илгээж байна...")
    for cid in chat_ids:
        try:
            send_message(cid, digest)
        except Exception as e:
            print(f"[WARN] {cid} руу илгээж чадсангүй: {_clean(e)}")


def run_breaking(chat_ids):
    """10 минут тутмын горим: шинэ мэдээ эсвэл fact нийтэлнэ (зөвхөн суваг руу)."""
    channel = chat_ids[0]
    state = load_state()
    posted = set(state["posted"])

    print("[1/3] Шинэ мэдээ шалгаж байна...")
    news = fetch_news()
    fresh = [n for n in news if n["link"] and n["link"] not in posted]
    print(f"      {len(news)} мэдээнээс {len(fresh)} нь шинэ")

    if fresh:
        item = fresh[0]  # хамгийн шинэ
        print(f"[2/3] Нийтлэх мэдээ: {item['title'][:70]}")
        video = find_video_in_article(item["link"])
        post = generate_single_post(item, video)
        print("[3/3] Суваг руу илгээж байна...")
        send_message(channel, post, preview=True)
        state["posted"].append(item["link"])
    else:
        print("[2/3] Шинэ мэдээ алга — fact нийтэлнэ...")
        fact = generate_fact_post(state["recent_facts"])
        print("[3/3] Суваг руу илгээж байна...")
        send_message(channel, fact)
        # Гарчгийн мөрийг санах (давталтаас сэргийлнэ)
        first_line = _strip_tags(fact).split("\n")[2] if len(_strip_tags(fact).split("\n")) > 2 else _strip_tags(fact)[:80]
        state["recent_facts"].append(first_line[:120])

    save_state(state)


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN тохируулаагүй байна!")
    if not GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY тохируулаагүй байна! https://aistudio.google.com/apikey")

    chat_ids = [c.strip() for c in (CHAT_ID or detect_chat_id()).split(",") if c.strip()]
    print(f"[INFO] Горим: {MODE}")

    if MODE == "breaking":
        run_breaking(chat_ids)
    else:
        run_digest(chat_ids)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print(_clean(traceback.format_exc()))
        sys.exit(1)
