import os
import asyncio
import logging
import re
from dotenv import load_dotenv
from supabase import create_client, Client
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# для workflow_dispatch
GITHUB_TRIGGER_TOKEN = os.getenv("GITHUB_TRIGGER_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW_FILE", "scraper.yml")
GITHUB_REF = os.getenv("GITHUB_REF", "main")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

REF_LINK = "https://panel.city-drive.pl/ref/PracaBOT"
DONATE_ACCOUNT = "84 9511 0000 0052 9681 3000 0010"

PROMO_TEXT = (
    "💼 <b>Ищешь подработку с гибким графиком в Польше?</b>\n\n"
    "Подключайся к доставке через <b>City Drive</b> и выходи на заказы в "
    "<b>Glovo / Uber Eats / Bolt Food</b>.\n\n"
    "🎁 <b>Бонус для новых:</b> 50 PLN на баланс при регистрации\n"
    "🏷 <b>Промокод:</b> PracaBOT\n\n"
    "👇 Нажми на кнопку ниже, чтобы оставить заявку"
)

BLOCKED_KEYWORDS = [
    "uber", "bolt", "glovo", "uber eats", "bolt food",
    "wolt", "dostawca jedzenia", "kurier rowerowy",
    "kierowca uber", "kierowca bolt", "kierowca glovo",
]

BTN_RESET = "🔄 Ustaw od nowa"
BTN_STOP = "⏹ Zatrzymaj"
BTN_HELP = "ℹ️ Pomoc"
BTN_RESTART = "🚀 Uruchom ponownie"


class SetupStates(StatesGroup):
    lang = State()
    city = State()
    city_custom = State()
    etat = State()
    umowa = State()


CITIES = [
    ("Warszawa", "Warszawa"),
    ("Kraków", "Kraków"),
    ("Wrocław", "Wrocław"),
    ("Poznań", "Poznań"),
    ("Gdańsk", "Gdańsk"),
    ("Łódź", "Łódź"),
    ("Katowice", "Katowice"),
    ("Lublin", "Lublin"),
    ("Toruń", "Toruń"),
    ("Szczecin", "Szczecin"),
    ("Bydgoszcz", "Bydgoszcz"),
    ("Gdynia", "Gdynia"),
]

UMOWY = [
    ("Dowolna", "any"),
    ("Umowa o pracę", "umowa_o_prace"),
    ("Umowa zlecenie", "umowa_zlecenie"),
    ("Umowa o dzieło", "umowa_o_dzielo"),
    ("B2B", "b2b"),
    ("Staż / Praktyki", "staz"),
]

UMOWA_DISPLAY = {
    "umowa_o_prace": "Umowa o pracę",
    "umowa_zlecenie": "Umowa zlecenie",
    "umowa_o_dzielo": "Umowa o dzieło",
    "b2b": "B2B",
    "staz": "Staż / Praktyki",
}

ETAT_DISPLAY = {
    "full": "Pełny etat",
    "part": "Niepełny etat",
}

TEXTS = {
    "ru": {
        "welcome": "👋 Привет! Я помогу найти работу в Польше.\n\nВыбери язык:",
        "choose_city": "🏙 Выбери город:",
        "enter_city": "✏️ Напиши город на польском:",
        "choose_etat": "⏰ Выбери тип занятости (можно несколько):\n\nПотом ✅ Готово",
        "choose_umowa": "📋 Выбери тип договора:",
        "saved": "✅ Фильтры сохранены!\n\n🏙 {city}\n⏰ {etat}\n📋 {umowa}\n\n🔍 Ищу актуальные вакансии...",
        "loading_city": "🔍 По этому городу пока нет кэша в базе.\nПробую быстро подтянуть вакансии, подожди 20–60 секунд...",
        "no_jobs": "😔 Пока не нашёл вакансий по твоим фильтрам.",
        "menu_active": "🟢 Бот запущен и ищет вакансии. Кнопки управления ниже 👇",
        "stop_donate": f"⏹ Рассылка остановлена.\n\n💳 <code>{DONATE_ACCOUNT}</code>\n\nПо вопросам: @Hriaker1",
        "reset_msg": "🔄 Фильтры сброшены! Выбери язык:",
        "help": "🤖 /start /reset /stop /help",
        "already_stopped": "ℹ️ Ты не подписан. Нажми кнопку ниже чтобы начать.",
        "btn_all": "🇵🇱 Вся Польша",
        "btn_custom": "✏️ Свой город",
        "btn_done": "✅ Готово",
        "after_initial": "👆 Это были последние актуальные вакансии.\n🔄 Дальше бот будет слать новые по мере появления.",
    }
}

def t(lang, key, **kwargs):
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, "")
    return text.format(**kwargs) if kwargs else text

def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = re.sub(r"\.css-[a-z0-9]+\{[^}]*\}", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def normalize_umowa(umowa):
    if not umowa:
        return None
    u = str(umowa).lower().strip()
    if u in ["umowa_o_prace", "umowa_zlecenie", "umowa_o_dzielo", "b2b", "staz"]:
        return u
    if "zlecenie" in u: return "umowa_zlecenie"
    if "o pracę" in u or "o prace" in u: return "umowa_o_prace"
    if "b2b" in u or "kontrakt" in u: return "b2b"
    if "dzieło" in u or "dzielo" in u: return "umowa_o_dzielo"
    if "staż" in u or "staz" in u or "praktyk" in u: return "staz"
    return None

def normalize_etat(etat):
    if not etat:
        return None
    e = str(etat).lower().strip()
    if e in ["full", "part"]:
        return e
    if any(x in e for x in ["part", "niepełny", "niepelny", "1/2", "część etatu", "czesc etatu"]):
        return "part"
    if any(x in e for x in ["full", "pełny", "pelny", "cały", "caly"]):
        return "full"
    return None

def job_matches_filter(job, user_filter):
    uf = user_filter.get("umowa", "any")
    ef = user_filter.get("etat_full", True)
    ep = user_filter.get("etat_part", True)

    job_umowa = normalize_umowa(job.get("umowa"))
    job_etat = normalize_etat(job.get("etat"))

    if uf != "any":
        if not job_umowa or job_umowa != uf:
            return False

    if not (ef and ep):
        if not job_etat:
            return False
        if ef and not ep and job_etat != "full":
            return False
        if ep and not ef and job_etat != "part":
            return False

    return True

def is_delivery_job(job):
    title = (job.get("title") or "").lower()
    company = (job.get("company") or "").lower()
    text = f"{title} {company}"
    return any(kw in text for kw in BLOCKED_KEYWORDS)

# ==================== DB HELPERS ====================

def db_upsert_user(tid, username=None):
    try:
        supabase.table("users").upsert(
            {"telegram_id": tid, "username": username, "is_active": True},
            on_conflict="telegram_id"
        ).execute()
    except Exception as e:
        logger.error(f"db_upsert_user: {e}")

def db_get_user(tid):
    try:
        r = supabase.table("users").select("*").eq("telegram_id", tid).execute()
        return r.data[0] if r.data else None
    except:
        return None

def db_set_user_active(tid, a):
    try:
        supabase.table("users").update({"is_active": a}).eq("telegram_id", tid).execute()
    except:
        pass

def db_upsert_filter(tid, city, ef, ep, umowa):
    try:
        supabase.table("user_filters").upsert(
            {"telegram_id": tid, "city": city, "etat_full": ef, "etat_part": ep, "umowa": umowa},
            on_conflict="telegram_id"
        ).execute()
    except Exception as e:
        logger.error(f"db_upsert_filter: {e}")

def db_delete_filter(tid):
    try:
        supabase.table("user_filters").delete().eq("telegram_id", tid).execute()
    except:
        pass

def db_get_filter(tid):
    try:
        r = supabase.table("user_filters").select("*").eq("telegram_id", tid).execute()
        return r.data[0] if r.data else None
    except:
        return None

def db_get_all_filters():
    try:
        r = supabase.table("user_filters").select("*").execute()
        return r.data or []
    except:
        return []

def db_already_sent(tid, jid):
    try:
        r = supabase.table("sent_jobs").select("id").eq("telegram_id", tid).eq("job_id", jid).execute()
        return bool(r.data)
    except:
        return False

def db_mark_sent(tid, jid):
    try:
        supabase.table("sent_jobs").insert({"telegram_id": tid, "job_id": jid}).execute()
    except:
        pass

def db_clear_sent(tid):
    try:
        supabase.table("sent_jobs").delete().eq("telegram_id", tid).execute()
    except:
        pass

def db_get_jobs_for_city(city, limit=150):
    try:
        if city == "all":
            r = supabase.table("jobs").select("*").order("created_at", desc=True).limit(limit).execute()
            return r.data or []
        r = supabase.table("jobs").select("*").ilike("city", f"%{city}%").order("created_at", desc=True).limit(limit).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"db_get_jobs_for_city: {e}")
        return []

# ==================== GITHUB ACTIONS TRIGGER ====================

async def trigger_scraper_for_city(city: str) -> bool:
    if not GITHUB_TRIGGER_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.error("GitHub trigger vars are missing")
        return False

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TRIGGER_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "ref": GITHUB_REF,
        "inputs": {
            "city": city
        }
    }

    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                txt = await resp.text()
                if resp.status in (200, 201, 204):
                    logger.info(f"GitHub workflow dispatched for city={city}")
                    return True
                logger.error(f"GitHub dispatch failed {resp.status}: {txt}")
                return False
    except Exception as e:
        logger.error(f"trigger_scraper_for_city: {e}")
        return False

async def wait_for_city_jobs(city: str, attempts: int = 12, delay: int = 5):
    """
    Ждём до ~60 секунд, пока workflow соберёт вакансии.
    """
    for _ in range(attempts):
        jobs = db_get_jobs_for_city(city, limit=150)
        if jobs:
            return jobs
        await asyncio.sleep(delay)
    return []

# ==================== WEB SERVER ====================

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🌐 Web server started on port {port}")

# ==================== SEND ====================

def format_job(job):
    ut = UMOWA_DISPLAY.get(job.get("umowa")) or job.get("umowa")
    et = ETAT_DISPLAY.get(job.get("etat")) or job.get("etat")
    lines = [f"💼 <b>{strip_html(job.get('title'))}</b>"]
    if job.get("company"):
        lines.append(f"🏢 {strip_html(job['company'])}")
    if ut:
        lines.append(f"📄 {strip_html(str(ut))}")
    if et:
        lines.append(f"⏰ {strip_html(str(et))}")
    if job.get("city"):
        lines.append(f"📍 {strip_html(job['city'])}")
    if job.get("salary"):
        lines.append(f"💰 {strip_html(job['salary'])}")
    lines.append(f"📌 {job.get('source','—')}")
    if job.get("url"):
        lines.append(f"🔗 <a href='{job['url']}'>Открыть вакансию</a>")
    lines.append("")
    lines.append("🤖 <a href='https://t.me/szukam_pracy_bot'>@szukam_pracy_bot</a> — свежие вакансии в Польше 🇵🇱")
    return "\n".join(lines)

async def send_promo(chat_id):
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Получить 50 PLN и начать", url=REF_LINK)
        ]])
        sent_msg = await bot.send_message(chat_id, PROMO_TEXT, reply_markup=kb, parse_mode="HTML")
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except:
            pass
    except Exception as e:
        logger.error(f"promo: {e}")

async def send_jobs_to_user(tid, jobs, user_filter=None, limit=15, is_initial=False):
    sent, sf, ss, blocked = 0, 0, 0, 0
    for job in jobs:
        if sent >= limit:
            break
        if is_delivery_job(job):
            blocked += 1
            continue
        if user_filter and not job_matches_filter(job, user_filter):
            sf += 1
            continue
        if db_already_sent(tid, job['id']):
            ss += 1
            continue
        try:
            await bot.send_message(
                tid,
                format_job(job),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            db_mark_sent(tid, job['id'])
            sent += 1
            await asyncio.sleep(0.1)  # безопасно для Telegram
        except Exception as e:
            logger.error(f"send: {e}")

    logger.info(f"Sent={sent} filtered={sf} already={ss} blocked={blocked}")

    if is_initial and sent > 0:
        lang = get_user_lang(tid)
        try:
            await bot.send_message(tid, t(lang, "after_initial"))
        except:
            pass

    return sent

# ==================== KEYBOARDS ====================

def kb_lang():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 RU", callback_data="l_ru"),
        InlineKeyboardButton(text="🇵🇱 PL", callback_data="l_pl"),
        InlineKeyboardButton(text="🇺🇦 UA", callback_data="l_ua"),
    ]])

def kb_cities(lang):
    rows, row = [], []
    for name, val in CITIES:
        row.append(InlineKeyboardButton(text=name, callback_data=f"c_{val}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=t(lang, "btn_all"), callback_data="c_all")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_custom"), callback_data="c_custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_etat(lang, sel):
    f = "✅ " if sel.get("full") else "☐ "
    p = "✅ " if sel.get("part") else "☐ "
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{f}Pełny etat", callback_data="e_full")],
        [InlineKeyboardButton(text=f"{p}Niepełny etat", callback_data="e_part")],
        [InlineKeyboardButton(text=t(lang, "btn_done"), callback_data="e_done")],
    ])

def kb_umowa():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=n, callback_data=f"u_{v}")]
        for n, v in UMOWY
    ])

def kb_active_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RESET), KeyboardButton(text=BTN_STOP)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True
    )

def kb_stopped_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_RESTART)]],
        resize_keyboard=True
    )

# ==================== HANDLERS ====================

@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    db_upsert_user(m.from_user.id, m.from_user.username)
    db_set_user_active(m.from_user.id, True)
    db_clear_sent(m.from_user.id)
    await state.update_data(lang="ru", etat={"full": False, "part": False})
    await state.set_state(SetupStates.lang)
    await m.answer("⚙️", reply_markup=ReplyKeyboardRemove())
    await m.answer(t("ru", "welcome"), reply_markup=kb_lang())

@router.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    await state.clear()
    lang = get_user_lang(m.from_user.id)
    db_delete_filter(m.from_user.id)
    db_clear_sent(m.from_user.id)
    db_set_user_active(m.from_user.id, True)
    await state.update_data(lang=lang, etat={"full": False, "part": False})
    await state.set_state(SetupStates.lang)
    await m.answer("⚙️", reply_markup=ReplyKeyboardRemove())
    await m.answer(t(lang, "reset_msg"), reply_markup=kb_lang())

@router.message(Command("stop"))
async def cmd_stop(m: Message, state: FSMContext):
    await state.clear()
    lang = get_user_lang(m.from_user.id)
    if not db_get_filter(m.from_user.id):
        await m.answer(t(lang, "already_stopped"), reply_markup=kb_stopped_menu())
        return
    db_delete_filter(m.from_user.id)
    db_set_user_active(m.from_user.id, False)
    await m.answer(t(lang, "stop_donate"), parse_mode="HTML", reply_markup=kb_stopped_menu())

@router.message(Command("help"))
async def cmd_help(m: Message):
    lang = get_user_lang(m.from_user.id)
    await m.answer(t(lang, "help"), parse_mode="HTML")

@router.message(F.text == BTN_RESET)
async def btn_reset(m: Message, state: FSMContext):
    await cmd_reset(m, state)

@router.message(F.text == BTN_STOP)
async def btn_stop(m: Message, state: FSMContext):
    await cmd_stop(m, state)

@router.message(F.text == BTN_HELP)
async def btn_help(m: Message):
    await cmd_help(m)

@router.message(F.text == BTN_RESTART)
async def btn_restart(m: Message, state: FSMContext):
    await cmd_start(m, state)

@router.callback_query(SetupStates.lang, F.data.startswith("l_"))
async def on_lang(c: CallbackQuery, state: FSMContext):
    lang = c.data[2:]
    await state.update_data(lang=lang)
    try:
        supabase.table("users").update({"language": lang}).eq("telegram_id", c.from_user.id).execute()
    except:
        pass
    await state.set_state(SetupStates.city)
    await c.message.edit_text(t(lang, "choose_city"), reply_markup=kb_cities(lang))
    await c.answer()

@router.callback_query(SetupStates.city, F.data.startswith("c_"))
async def on_city(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    val = c.data[2:]
    if val == "custom":
        await state.set_state(SetupStates.city_custom)
        await c.message.edit_text(t(lang, "enter_city"))
        await c.answer()
        return
    cd = t(lang, "btn_all") if val == "all" else val
    await state.update_data(city=val, city_display=cd)
    await state.set_state(SetupStates.etat)
    sel = data.get("etat", {"full": False, "part": False})
    await c.message.edit_text(t(lang, "choose_etat"), reply_markup=kb_etat(lang, sel))
    await c.answer()

@router.message(SetupStates.city_custom)
async def on_city_custom(m: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    city = m.text.strip()
    await state.update_data(city=city, city_display=city)
    await state.set_state(SetupStates.etat)
    sel = data.get("etat", {"full": False, "part": False})
    await m.answer(t(lang, "choose_etat"), reply_markup=kb_etat(lang, sel))

@router.callback_query(SetupStates.etat, F.data.startswith("e_"))
async def on_etat(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    sel = data.get("etat", {"full": False, "part": False})
    action = c.data[2:]
    if action == "done":
        if not sel.get("full") and not sel.get("part"):
            sel = {"full": True, "part": True}
        await state.update_data(etat=sel)
        await state.set_state(SetupStates.umowa)
        await c.message.edit_text(t(lang, "choose_umowa"), reply_markup=kb_umowa())
        await c.answer()
        return
    if action == "full":
        sel["full"] = not sel.get("full", False)
    elif action == "part":
        sel["part"] = not sel.get("part", False)
    await state.update_data(etat=sel)
    await c.message.edit_reply_markup(reply_markup=kb_etat(lang, sel))
    await c.answer()

@router.callback_query(SetupStates.umowa, F.data.startswith("u_"))
async def on_umowa(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    uv = c.data[2:]
    city = data.get("city", "all")
    cd = data.get("city_display", "Вся Польша")
    sel = data.get("etat", {"full": True, "part": False})

    ep = []
    if sel.get("full"):
        ep.append("Pełny etat")
    if sel.get("part"):
        ep.append("Niepełny etat")
    ed = ", ".join(ep) if ep else "Pełny etat"
    ud = next((n for n, v in UMOWY if v == uv), uv)

    db_upsert_filter(c.from_user.id, city, sel.get("full", True), sel.get("part", False), uv)
    await state.clear()

    uf = {
        "umowa": uv,
        "etat_full": sel.get("full", True),
        "etat_part": sel.get("part", False),
    }

    await c.message.edit_text(t(lang, "saved", city=cd, etat=ed, umowa=ud))
    await c.answer()

    await bot.send_message(c.from_user.id, t(lang, "menu_active"), reply_markup=kb_active_menu())
    await send_promo(c.from_user.id)
    await asyncio.sleep(1)

    # Сначала пробуем быстро найти вакансии в базе
    jobs = db_get_jobs_for_city(city)

    # Если нет — дёргаем GitHub Actions под этот город
    if not jobs and city != "all":
        await bot.send_message(c.from_user.id, t(lang, "loading_city"))
        ok = await trigger_scraper_for_city(city)
        if ok:
            jobs = await wait_for_city_jobs(city)

    if jobs:
        sent = await send_jobs_to_user(c.from_user.id, jobs, user_filter=uf, limit=8, is_initial=True)
        if sent == 0:
            await bot.send_message(c.from_user.id, t(lang, "no_jobs"))
    else:
        await bot.send_message(c.from_user.id, t(lang, "no_jobs"))


# ==================== SCHEDULER ====================

async def scheduled_check():
    logger.info("⏰ Check")
    filters = db_get_all_filters()
    if not filters:
        return

    # Бот больше не парсит, только читает Supabase
    for f in filters:
        tid = f["telegram_id"]
        city = f.get("city", "all")
        jobs = db_get_jobs_for_city(city)
        uf = {
            "umowa": f.get("umowa", "any"),
            "etat_full": f.get("etat_full", True),
            "etat_part": f.get("etat_part", False),
        }
        if jobs:
            sent = await send_jobs_to_user(tid, jobs, user_filter=uf, limit=15)
            logger.info(f"Sent {sent} to {tid}")

    logger.info("✅ Done")


async def main():
    logger.info("🚀 Bot starting...")
    await start_web_server()

    s = AsyncIOScheduler()
    s.add_job(scheduled_check, "interval", minutes=10, id="check", replace_existing=True)
    s.start()

    logger.info("⏰ Scheduler started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())