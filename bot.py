import os
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
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
GITHUB_TRIGGER_TOKEN = os.getenv("GITHUB_TRIGGER_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW_FILE", "scraper.yml")
GITHUB_REF = os.getenv("GITHUB_REF", "main")

# Твой ID администратора
ADMIN_ID = 6526189823

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
    "💼 <b>Ищешь подработку с гибким графиком in Польше?</b>\n\n"
    "Подключайся к доставке через <b>City Drive</b> и выходи на заказы in "
    "<b>Glovo / Uber Eats / Bolt Food</b>.\n\n"
    "Что по условиям:\n"
    "• свободный график — можно совмещать с учёбой или основной работой\n"
    "• выплаты каждую неделю на карту\n"
    "• можно работать на своём авто, велосипеде или самокате\n"
    "• быстрый старт через проверенного партнёра\n\n"
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


# Состояния для твоей админки
class AdminStates(StatesGroup):
    waiting_for_ad = State()
    confirm_ad = State()


CITIES = [
    ("Warszawa", "Warszawa"), ("Kraków", "Kraków"),
    ("Wrocław", "Wrocław"), ("Poznań", "Poznań"),
    ("Gdańsk", "Gdańsk"), ("Łódź", "Łódź"),
    ("Katowice", "Katowice"), ("Lublin", "Lublin"),
    ("Toruń", "Toruń"), ("Szczecin", "Szczecin"),
    ("Bydgoszcz", "Bydgoszcz"), ("Gdynia", "Gdynia"),
]

CITY_SLUGS = {
    "warszawa": "warszawa", "kraków": "krakow", "krakow": "krakow",
    "wrocław": "wroclaw", "wroclaw": "wroclaw",
    "poznań": "poznan", "poznan": "poznan",
    "gdańsk": "gdansk", "gdansk": "gdansk",
    "łódź": "lodz", "lodz": "lodz",
    "katowice": "katowice", "lublin": "lublin",
    "toruń": "torun", "torun": "torun",
    "szczecin": "szczecin", "bydgoszcz": "bydgoszcz",
    "białystok": "bialystok", "bialystok": "bialystok",
    "gdynia": "gdynia",
    "częstochowa": "czestochowa", "czestochowa": "czestochowa",
    "sosnowiec": "sosnowiec",
    "rzeszów": "rzeszow", "rzeszow": "rzeszow",
    "kielce": "kielce", "gliwice": "gliwice",
    "zabrze": "zabrze", "olsztyn": "olsztyn", "opole": "opole",
    "zielona góra": "zielona-gora", "zielona gora": "zielona-gora",
}

UMOWY = [
    ("Dowolna", "any"),
    ("Umowa o pracę", "umowa_o_prace"),
    ("Umowa zlecenie", "umowa_zlecenie"),
    ("Umowa o dzieło", "umowa_o_dzielo"),
    ("B2B", "b2b"),
    ("Staż / Praktyки", "staz"),
]

UMOWY_DISPLAY = {
    "umowa_o_prace": "Umowa o pracę",
    "umowa_zlecenie": "Umowa zlecenie",
    "umowa_o_dzielo": "Umowa o dzieło",
    "b2b": "B2B",
    "staz": "Staż / Praktyки",
}

ETAT_DISPLAY = {
    "full": "Pełny etat",
    "part": "Niepełny etat",
}

TEXTS = {
    "ru": {
        "welcome": (
            "👋 Привет! Я помогу найти работу в Польше.\n\n"
            "Буду присылать свежие вакансии по мере их появления "
            "с OLX, Praca.pl и GoWork.\n\n"
            "Выбери язык:"
        ),
        "choose_city": "🏙 Выбери город:",
        "enter_city": "✏️ Напиши название города на польском (например: Szczecin):",
        "choose_etat": "⏰ Выбери тип занятости (можно несколько):\n\nНажми нужные, потом ✅ Готово",
        "choose_umowa": "📋 Выбери тип договора:",
        "saved": (
            "✅ Фильтры сохранены!\n\n"
            "🏙 Город: {city}\n"
            "⏰ Занятость: {etat}\n"
            "📋 Договор: {umowa}\n\n"
            "🔍 Ищу свежие вакансии на OLX, Praca.pl и GoWork..."
        ),
        "loading_city": (
            "🔍 По этому городу собираю свежие вакансии...\n"
            "Подожди 30–60 секунд."
        ),
        "no_jobs": "😔 Пока нет вакансий по твоим фильтрам.\nБуду проверять каждые 15 минут!",
        "menu_active": "🟢 Бот запущен и ищет вакансии. Кнопки управления ниже 👇",
        "stop_donate": (
            "⏹ Рассылка остановлена.\n\n"
            "🙏 Надеюсь, с моей помощью тебе удалось найти желанную вакансию.\n"
            "Ты получил то, что хотел — а если хочешь отблагодарить "
            "дедди лавэхой, то реализуй и это своё желание 😏\n\n"
            f"💳 <code>{DONATE_ACCOUNT}</code>\n\n"
            "По вопросам и сотрудничеству: @Hriaker1"
        ),
        "reset_msg": "🔄 Фильтры сброшены! Начнём заново.\n\nВыбери язык:",
        "help": (
            "🤖 <b>Что умеет бот:</b>\n\n"
            "Агрегирует публично доступные вакансии "
            "с OLX, Praca.pl и GoWork и присылает их тебе.\n\n"
            "<b>Управление:</b>\n"
            f"<b>{BTN_RESET}</b> — настроить фильтры заново\n"
            f"<b>{BTN_STOP}</b> — остановить рассылку\n"
            f"<b>{BTN_HELP}</b> — эта справка\n\n"
            "По вопросам и сотрудничеству: @Hriaker1"
        ),
        "already_stopped": "ℹ️ Ты не подписан на вакансии. Нажми кнопку ниже чтобы начать.",
        "btn_all": "🇵🇱 Вся Польша",
        "btn_custom": "✏️ Свой город",
        "btn_done": "✅ Готово",
        "after_initial": (
            "👆 Это были последние актуальные вакансии за сегодня.\n\n"
            "🔄 Теперь бот будет присылать только новые вакансии "
            "по мере их появления на OLX, Praca.pl и GoWork."
        ),
        "search_paused": (
            "⏸ <b>Поиск временно приостановлен</b>\n\n"
            "Ты пользуешься поиском уже 3 дня. Чтобы бот продолжил присылать "
            "тебе свежие вакансии бесплатно, подтверди, что ты всё ещё ищешь работу! 👇"
        ),
        "btn_continue": "🔄 Продолжить поиск",
        "search_renewed": "🟢 Отлично! Поиск успешно возобновлен еще на 3 дня. Свежие вакансии уже в пути! 🚀",
    },
    "pl": {
        "welcome": (
            "👋 Cześć! Pomogę znaleźć pracę w Polsce.\n\n"
            "Będę wysyłać nowe oferty na bieżąco z OLX, Praca.pl i GoWork.\n\n"
            "Wybierz język:"
        ),
        "choose_city": "🏙 Wybierz miasto:",
        "enter_city": "✏️ Wpisz miasto (np. Szczecin):",
        "choose_etat": "⏰ Wybierz etat (można kilka):\n\nPotem ✅ Gotowe",
        "choose_umowa": "📋 Wybierz umowę:",
        "saved": (
            "✅ Zapisane!\n\n"
            "🏙 Miasto: {city}\n"
            "⏰ Etat: {etat}\n"
            "📋 Umowa: {umowa}\n\n"
            "🔍 Szukam ofert na OLX, Praca.pl i GoWork..."
        ),
        "loading_city": "🔍 Szukam nowych ofert dla tego miasta...\nPoczekaj 30–60 sekund.",
        "no_jobs": "😔 Brak ofert. Sprawdzam co 15 min!",
        "menu_active": "🟢 Bot działa i szuka ofert. Przyciski poniżej 👇",
        "stop_donate": (
            "⏹ Wysyłka zatrzymana.\n\n"
            f"💳 <code>{DONATE_ACCOUNT}</code>\n\n"
            "Pytania i współpraca: @Hriaker1"
        ),
        "reset_msg": "🔄 Zresetowano! Zaczynamy od nowа.\n\nWybierz język:",
        "help": (
            "🤖 <b>Co robi bot:</b>\n\n"
            "Agreguje oferty pracy z OLX, Praca.pl i GoWork.\n\n"
            f"<b>{BTN_RESET}</b> — ustaw filtry od nowa\n"
            f"<b>{BTN_STOP}</b> — zatrzymaj wysyłkę\n"
            f"<b>{BTN_HELP}</b> — ta pomoc\n\n"
            "Pytania i współpraca: @Hriaker1"
        ),
        "already_stopped": "ℹ️ Nie masz subskrypcji. Naciśnij przycisk poniżej.",
        "btn_all": "🇵🇱 Cała Polska",
        "btn_custom": "✏️ Inne miasto",
        "btn_done": "✅ Gotowe",
        "after_initial": (
            "👆 To były ostatnie aktualne oferty z dzisiaj.\n\n"
            "🔄 Bot będzie teraz wysyłać tylko nowe oferty na bieżąco."
        ),
        "search_paused": (
            "⏸ <b>Wyszukiwanie wstrzymane</b>\n\n"
            "Korzystasz z bota już od 3 dni. Aby kontynuować darmowe otrzymywanie "
            "nowych ofert, potwierdź, że nadal szukasz pracy! 👇"
        ),
        "btn_continue": "🔄 Kontynuuj wyszukiwanie",
        "search_renewed": "🟢 Super! Wyszukiwanie zostało wznowione na kolejne 3 dni. Nowe oferty już wkrótce! 🚀",
    },
    "ua": {
        "welcome": (
            "👋 Привіт! Допоможу знайти роботу в Польщі.\n\n"
            "Бот надсилатиме нові вакансії з OLX, Praca.pl та GoWork.\n\n"
            "Обери мову:"
        ),
        "choose_city": "🏙 Обери місто:",
        "enter_city": "✏️ Напиши місто польською (наприклад: Szczecin):",
        "choose_etat": "⏰ Обери зайнятість (можна кілька):\n\nПотім ✅ Готово",
        "choose_umowa": "📋 Обери договір:",
        "saved": (
            "✅ Збережено!\n\n"
            "🏙 Місто: {city}\n"
            "⏰ Зайнятість: {etat}\n"
            "📋 Договір: {umowa}\n\n"
            "🔍 Шукаю вакансії на OLX, Praca.pl та GoWork..."
        ),
        "loading_city": "🔍 Шукаю свіжі вакансії для этого міста...\nЗачекай 30–60 секунд.",
        "no_jobs": "😔 Немає вакансій. Перевірю через 15 хв!",
        "menu_active": "🟢 Бот запущено і шукає вакансії. Кнопки керування нижче 👇",
        "stop_donate": (
            "⏹ Розсилку зупинено.\n\n"
            f"💳 <code>{DONATE_ACCOUNT}</code>\n\n"
            "Питання та співпраця: @Hriaker1"
        ),
        "reset_msg": "🔄 Скинуто! Починаємо заново.\n\nОбери мову:",
        "help": (
            "🤖 <b>Що вміє бот:</b>\n\n"
            "Агрегує публічні вакансії з OLX, Praca.pl та GoWork.\n\n"
            f"<b>{BTN_RESET}</b> — налаштувати фільтри заново\n"
            f"<b>{BTN_STOP}</b> — зупинити розсилку\n"
            f"<b>{BTN_HELP}</b> — ця довідка\n\n"
            "Питання та співпраця: @Hriaker1"
        ),
        "already_stopped": "ℹ️ Ти не підписаний. Натисни кнопку нижче.",
        "btn_all": "🇵🇱 Вся Польща",
        "btn_custom": "✏️ Своє місто",
        "btn_done": "✅ Готово",
        "after_initial": (
            "👆 Це були останні актуальные вакансії за сьогодні.\n\n"
            "🔄 Тепер бот надсилатиме лише нові вакансії щойно они з'являться."
        ),
        "search_paused": (
            "⏸ <b>Пошук тимчасово призупинено</b>\n\n"
            "Ти користуєшся пошуком вже 3 дні. Щоб бот продовжував надсилати "
            "тобі свежие вакансії безкоштовно, підтвердь, що ти досі шукаєш роботу! 👇"
        ),
        "btn_continue": "🔄 Продовжити пошук",
        "search_renewed": "🟢 Чудово! Пошук успішно відновлено ще на 3 дні. Свіжі вакансії вже летять до тебе! 🚀",
    },
}


def t(lang, key, **kwargs):
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, "")
    return text.format(**kwargs) if kwargs else text


def get_user_lang(tid):
    user = db_get_user(tid)
    return user.get("language", "ru") if user else "ru"


def get_city_slug(city):
    if not city:
        return ""
    cl = city.lower().strip()
    if cl in CITY_SLUGS:
        return CITY_SLUGS[cl]
    for k, v in {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l',
        'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z',
        'ż': 'z', ' ': '-'
    }.items():
        cl = cl.replace(k, v)
    return cl


def city_matches(job_city, filter_city):
    if not filter_city or filter_city == "all":
        return True
    if not job_city:
        return False
    fc = get_city_slug(filter_city)
    jc = get_city_slug(job_city)
    return fc == jc or fc in jc or jc in fc


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
    if isinstance(umowa, list):
        umowa = " ".join(str(v) for v in umowa)
    u = str(umowa).lower().strip()
    if u in ["umowa_o_prace", "umowa_zlecenie", "umowa_o_dzielo", "b2b", "staz"]:
        return u
    u = u.replace("_", " ").replace("-", " ")
    if "zlecenie" in u: return "umowa_zlecenie"
    if "o pracę" in u or "o prace" in u: return "umowa_o_prace"
    if "b2b" in u or "selfemployment" in u or "kontrakt b2b" in u: return "b2b"
    if "dzieło" in u or "dzielo" in u: return "umowa_o_dzielo"
    if "staż" in u or "staz" in u or "praktyк" in u: return "staz"
    return None


def normalize_etat(etat):
    if not etat:
        return None
    if isinstance(etat, list):
        etat = " ".join(str(v) for v in etat)
    e = str(etat).lower().strip()
    if e in ["full", "part"]:
        return e
    e = e.replace("_", " ").replace("-", " ")
    if any(x in e for x in [
        "parttime", "part time", "niepełny", "niepelny",
        "1/2", "pół etatu", "pol etatu", "3/4",
        "część etatu", "czesc etatu", "tymczasowa", "dodatkowa"
    ]):
        return "part"
    if any(x in e for x in [
        "fulltime", "full time", "pełny", "pelny",
        "cały etat", "caly etat"
    ]):
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
    text = f"{title}"
    return any(kw in text for kw in BLOCKED_KEYWORDS)


def is_night_time() -> bool:
    """Определяет, ночь ли в Польше (23:00 - 08:00)"""
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset_hours = 2 if 3 < month < 11 else 1
    local_time = now_utc + timedelta(hours=offset_hours)
    return local_time.hour >= 23 or local_time.hour < 8


# ==================== DATABASE ====================

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
        r = supabase.table("users").select("telegram_id, language, is_active").eq("telegram_id", tid).execute()
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
        now_str = datetime.now(timezone.utc).isoformat()
        supabase.table("user_filters").upsert(
            {
                "telegram_id": tid,
                "city": city,
                "etat_full": ef,
                "etat_part": ep,
                "umowa": umowa,
                "is_paused": False,
                "last_renewal": now_str
            },
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
        r = supabase.table("user_filters").select("telegram_id, city, umowa, etat_full, etat_part, is_paused, last_renewal").eq("telegram_id", tid).execute()
        return r.data[0] if r.data else None
    except:
        return None


def db_get_active_filters():
    try:
        r = supabase.table("user_filters") \
            .select("telegram_id, city, umowa, etat_full, etat_part, is_paused, last_renewal") \
            .eq("is_paused", False).execute()
        return r.data or []
    except:
        return []


def db_get_all_active_users():
    """Получает всех уникальных активных юзеров из базы для админской рассылки"""
    try:
        r = supabase.table("users").select("telegram_id").eq("is_active", True).execute()
        return [row["telegram_id"] for row in r.data] if r.data else []
    except Exception as e:
        logger.error(f"db_get_all_active_users error: {e}")
        return []


# ==================== SENT STATUS OPTIMIZATION ====================

def db_get_sent_job_ids(tid) -> set:
    try:
        r = supabase.table("sent_jobs").select("job_id").eq("telegram_id", tid).execute()
        return {row["job_id"] for row in r.data} if r.data else set()
    except Exception as e:
        logger.error(f"db_get_sent_job_ids error for {tid}: {e}")
        return set()


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


def db_renew_search_filter(tid):
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        supabase.table("user_filters").update({
            "is_paused": False,
            "last_renewal": now_str
        }).eq("telegram_id", tid).execute()
        return True
    except Exception as e:
        logger.error(f"db_renew_search_filter: {e}")
        return False


def db_pause_search_filter(tid):
    try:
        supabase.table("user_filters").update({
            "is_paused": True
        }).eq("telegram_id", tid).execute()
        return True
    except Exception as e:
        logger.error(f"db_pause_search_filter: {e}")
        return False


def db_get_jobs_for_city(city, limit=150, hours=24):
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        fields = "id, external_id, title, city, salary, url, source, umowa, etat"

        if city == "all":
            r = supabase.table("jobs").select(fields) \
                .gt("created_at", cutoff) \
                .order("created_at", desc=True) \
                .limit(limit).execute()
            return r.data or []

        r = supabase.table("jobs").select(fields) \
            .ilike("city", f"%{city}%") \
            .gt("created_at", cutoff) \
            .order("created_at", desc=True) \
            .limit(limit).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"db_get_jobs_for_city: {e}")
        return []


# ==================== GITHUB ACTIONS TRIGGER ====================

async def trigger_scraper_for_city(city: str) -> bool:
    if not GITHUB_TRIGGER_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.warning("GitHub trigger vars missing — skipping on-demand scrape")
        return False

    url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TRIGGER_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": GITHUB_REF, "inputs": {"city": city}}

    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                if resp.status in (200, 201, 204):
                    logger.info(f"✅ GitHub workflow dispatched for city={city}")
                    return True
                txt = await resp.text()
                logger.error(f"GitHub dispatch failed {resp.status}: {txt}")
                return False
    except Exception as e:
        logger.error(f"trigger_scraper_for_city: {e}")
        return False


async def wait_for_city_jobs(city: str, attempts: int = 10, delay: int = 6):
    for i in range(attempts):
        await asyncio.sleep(delay)
        jobs = db_get_jobs_for_city(city, limit=150, hours=1)
        if jobs:
            logger.info(f"✅ Got {len(jobs)} jobs for {city} after {(i+1)*delay}s")
            return jobs
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


# ==================== FORMAT ====================

def format_job(job):
    ut = UMOWY_DISPLAY.get(job.get("umowa")) or job.get("umowa")
    et = ETAT_DISPLAY.get(job.get("etat")) or job.get("etat")
    lines = [f"💼 <b>{strip_html(job.get('title', ''))}</b>"]
    
    if ut:
        lines.append(f"📄 {strip_html(str(ut))}")
    if et:
        lines.append(f"⏰ {strip_html(str(et))}")
    if job.get("city"):
        lines.append(f"📍 {strip_html(job['city'])}")
    if job.get("salary"):
        lines.append(f"💰 {strip_html(job['salary'])}")
    lines.append(f"📌 {job.get('source', '—')}")
    if job.get("url"):
        lines.append(f"🔗 <a href='{job['url']}'>Открыть вакансию</a>")
    lines.append("")
    lines.append(
        "🤖 <a href='https://t.me/szukam_pracy_bot'>@szukam_pracy_bot</a> "
        "— свежие вакансии в Польше 🇵🇱"
    )
    return "\n".join(lines)


async def send_promo(chat_id):
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Получить 50 PLN и начать", url=REF_LINK)
        ]])
        sent_msg = await bot.send_message(
            chat_id, PROMO_TEXT, reply_markup=kb, parse_mode="HTML"
        )
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=sent_msg.message_id,
                disable_notification=True
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"promo: {e}")


async def send_jobs_to_user(tid, jobs, user_filter=None, limit=15, is_initial=False):
    sent, sf, ss, blocked = 0, 0, 0, 0

    already_sent_ids = db_get_sent_job_ids(tid)

    for job in jobs:
        if sent >= limit:
            break
        if is_delivery_job(job):
            blocked += 1
            continue
        if user_filter and not job_matches_filter(job, user_filter):
            sf += 1
            continue
        if job['id'] in already_sent_ids:
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
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"send: {e}")

    logger.info(f"Sent={sent} filtered={sf} already={ss} blocked={blocked}")

    if is_initial:
        lang = get_user_lang(tid)
        if sent > 0:
            try:
                await bot.send_message(tid, t(lang, "after_initial"))
            except Exception:
                pass
        else:
            try:
                await bot.send_message(tid, t(lang, "no_jobs"))
            except Exception:
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


def kb_renew_search(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn_continue"), callback_data="renew_search")
    ]])


# ==================== BACKGROUND AD BROADCASTER ====================

async def run_broadcast(bot: Bot, admin_id: int, from_chat_id: int, message_id: int, users: list):
    sent = 0
    failed = 0
    
    for uid in users:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=from_chat_id,
                message_id=message_id
            )
            sent += 1
            await asyncio.sleep(0.05) 
        except Exception as e:
            db_set_user_active(uid, False)
            failed += 1
            logger.warning(f"Failed to copy message to {uid}: {e}")
            
    try:
        await bot.send_message(
            admin_id,
            f"📢 <b>Рассылка успешно завершена!</b>\n\n"
            f"✅ Получили сообщение: {sent}\n"
            f"❌ Не доставлено (заблокировали/ошибки): {failed}"
        )
    except Exception as e:
        logger.error(f"Failed to send broadcast report to admin: {e}")


# ==================== HANDLERS ====================

# --- СЕКРЕТНЫЕ АДМИН-ХЕНДЛЕРЫ ---

@router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminStates.waiting_for_ad)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")
    ]])
    await m.answer(
        "👑 <b>Секретная админ-панель</b>\n\n"
        "Отправь мне сообщение для рассылки. Это может быть всё что угодно:\n"
        "• Обычный текст\n"
        "• Сообщение с картинкой/файлом\n"
        "• Текст с разметкой (жирный, курсив, ссылки)\n"
        "• Пересланный откуда-то пост\n\n"
        "Я покажу тебе превью перед отправкой. Для выхода нажми кнопку ниже.",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_cancel", F.from_user.id == ADMIN_ID)
async def admin_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("❌ Создание рассылки отменено.")
    await c.answer()


@router.message(AdminStates.waiting_for_ad, F.from_user.id == ADMIN_ID)
async def admin_get_ad(m: Message, state: FSMContext):
    try:
        logger.info(f"Admin triggered ad preview for message_id {m.message_id}")
        
        # Исправлено: используем m.chat.id
        await state.update_data(ad_msg_id=m.message_id, ad_chat_id=m.chat.id)
        await state.set_state(AdminStates.confirm_ad)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать рассылку", callback_data="admin_send")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]
        ])
        
        await m.answer("👇 <b>Вот как твой пост будет выглядеть у пользователей:</b>")
        
        # Исправлено: передаем m.chat.id
        await bot.copy_message(
            chat_id=m.chat.id,
            from_chat_id=m.chat.id,
            message_id=m.message_id
        )
        
        await m.answer(
            "Если всё выглядит правильно, нажми кнопку ниже, чтобы запустить массовую отправку.",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Error in admin_get_ad handler: {e}")
        await m.answer(f"❌ Произошла ошибка при генерации превью: {e}")


@router.callback_query(AdminStates.confirm_ad, F.data == "admin_send", F.from_user.id == ADMIN_ID)
async def admin_send_ad(c: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        msg_id = data.get("ad_msg_id")
        chat_id = data.get("ad_chat_id")
        await state.clear()
        
        if not msg_id or not chat_id:
            await c.message.edit_text("❌ Произошла ошибка (сообщение не найдено в кэше). Пожалуйста, введи /admin заново.")
            await c.answer()
            return
            
        await c.message.edit_text("⏳ Считываю список активных пользователей из базы данных...")
        await c.answer()
        
        users = db_get_all_active_users()
        
        if not users:
            await c.message.answer("❌ В базе данных нет ни одного активного пользователя!")
            return
            
        await c.message.answer(
            f"🚀 Массовая рассылка для <b>{len(users)}</b> пользователей успешно запущена "
            f"в фоновом режиме.\n\nБот продолжит бесперебойно работать, "
            f"а я напишу тебе сюда сразу же по завершении отправки!",
            parse_mode="HTML"
        )
        
        asyncio.create_task(run_broadcast(bot, ADMIN_ID, chat_id, msg_id, users))
        
    except Exception as e:
        logger.error(f"Error in admin_send_ad callback: {e}")
        await c.message.answer(f"❌ Ошибка запуска рассылки: {e}")


# --- СТАНДАРТНЫЕ ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ ---

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


# --- Кнопки нижнего меню ---
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


# --- Inline flow ---
@router.callback_query(SetupStates.lang, F.data.startswith("l_"))
async def on_lang(c: CallbackQuery, state: FSMContext):
    lang = c.data[2:]
    await state.update_data(lang=lang)
    try:
        supabase.table("users").update({"language": lang}).eq(
            "telegram_id", c.from_user.id).execute()
    except Exception:
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


@router.message(
    SetupStates.city_custom,
    ~F.text.in_({BTN_RESET, BTN_STOP, BTN_HELP, BTN_RESTART})
)
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
    if sel.get("full"): ep.append("Pełny etat")
    if sel.get("part"): ep.append("Niepełny etat")
    ed = ", ".join(ep) if ep else "Pełny etat"
    ud = next((n for n, v in UMOWY if v == uv), uv)

    db_upsert_filter(c.from_user.id, city, sel.get("full", True), sel.get("part", False), uv)
    await state.clear()
    uf = {"umowa": uv, "etat_full": sel.get("full", True), "etat_part": sel.get("part", False)}

    await c.message.edit_text(t(lang, "saved", city=cd, etat=ed, umowa=ud))
    await c.answer()

    await bot.send_message(c.from_user.id, t(lang, "menu_active"), reply_markup=kb_active_menu())
    await send_promo(c.from_user.id)
    await asyncio.sleep(1)

    jobs = db_get_jobs_for_city(city, limit=150, hours=24)

    if not jobs and city != "all":
        await bot.send_message(c.from_user.id, t(lang, "loading_city"))
        ok = await trigger_scraper_for_city(city)
        if ok:
            jobs = await wait_for_city_jobs(city)

    await send_jobs_to_user(
        c.from_user.id, jobs,
        user_filter=uf, limit=8, is_initial=True
    )


@router.callback_query(F.data == "renew_search")
async def on_renew_search(c: CallbackQuery):
    tid = c.from_user.id
    lang = get_user_lang(tid)
    
    ok = db_renew_search_filter(tid)
    if ok:
        await c.message.edit_text(t(lang, "search_renewed"), parse_mode="HTML")
    else:
        await c.answer("Error. Try again.", show_alert=True)
    await c.answer()


# ==================== SCHEDULER ====================

async def scheduled_check():
    """
    Каждые 15 минут проверяет новые вакансии в базе.
    """
    if is_night_time():
        logger.info("🌙 Night time — skipping scheduled check.")
        return

    logger.info("⏰ Check started")
    
    filters = db_get_active_filters()
    if not filters:
        logger.info("No active filters found.")
        return

    now = datetime.now(timezone.utc)
    active_filters = []

    for f in filters:
        tid = f["telegram_id"]
        lang = get_user_lang(tid)

        last_renewal_str = f.get("last_renewal")
        if last_renewal_str:
            try:
                last_renewal = datetime.fromisoformat(last_renewal_str.replace("Z", "+00:00"))
                if (now - last_renewal).total_seconds() > 259200:
                    db_pause_search_filter(tid)
                    try:
                        await bot.send_message(
                            tid, 
                            t(lang, "search_paused"), 
                            parse_mode="HTML", 
                            reply_markup=kb_renew_search(lang)
                        )
                        logger.info(f"⏸ Paused user {tid} due to 3-day inactivity.")
                    except Exception as e:
                        logger.error(f"Failed to send pause notification to {tid}: {e}")
                    continue
            except Exception as e:
                logger.error(f"Error parsing last_renewal for {tid}: {e}")

        active_filters.append(f)

    if not active_filters:
        return

    cities = list(set(f.get("city", "all") for f in active_filters))
    city_jobs = {}
    for city in cities:
        # ИСПРАВЛЕНО: ищем вакансии за последние 2 часа, чтобы ничего не терять из-за задержек парсера
        city_jobs[city] = db_get_jobs_for_city(city, limit=100, hours=2)
        await asyncio.sleep(0.5)

    for f in active_filters:
        tid = f["telegram_id"]
        city = f.get("city", "all")
        jobs = city_jobs.get(city, [])
        uf = {
            "umowa": f.get("umowa", "any"),
            "etat_full": f.get("etat_full", True),
            "etat_part": f.get("etat_part", False),
        }
        if jobs:
            sent = await send_jobs_to_user(tid, jobs, user_filter=uf, limit=15)
            if sent > 0:
                logger.info(f"Sent {sent} to {tid}")

    logger.info("✅ Done")


# ==================== MAIN ====================

async def main():
    logger.info("🚀 Bot starting...")
    await start_web_server()

    s = AsyncIOScheduler()
    s.add_job(scheduled_check, "interval", minutes=15, id="check", replace_existing=True)
    s.start()

    logger.info("⏰ Scheduler started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
