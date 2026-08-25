import os
import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
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
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
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

# Локальные блокировки для каждого пользователя, чтобы исключить Race Condition при отправке
USER_LOCKS = {}

def get_user_lock(tid: int) -> asyncio.Lock:
    if tid not in USER_LOCKS:
        USER_LOCKS[tid] = asyncio.Lock()
    return USER_LOCKS[tid]

REF_LINK = "https://kierowca.mbpartners.pl/rejestracja/?invitation=048A521B"
DONATE_ACCOUNT = "84 9511 0000 0052 9681 3000 0010"

PROMO_TEXT = (
    "💼 <b>Ищешь подработку с гибким графиком в Польше?</b>\n\n"
    "Подключайся к доставке через <b>MB Partners</b> и выходи на заказы в "
    "<b>Glovo / Uber Eats / Bolt Food</b>.\n\n"
    "Что по условиям:\n"
    "• свободный график — можно совмещать с учёбой или основной работой\n"
    "• выплаты каждую неделю на карту\n"
    "• можно работать на своём авто, велосипеде или самокате\n"
    "• быстрый старт через проверенного партнёра\n\n"
    "🎁 <b>Бонус для новых:</b> 50 PLN на баланс при регистрации\n"
    "🏷 <b>Промокод:</b> 048A521B\n\n"
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


# Состояния для админки
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
    ("Staż / Praktyki", "staz"),
]

UMOWY_DISPLAY = {
    "umowa_o_prace": "Umowa o pracę",
    "umowa_zlecenie": "Umowa zlecenie",
    "umowa_o_dzielo": "Umowa o dzieło",
    "b2b": "B2B",
    "staz": "Staż / Практики",
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
            "с OLX и Praca.pl.\n\n"
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
            "Będę wysyłać nowe oferty na bieżąco z OLX i Praca.pl.\n\n"
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
        "search_renewed": "🟢 Super! Wyszukiwanie zostało wznowione na kolejne 3 dni. Nowе oferty już wkrótce! 🚀",
    },
    "ua": {
        "welcome": (
            "👋 Привіт! Допоможу знайти роботу в Польщі.\n\n"
            "Бот надсилатиме нові вакансії з OLX та Praca.pl.\n\n"
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


def get_all_job_umowas(raw_umowa):
    if not raw_umowa:
        return []
    u = str(raw_umowa).lower().strip().replace("_", " ").replace("-", " ")
    found = []
    if "zlecenie" in u or "zlecenia" in u or "uz" in u:
        found.append("umowa_zlecenie")
    if "o pracę" in u or "o prace" in u or "uop" in u or "praca" in u:
        found.append("umowa_o_prace")
    if "b2b" in u or "kontrakt" in u or "business" in u:
        found.append("b2b")
    if "dzieło" in u or "dzielo" in u or "uod" in u:
        found.append("umowa_o_dzielo")
    if "staż" in u or "staz" in u or "praktyк" in u or "praktyki" in u or "internship" in u:
        found.append("staz")
    return found


def normalize_etat(etat):
    if not etat:
        return None
    e = str(etat).lower().strip()
    if any(x in e for x in ["part", "niepełny", "niepelny", "1/2", "3/4", "1/4", "pół etatu", "pol etatu", "dodatkowa"]):
        return "part"
    if any(x in e for x in ["full", "pełny", "pelny", "pełен", "pelen", "cały etat", "caly etat"]):
        return "full"
    return None


# ==================== УМНЫЙ ЛОЯЛЬНЫЙ ФИЛЬТР ====================

def job_matches_filter(job, user_filter):
    uf = user_filter.get("umowa", "any")
    ef = user_filter.get("etat_full", True)
    ep = user_filter.get("etat_part", True)
    
    job_umowas = get_all_job_umowas(job.get("umowa"))
    job_etat = normalize_etat(job.get("etat"))

    if uf != "any":
        if job_umowas and uf not in job_umowas:
            return False
        if not job_umowas and uf in ["b2b", "staz", "umowa_o_dzielo"]:
            return False

    if not (ef and ep):
        if job_etat:
            if ep and not ef and job_etat == "full":
                return False
            if ef and not ep and job_etat == "part":
                return False
        elif ep and not ef:
            return False

    return True


def is_delivery_job(job):
    title = (job.get("title") or "").lower()
    return any(kw in title for kw in BLOCKED_KEYWORDS)


def is_invalid_olx_url(url: str) -> bool:
    if not url:
        return True
    
    url_lower = url.lower()
    if "olx.pl" in url_lower:
        if "/oferta/" not in url_lower:
            return True
        if "/uzytkownik/" in url_lower:
            return True
        
    return False


def is_night_time() -> bool:
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset_hours = 2 if 3 < month < 11 else 1
    local_time = now_utc + timedelta(hours=offset_hours)
    return local_time.hour >= 23 or local_time.hour < 8


def parse_iso_datetime(dt_str: str) -> datetime:
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


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
    try:
        r = supabase.table("users").select("telegram_id").eq("is_active", True).execute()
        return [row["telegram_id"] for row in r.data] if r.data else []
    except Exception as e:
        logger.error(f"db_get_all_active_users error: {e}")
        return []


def db_get_sent_job_ids(tid) -> set:
    """С повторной попыткой при ошибке [Errno 11] Resource temporarily unavailable"""
    for attempt in range(3):
        try:
            r = supabase.table("sent_jobs").select("job_id").eq("telegram_id", tid).execute()
            return {row["job_id"] for row in r.data} if r.data else set()
        except Exception as e:
            err_text = str(e)
            if ("errno 11" in err_text.lower() or "temporarily unavailable" in err_text.lower()) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.error(f"db_get_sent_job_ids error for {tid}: {e}")
            return set()
    return set()


def db_mark_sent_batch(tid, job_ids: list) -> bool:
    """С повторной попыткой при ошибке [Errno 11] Resource temporarily unavailable"""
    if not job_ids:
        return True
    for attempt in range(3):
        try:
            records = [{"telegram_id": tid, "job_id": jid} for jid in job_ids]
            supabase.table("sent_jobs").upsert(
                records,
                on_conflict="telegram_id,job_id",
                ignore_duplicates=True
            ).execute()
            return True
        except Exception as e:
            err_text = str(e)
            if ("errno 11" in err_text.lower() or "temporarily unavailable" in err_text.lower()) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.error(f"db_mark_sent_batch error for {tid}: {e}")
            return False
    return False


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
    """С повторной попыткой при ошибке [Errno 11] Resource temporarily unavailable"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    fields = "id, external_id, title, city, salary, url, source, umowa, etat, created_at"
    for attempt in range(3):
        try:
            if city == "all":
                r = supabase.table("jobs").select(fields).gt("created_at", cutoff).order("created_at", desc=True).limit(limit).execute()
                return r.data or []

            slug = get_city_slug(city)
            r = supabase.table("jobs") \
                .select(fields) \
                .or_(f"city.ilike.%{city}%,city.ilike.%{slug}%") \
                .gt("created_at", cutoff) \
                .order("created_at", desc=True) \
                .limit(limit) \
                .execute()
            return r.data or []
        except Exception as e:
            err_text = str(e)
            if ("errno 11" in err_text.lower() or "temporarily unavailable" in err_text.lower()) and attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            logger.error(f"db_get_jobs_for_city error: {e}")
            return []
    return []


def db_get_bot_stats() -> dict:
    """
    Возвращает реальную статистику бота из Supabase.
    total = все зарегистрированные за всё время
    active = те, кто не заблокировал бота (is_active = true)
    """
    try:
        total_res = supabase.table("users").select("telegram_id", count="exact").limit(1).execute()
        active_res = supabase.table("users").select("telegram_id", count="exact").eq("is_active", True).limit(1).execute()
        
        total = total_res.count if total_res.count is not None else 0
        active = active_res.count if active_res.count is not None else 0
        
        return {"total": total, "active": active}
    except Exception as e:
        logger.error(f"db_get_bot_stats error: {e}")
        return {"total": 0, "active": 0}


# ==================== GITHUB TRIGGER ====================

async def trigger_scraper_for_city(city: str) -> bool:
    if not GITHUB_TRIGGER_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        return False

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TRIGGER_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"ref": GITHUB_REF, "inputs": {"city": city}}

    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                return resp.status in (200, 201, 204)
    except Exception:
        return False


async def wait_for_city_jobs(city: str, attempts: int = 10, delay: int = 6):
    for i in range(attempts):
        await asyncio.sleep(delay)
        jobs = await asyncio.to_thread(db_get_jobs_for_city, city, 150, 1)
        if jobs:
            return jobs
    return []


# ==================== WEB SERVER ====================

async def health_check(request):
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/ping", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port}")


# ==================== FORMAT & SEND ====================

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
    lines.append("🤖 <a href='https://t.me/szukam_pracy_bot'>@szukam_pracy_bot</a> — свежие вакансии в <a href='https://t.me/szukam_pracy_bot'>Польше 🇵🇱</a>")
    return "\n".join(lines)


async def send_promo(chat_id):
    try:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Начать", url=REF_LINK)]])
        sent_msg = await bot.send_message(chat_id, PROMO_TEXT, reply_markup=kb, parse_mode="HTML")
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"promo error: {e}")


async def send_jobs_to_user(tid, jobs, user_filter=None, limit=15, is_initial=False):
    async with get_user_lock(tid):
        sent, sf, ss, blocked = 0, 0, 0, 0
        already_sent_ids = await asyncio.to_thread(db_get_sent_job_ids, tid)
        sent_job_ids_batch = []

        for job in jobs:
            if sent >= limit:
                break

            job_id = job.get("id")
            if job_id is None:
                continue

            if is_invalid_olx_url(job.get("url")):
                continue

            if is_delivery_job(job):
                blocked += 1
                continue

            if user_filter and not job_matches_filter(job, user_filter):
                sf += 1
                continue

            if job_id in already_sent_ids:
                ss += 1
                continue

            try:
                await bot.send_message(
                    tid,
                    format_job(job),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent_job_ids_batch.append(job_id)
                already_sent_ids.add(job_id)
                sent += 1

                await asyncio.sleep(0.05)

            except TelegramRetryAfter as e:
                retry_after = max(1, int(getattr(e, "retry_after", 1)))
                logger.warning(f"Telegram rate limit for {tid}; sleeping {retry_after}s")
                await asyncio.sleep(retry_after)
                try:
                    await bot.send_message(
                        tid,
                        format_job(job),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    sent_job_ids_batch.append(job_id)
                    already_sent_ids.add(job_id)
                    sent += 1
                except TelegramForbiddenError:
                    logger.warning(f"send to {tid}: bot was blocked by user")
                    await asyncio.to_thread(db_set_user_active, tid, False)
                    await asyncio.to_thread(db_delete_filter, tid)
                    blocked += 1
                    break
                except Exception as retry_error:
                    logger.warning(f"retry send to {tid} error: {retry_error}")
                    break

            except TelegramForbiddenError:
                logger.warning(f"send to {tid}: bot was blocked by user")
                await asyncio.to_thread(db_set_user_active, tid, False)
                await asyncio.to_thread(db_delete_filter, tid)
                blocked += 1
                break

            except Exception as e:
                logger.warning(f"send to {tid} error: {e}")
                break

        if sent_job_ids_batch:
            ok = await asyncio.to_thread(db_mark_sent_batch, tid, sent_job_ids_batch)
            if not ok:
                logger.error(
                    f"sent_jobs insert failed for {tid}; {len(sent_job_ids_batch)} jobs "
                    "were delivered but not marked as sent"
                )

        logger.info(
            f"User {tid}: Sent={sent} filtered={sf} already={ss} blocked={blocked}"
        )

        if is_initial:
            lang = await asyncio.to_thread(get_user_lang, tid)
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
        [InlineKeyboardButton(text=n, callback_data=f"u_{v}")] for n, v in UMOWY
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
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_RESTART)]], resize_keyboard=True)


def kb_renew_search(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "btn_continue"), callback_data="renew_search")]])


# ==================== BROADCASTER ====================

async def run_broadcast(bot: Bot, admin_id: int, from_chat_id: int, message_id: int, users: list):
    sent, failed = 0, 0
    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            await asyncio.to_thread(db_set_user_active, uid, False)
            failed += 1
            
    try:
        await bot.send_message(
            admin_id,
            f"📢 <b>Рассылка успешно завершена!</b>\n\n✅ Получили: {sent}\n❌ Заблокировали: {failed}"
        )
    except Exception:
        pass


# ==================== VIP AUTOMATIC GITHUB TRIGGER ====================

async def auto_trigger_github_scraper():
    """
    Каждые 25 минут пинает GitHub через VIP API для мгновенного и точного парсинга по расписанию.
    """
    if is_night_time():
        logger.info("🌙 Night time — skipping auto-trigger of GitHub Scraper.")
        return
        
    logger.info("🚀 Triggering scheduled instant scrape via GitHub API...")
    # Передаем пустую строку, чтобы запустить ПОЛНЫЙ сбор по всем активным городам!
    ok = await trigger_scraper_for_city("")
    if ok:
        logger.info("✅ GitHub Scraper successfully triggered via VIP API!")
    else:
        logger.warning("⚠️ Failed to trigger GitHub Scraper via API.")


# ==================== AUTOMATIC BOT DESCRIPTION UPDATE ====================

async def update_bot_description():
    """
    Раз в час обновляет описание бота (Short Description / What can this bot do?) в Telegram,
    подставляя реальную статистику базы данных. Картинки/медиа при этом не затрагиваются.
    """
    try:
        stats = await asyncio.to_thread(db_get_bot_stats)
        total = stats["total"]
        active = stats["active"]
        
        description_text = (
            "Зачем пахать над поиском работы, чилль на диване "
            "пока твой цифровой раб пылесосит вакансии 24/7\n\n"
            f"👥 Всего: {total} / 🟢 Активных: {active}"
        )
        
        # Обновляем описание для дефолтного и основных языковых кодов
        await bot.set_my_description(description=description_text)
        await bot.set_my_description(description=description_text, language_code="ru")
        await bot.set_my_description(description=description_text, language_code="pl")
        await bot.set_my_description(description=description_text, language_code="uk")
        
        logger.info(f"✅ Bot Description updated: {total} total / {active} active")
    except Exception as e:
        logger.error(f"Failed to update bot description: {e}")


# ==================== HANDLERS ====================

@router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def cmd_admin(m: Message, state: FSMContext):
    try:
        await state.clear()
        await state.set_state(AdminStates.waiting_for_ad)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]])
        await m.answer("👑 <b>Админ-панель:</b>\n\nОтправь мне сообщение для рассылки.", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning(f"cmd_admin error: {e}")


@router.callback_query(F.data == "admin_cancel", F.from_user.id == ADMIN_ID)
async def admin_cancel(c: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await c.message.edit_text("❌ Рассылка отменена.")
        await c.answer()
    except Exception as e:
        logger.warning(f"admin_cancel error: {e}")


@router.message(AdminStates.waiting_for_ad, F.from_user.id == ADMIN_ID)
async def admin_get_ad(m: Message, state: FSMContext):
    try:
        await state.update_data(ad_msg_id=m.message_id, ad_chat_id=m.chat.id)
        await state.set_state(AdminStates.confirm_ad)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать рассылку", callback_data="admin_send")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")]
        ])
        await m.answer("👇 <b>Превью поста:</b>")
        await bot.copy_message(chat_id=m.chat.id, from_chat_id=m.chat.id, message_id=m.message_id)
        await m.answer("Запустить отправку?", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")


@router.callback_query(AdminStates.confirm_ad, F.data == "admin_send", F.from_user.id == ADMIN_ID)
async def admin_send_ad(c: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        msg_id, chat_id = data.get("ad_msg_id"), data.get("ad_chat_id")
        await state.clear()
        
        users = await asyncio.to_thread(db_get_all_active_users)
        if not users:
            await c.message.answer("❌ Нет активных пользователей!")
            return
            
        await c.message.answer(f"🚀 Рассылка для <b>{len(users)}</b> пользователей запущена!", parse_mode="HTML")
        asyncio.create_task(run_broadcast(bot, ADMIN_ID, chat_id, msg_id, users))
    except Exception as e:
        await c.message.answer(f"❌ Ошибка: {e}")


@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    try:
        await state.clear()
        await asyncio.to_thread(db_upsert_user, m.from_user.id, m.from_user.username)
        await asyncio.to_thread(db_set_user_active, m.from_user.id, True)
        await asyncio.to_thread(db_clear_sent, m.from_user.id)
        await state.update_data(lang="ru", etat={"full": False, "part": False})
        await state.set_state(SetupStates.lang)
        await m.answer("⚙️", reply_markup=ReplyKeyboardRemove())
        await m.answer(t("ru", "welcome"), reply_markup=kb_lang())
    except Exception as e:
        logger.warning(f"cmd_start error for {m.from_user.id}: {e}")


@router.message(Command("reset"))
async def cmd_reset(m: Message, state: FSMContext):
    try:
        await state.clear()
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        await asyncio.to_thread(db_delete_filter, m.from_user.id)
        await asyncio.to_thread(db_clear_sent, m.from_user.id)
        await asyncio.to_thread(db_set_user_active, m.from_user.id, True)
        await state.update_data(lang=lang, etat={"full": False, "part": False})
        await state.set_state(SetupStates.lang)
        await m.answer("⚙️", reply_markup=ReplyKeyboardRemove())
        await m.answer(t(lang, "reset_msg"), reply_markup=kb_lang())
    except Exception as e:
        logger.warning(f"cmd_reset error: {e}")


@router.message(Command("stop"))
async def cmd_stop(m: Message, state: FSMContext):
    try:
        await state.clear()
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        has_filter = await asyncio.to_thread(db_get_filter, m.from_user.id)
        if not has_filter:
            await m.answer(t(lang, "already_stopped"), reply_markup=kb_stopped_menu())
            return
        await asyncio.to_thread(db_delete_filter, m.from_user.id)
        await asyncio.to_thread(db_set_user_active, m.from_user.id, False)
        await m.answer(t(lang, "stop_donate"), parse_mode="HTML", reply_markup=kb_stopped_menu())
    except Exception as e:
        logger.warning(f"cmd_stop error: {e}")


@router.message(Command("help"))
async def cmd_help(m: Message):
    try:
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        await m.answer(t(lang, "help"), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"cmd_help error: {e}")


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
    try:
        lang = c.data[2:]
        await state.update_data(lang=lang)
        await asyncio.to_thread(
            lambda: supabase.table("users").update({"language": lang}).eq("telegram_id", c.from_user.id).execute()
        )
        await state.set_state(SetupStates.city)
        await c.message.edit_text(t(lang, "choose_city"), reply_markup=kb_cities(lang))
        await c.answer()
    except Exception as e:
        logger.warning(f"on_lang error: {e}")


@router.callback_query(SetupStates.city, F.data.startswith("c_"))
async def on_city(c: CallbackQuery, state: FSMContext):
    try:
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
    except Exception as e:
        logger.warning(f"on_city error: {e}")


@router.message(SetupStates.city_custom, ~F.text.in_({BTN_RESET, BTN_STOP, BTN_HELP, BTN_RESTART}))
async def on_city_custom(m: Message, state: FSMContext):
    try:
        data = await state.get_data()
        lang = data.get("lang", "ru")
        city = m.text.strip()
        await state.update_data(city=city, city_display=city)
        await state.set_state(SetupStates.etat)
        sel = data.get("etat", {"full": False, "part": False})
        await m.answer(t(lang, "choose_etat"), reply_markup=kb_etat(lang, sel))
    except Exception as e:
        logger.warning(f"on_city_custom error: {e}")


@router.callback_query(SetupStates.etat, F.data.startswith("e_"))
async def on_etat(c: CallbackQuery, state: FSMContext):
    try:
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
    except Exception as e:
        logger.warning(f"on_etat error: {e}")


@router.callback_query(SetupStates.umowa, F.data.startswith("u_"))
async def on_umowa(c: CallbackQuery, state: FSMContext):
    try:
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

        await asyncio.to_thread(db_upsert_filter, c.from_user.id, city, sel.get("full", True), sel.get("part", False), uv)
        await state.clear()
        uf = {"umowa": uv, "etat_full": sel.get("full", True), "etat_part": sel.get("part", False)}

        await c.message.edit_text(t(lang, "saved", city=cd, etat=ed, umowa=ud))
        await c.answer()

        await bot.send_message(c.from_user.id, t(lang, "menu_active"), reply_markup=kb_active_menu())
        await send_promo(c.from_user.id)
        await asyncio.sleep(1)

        jobs = await asyncio.to_thread(db_get_jobs_for_city, city, 150, 24)

        if not jobs and city != "all":
            await bot.send_message(c.from_user.id, t(lang, "loading_city"))
            ok = await trigger_scraper_for_city(city)
            if ok:
                jobs = await wait_for_city_jobs(city)

        await send_jobs_to_user(c.from_user.id, jobs, user_filter=uf, limit=8, is_initial=True)
    except Exception as e:
        logger.warning(f"on_umowa error: {e}")


@router.callback_query(F.data == "renew_search")
async def on_renew_search(c: CallbackQuery):
    try:
        tid = c.from_user.id
        lang = await asyncio.to_thread(get_user_lang, tid)
        ok = await asyncio.to_thread(db_renew_search_filter, tid)
        if ok:
            await c.message.edit_text(t(lang, "search_renewed"), parse_mode="HTML")
        else:
            await c.answer("Error. Try again.", show_alert=True)
        await c.answer()
    except Exception as e:
        logger.warning(f"on_renew_search error: {e}")


# ==================== SCHEDULER (НЕБЛОКИРУЮЩИЙ) ====================

async def scheduled_check():
    started = datetime.now(timezone.utc)

    if is_night_time():
        logger.info("🌙 Night time — skipping scheduled check.")
        return

    logger.info("⏰ Check started")

    try:
        filters = await asyncio.to_thread(db_get_active_filters)
        logger.info(f"👥 Active filters: {len(filters)}")

        if not filters:
            logger.info("No active filters found.")
            return

        now = datetime.now(timezone.utc)
        active_filters = []

        for f in filters:
            tid = f["telegram_id"]
            last_renewal_str = f.get("last_renewal")

            if last_renewal_str:
                try:
                    last_renewal = datetime.fromisoformat(
                        last_renewal_str.replace("Z", "+00:00")
                    )
                    if (now - last_renewal).total_seconds() > 259200:
                        await asyncio.to_thread(db_pause_search_filter, tid)
                        lang = await asyncio.to_thread(get_user_lang, tid)
                        try:
                            await bot.send_message(
                                tid,
                                t(lang, "search_paused"),
                                parse_mode="HTML",
                                reply_markup=kb_renew_search(lang),
                            )
                            logger.info(
                                f"⏸ Paused user {tid} due to 3-day inactivity."
                            )
                        except TelegramForbiddenError:
                            await asyncio.to_thread(db_set_user_active, tid, False)
                        except Exception as e:
                            logger.warning(f"pause notification error for {tid}: {e}")
                        continue
                except Exception as e:
                    logger.error(f"Error parsing last_renewal for {tid}: {e}")

            active_filters.append(f)

        if not active_filters:
            logger.info("No filters left after renewal check.")
            return

        cities = list({f.get("city", "all") for f in active_filters})
        logger.info(f"🏙 Loading jobs for {len(cities)} cities: {cities}")

        db_semaphore = asyncio.Semaphore(3)

        async def fetch_jobs_safe(city):
            async with db_semaphore:
                return await asyncio.to_thread(db_get_jobs_for_city, city, 100, 24)

        city_results = await asyncio.gather(
            *(fetch_jobs_safe(city) for city in cities),
            return_exceptions=True,
        )

        city_jobs = {}
        for city, result in zip(cities, city_results):
            if isinstance(result, Exception):
                logger.error(f"❌ Failed loading jobs for {city}: {result}")
                city_jobs[city] = []
            else:
                city_jobs[city] = result or []
                logger.info(f"📦 {city}: {len(city_jobs[city])} jobs")

        semaphore = asyncio.Semaphore(5)

        async def process_user(f):
            tid = f["telegram_id"]
            city = f.get("city", "all")
            jobs = city_jobs.get(city, [])
            uf = {
                "umowa": f.get("umowa", "any"),
                "etat_full": f.get("etat_full", True),
                "etat_part": f.get("etat_part", False),
            }

            if not jobs:
                logger.info(f"👤 {tid}: no fresh jobs for {city}")
                return 0

            user_renewal_time = parse_iso_datetime(f.get("last_renewal"))
            
            fresh_jobs = []
            for j in jobs:
                job_created_time = parse_iso_datetime(j.get("created_at"))
                if job_created_time > user_renewal_time:
                    fresh_jobs.append(j)

            if not fresh_jobs:
                logger.info(f"👤 {tid}: no NEW jobs since subscription start.")
                return 0

            async with semaphore:
                try:
                    return await send_jobs_to_user(
                        tid, fresh_jobs, user_filter=uf, limit=15
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(f"❌ User {tid} processing error: {e}")
                    return 0

        results = await asyncio.gather(
            *(process_user(f) for f in active_filters),
            return_exceptions=True,
        )

        total_sent = 0
        for result in results:
            if isinstance(result, int):
                total_sent += result
            elif isinstance(result, Exception):
                logger.error(f"User task failed: {result}")

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            f"✅ Done: users={len(active_filters)}, cities={len(cities)}, "
            f"sent={total_sent}, duration={elapsed:.1f}s"
        )

    except asyncio.CancelledError:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.warning(
            f"🛑 Scheduled check cancelled after {elapsed:.1f}s "
            "(service is shutting down/restarting)"
        )
        raise
    except Exception as e:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.exception(
            f"❌ scheduled_check crashed after {elapsed:.1f}s: {e}"
        )


# ==================== MAIN ====================

async def main():
    logger.info("🚀 Bot starting...")
    await start_web_server()

    try:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=5))
        logger.info("⚙️ Thread pool executor limited to 5 workers for Render stability.")
    except Exception as e:
        logger.warning(f"Failed to set custom thread pool executor: {e}")

    s = AsyncIOScheduler(timezone="UTC")
    
    # 1. Задача регулярной проверки новых вакансий в БД (Каждые 15 минут)
    s.add_job(
        scheduled_check,
        "interval",
        minutes=15,
        id="check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    
    # 2. VIP-задача автоматического запуска парсера на GitHub (Каждые 25 минут секунда в секунду)
    s.add_job(
        auto_trigger_github_scraper,
        "interval",
        minutes=25,
        id="github_scraper",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )
    
    # 3. Автоматическое обновление описания бота (Каждый час)
    s.add_job(
        update_bot_description,
        "interval",
        hours=1,
        id="update_description",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
    )
    
    s.start()

    logger.info("⏰ Scheduler started: first check in 10s, VIP scraper trigger in 20s, then regular intervals")

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("🛑 Shutting down scheduler...")
        try:
            s.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"Scheduler shutdown error: {e}")
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
