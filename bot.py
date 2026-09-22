import os
import asyncio
import logging
import re
import time
import hashlib
import html
import json
import hmac
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_client, Client
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    WebAppInfo, BufferedInputFile, LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
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

DONATE_ACCOUNT = "84 9511 0000 0052 9681 3000 0010"

# ==================== VIP ====================
VIP_PRICE_STARS = 100          # цена в Telegram Stars
VIP_PRICE_PLN = 8              # ориентировочная цена в злотых (для отображения рядом со звёздами)
VIP_DURATION_DAYS = 60         # срок VIP за одну покупку
VIP_PAYLOAD = "vip_60d"        # идентификатор товара в инвойсе

# Источники, которые получают только VIP-пользователи (у бесплатных: OLX, Praca.pl, RocketJobs)
VIP_ONLY_SOURCES = {"Lento", "Infopraca"}

# Слать ли VIP-источники в каналы-сателлиты. False = каналы получают только бесплатные 3 сайта.
CHANNELS_ALLOW_VIP_SOURCES = False

BLOCKED_KEYWORDS = [
    "uber", "bolt", "glovo", "uber eats", "bolt food",
    "wolt", "dostawca jedzenia", "kurier rowerowy",
    "kierowca uber", "kierowca bolt", "kierowca glovo",
]

# Подписи кнопок нижнего меню локализованы через TEXTS (ключи btn_reset/btn_stop/btn_help/btn_restart/btn_cv)

# Шаблоны резюме, доступные только VIP-пользователям (совпадает с data-tpl в index.html)
VIP_ONLY_CV_TEMPLATES = {"t4", "t5", "t6"}

# Временный лимит-трекер резюме (в памяти: user_id -> список timestamp генераций за последние 24 часа)
CV_LIMIT_TRACKER = {}


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
    "radom": "radom",
}

# ==================== КАРТА КАНАЛОВ ДЛЯ АВТОПОСТИНГА ====================
CHANNELS_MAPPING = {
    "Lublin": {"id": -1004402210524, "limit": 5},                          # @Praca_Lublin
    "Białystok": {"id": -1004359303051, "limit": 5},                       # @Praca_Belostok
    "Radom": {"id": -1003797919409, "limit": 5},                           # @Praca_Radom
    "Częstochowa": {"id": -1004372087006, "limit": 5},                     # @Praca_Czestochowa
    "Rzeszów": {"id": -1003849575739, "limit": 5},                         # @Praca_rzeszow_ua
    "Gdynia": {"id": -1004432735605, "limit": 5},                          # @Praca_w_Gdynie
    "Poznań": {"id": -1001716517416, "limit": 3, "thread_id": 81854},    # Ветка познань
    "Bydgoszcz": {"id": -1001759834702, "limit": 5, "thread_id": 1427}    # Ветка 1445 в Быдгощ @ua_bydgoszcz
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
            "с OLX, Praca.pl и RocketJobs.\n\n"
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
            "🔍 Ищу свежие вакансии на OLX, Praca.pl и Rocket Jobs..."
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
            "с OLX, Praca.pl и RocketJobs и присылает их тебе.\n\n"
            "<b>Управление:</b>\n"
            "<b>🔄 Сбросить фильтры</b> — настроить фильтры заново\n"
            "<b>⏹ Остановить</b> — остановить рассылку\n"
            "<b>ℹ️ Помощь/VIP</b> — эта справка\n"
            "<b>/vip</b> — ⭐ VIP-версия: 5 сайтов вместо 3, поиск без остановок и стильные шаблоны резюме\n"
            "<b>#⃣ Создать резюме</b> — конструктор резюме с моментальным получением PDF в чат (лимит: 3 резюме в день)\n\n"
            "По вопросам и сотрудничеству: @Hriaker1"
        ),
        "already_stopped": "ℹ️ Ты не подписан на вакансии. Нажми кнопку ниже чтобы начать.",
        "btn_all": "🇵🇱 Вся Польша",
        "btn_custom": "✏️ Свой город",
        "btn_done": "✅ Готово",
        "after_initial": (
            "👆 Это были последние актуальные вакансии за сегодня.\n\n"
            "🔄 Теперь бот будет присылать только новые вакансии "
            "по мере их появления на OLX, Praca.pl и RocketJobs."
        ),
        "search_paused": (
            "⏸ <b>Поиск временно приостановлен</b>\n\n"
            "Ты пользуешься поиском уже 3 дня. Чтобы бот продолжил присылать "
            "тебе свежие вакансии бесплатно, подтверди, что ты всё ещё ищешь работу! 👇"
        ),
        "btn_continue": "🔄 Продолжить поиск",
        "search_renewed": "🟢 Отлично! Поиск успешно возобновлен еще на 3 дня. Свежие вакансии уже в пути! 🚀",
        "vip_promo": (
            "⭐ <b>Хочешь больше вакансий?</b>\n\n"
            "С <b>VIP</b> бот ищет на <b>5 сайтах вместо 3</b> — добавляются "
            "<b>Lento.pl</b> и <b>Infopraca.pl</b>, а поиск работает "
            "<b>без остановок каждые 3 дня</b>. А ещё — <b>стильные VIP-шаблоны резюме</b>.\n\n"
            "💰 <b>{price} ⭐ (≈{price_pln} zł) = {days} дней VIP</b>\n\n"
            "👇 Подробнее — кнопка ниже или команда /vip"
        ),
        "vip_info": (
            "⭐ <b>VIP-версия</b>\n\n"
            "<b>Что даёт VIP:</b>\n"
            "• <b>+2 сайта в поиске</b> — всего 5 вместо 3:\n"
            "   🔹 <b>Lento.pl</b> — большой польский сайт локальных объявлений (что-то вроде OLX). "
            "В разделе «Praca» часто попадаются вакансии от небольших работодателей.\n"
            "   🔹 <b>Infopraca.pl</b> — польский портал вакансий: предложения от работодателей "
            "и агентств по всей Польше.\n"
            "• <b>Поиск без остановок</b> — в бесплатном плане поиск нужно подтверждать каждые 3 дня, "
            "в VIP этого нет.\n"
            "• <b>Стильные шаблоны резюме</b> — в конструкторе резюме (#⃣) открываются эксклюзивные VIP-шаблоны с более современным дизайном.\n\n"
            "<b>Сравнение:</b>\n"
            "🆓 Бесплатно: OLX, Praca.pl, RocketJobs + подтверждение каждые 3 дня + базовые шаблоны резюме\n"
            "⭐ VIP: OLX, Praca.pl, RocketJobs + Lento.pl + Infopraca.pl, без остановок + стильные шаблоны резюме\n\n"
            "💰 <b>Цена: {price} ⭐ (≈{price_pln} zł) Telegram Stars за {days} дней VIP.</b>\n"
            "Разовый платёж, автопродления нет."
        ),
        "vip_active_line": "✅ <b>VIP активен до {until}.</b>\nЕсли продлишь сейчас, новые {days} дней добавятся к текущему сроку.\n\n",
        "btn_buy_vip": "⭐ Купить VIP — {price} ⭐ (≈{price_pln} zł) / {days} дней",
        "btn_extend_vip": "⭐ Продлить на {days} дней — {price} ⭐ (≈{price_pln} zł)",
        "btn_vip_details": "ℹ️ Подробнее про VIP",
        "vip_invoice_title": "VIP на {days} дней",
        "vip_invoice_desc": "5 сайтов вместо 3 (+ Lento.pl и Infopraca.pl) и поиск без остановок каждые 3 дня + стильные шаблоны резюме. Срок действия — {days} дней.",
        "vip_invoice_label": "VIP {days} дней",
        "vip_thanks": (
            "🎉 <b>Оплата прошла — VIP активирован!</b>\n\n"
            "VIP действует до <b>{until}</b>.\n"
            "Теперь бот ищет на 5 сайтах (добавились Lento.pl и Infopraca.pl) "
            "и не останавливается каждые 3 дня. Также тебе открылись стильные VIP-шаблоны резюме. Спасибо за поддержку! ⭐"
        ),
        "vip_pay_error": "⚠️ Оплата прошла, но при активации VIP возникла ошибка. Напиши @Hriaker1 — всё быстро исправим.",
        "paysupport": "💬 По вопросам оплаты пиши: @Hriaker1\nУкажи свой Telegram ID и время платежа.",
        "btn_cv": "#⃣ Создать резюме",
        "btn_reset": "🔄 Сбросить фильтры",
        "btn_stop": "⏹ Остановить",
        "btn_help": "ℹ️ Помощь/VIP",
        "btn_restart": "🚀 Запустить заново",
    },
    "pl": {
        "welcome": (
            "👋 Cześć! Pomogę znaleźć pracę w Polsce.\n\n"
            "Będę wysyłać nowe oferty na bieżąco z OLX, Praca.pl i RocketJobs.\n\n"
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
            "🔍 Szukam ofert na OLX, Praca.pl i RocketJobs..."
        ),
        "loading_city": "🔍 Szukam nowych ofert dla tego miasta...\nPoczekaj 30–60 секунд.",
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
            "Agreguje oferty pracy z OLX, Praca.pl i RocketJobs.\n\n"
            "<b>🔄 Ustaw od nowa</b> — ustaw filtry od nowa\n"
            "<b>⏹ Zatrzymaj</b> — zatrzymaj wysyłkę\n"
            "<b>ℹ️ Pomoc/VIP</b> — ta pomoc\n"
            "<b>/vip</b> — ⭐ wersja VIP: 5 serwisów zamiast 3, wyszukiwanie bez przerw i stylowe szablony CV\n"
            "<b>#⃣ Stwórz CV</b> — kreator CV z bezpośrednim przesłaniem PDF (limit: 3 na dobę)\n\n"
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
        "vip_promo": (
            "⭐ <b>Chcesz więcej ofert?</b>\n\n"
            "Z <b>VIP</b> bot szuka na <b>5 serwisach zamiast 3</b> — dochodzą "
            "<b>Lento.pl</b> i <b>Infopraca.pl</b>, a wyszukiwanie działa "
            "<b>bez przerw co 3 dni</b>. Do tego — <b>stylowe szablony CV VIP</b>.\n\n"
            "💰 <b>{price} ⭐ (≈{price_pln} zł) = {days} dni VIP</b>\n\n"
            "👇 Szczegóły — przycisk poniżej lub komenda /vip"
        ),
        "vip_info": (
            "⭐ <b>Wersja VIP</b>\n\n"
            "<b>Co daje VIP:</b>\n"
            "• <b>+2 serwisy w wyszukiwaniu</b> — razem 5 zamiast 3:\n"
            "   🔹 <b>Lento.pl</b> — duży polski serwis ogłoszeń lokalnych (coś jak OLX). "
            "W dziale „Praca” często trafiają się oferty od mniejszych pracodawców.\n"
            "   🔹 <b>Infopraca.pl</b> — polski portal z ofertami pracy od pracodawców "
            "i agencji z całej Polski.\n"
            "• <b>Wyszukiwanie bez przerw</b> — w wersji darmowej trzeba co 3 dni potwierdzać "
            "wyszukiwanie, w VIP nie.\n"
            "• <b>Stylowe szablony CV</b> — w kreatorze CV (#⃣) odblokowują się ekskluzywne szablony VIP o nowocześniejszym designie.\n\n"
            "<b>Porównanie:</b>\n"
            "🆓 Darmowa: OLX, Praca.pl, RocketJobs + potwierdzenie co 3 dni + podstawowe szablony CV\n"
            "⭐ VIP: OLX, Praca.pl, RocketJobs + Lento.pl + Infopraca.pl, bez przerw + stylowe szablony CV\n\n"
            "💰 <b>Cena: {price} ⭐ (≈{price_pln} zł) Telegram Stars za {days} dni VIP.</b>\n"
            "Płatność jednorazowa, bez automatycznego odnawiania."
        ),
        "vip_active_line": "✅ <b>VIP aktywny do {until}.</b>\nJeśli przedłużysz teraz, kolejne {days} dni zostanie dodane do obecnego terminu.\n\n",
        "btn_buy_vip": "⭐ Kup VIP — {price} ⭐ (≈{price_pln} zł) / {days} dni",
        "btn_extend_vip": "⭐ Przedłuż o {days} dni — {price} ⭐ (≈{price_pln} zł)",
        "btn_vip_details": "ℹ️ Więcej o VIP",
        "vip_invoice_title": "VIP na {days} dni",
        "vip_invoice_desc": "5 serwisów zamiast 3 (+ Lento.pl i Infopraca.pl) i wyszukiwanie bez przerw co 3 dni + stylowe szablony CV. Okres ważności — {days} dni.",
        "vip_invoice_label": "VIP {days} dni",
        "vip_thanks": (
            "🎉 <b>Płatność przyjęta — VIP aktywowany!</b>\n\n"
            "VIP działa do <b>{until}</b>.\n"
            "Bot szuka teraz na 5 serwisach (doszły Lento.pl i Infopraca.pl) "
            "i nie zatrzymuje się co 3 dni. Odblokowały się też stylowe szablony CV VIP. Dziękuję za wsparcie! ⭐"
        ),
        "vip_pay_error": "⚠️ Płatność przeszła, ale wystąpił błąd przy aktywacji VIP. Napisz do @Hriaker1 — szybko to naprawimy.",
        "paysupport": "💬 W sprawie płatności pisz: @Hriaker1\nPodaj swoje Telegram ID i czas płatności.",
        "btn_cv": "#⃣ Stwórz CV",
        "btn_reset": "🔄 Ustaw od nowa",
        "btn_stop": "⏹ Zatrzymaj",
        "btn_help": "ℹ️ Pomoc/VIP",
        "btn_restart": "🚀 Uruchom ponownie",
    },
    "ua": {
        "welcome": (
            "👋 Привіт! Допоможу знайти роботу в Польщі.\n\n"
            "Бот надсилатиме нові вакансії з OLX, Praca.pl та RocketJobs.\n\n"
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
            "🔍 Шукаю вакансії на OLX, Praca.pl та RocketJobs..."
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
            "Агрегує публічні вакансії з OLX, Praca.pl та RocketJobs.\n\n"
            "<b>🔄 Скинути фільтри</b> — налаштувати фільтри заново\n"
            "<b>⏹ Зупинити</b> — зупинити розсилку\n"
            "<b>ℹ️ Допомога/VIP</b> — ця довідка\n"
            "<b>/vip</b> — ⭐ VIP-версія: 5 сайтів замість 3, пошук без зупинок і стильні шаблони резюме\n"
            "<b>#⃣ Створити резюме</b> — конструктор резюме з миттєвим отриманням PDF в чаті (ліміт: 3 на день)\n\n"
            "Питання та співпраця: @Hriaker1"
        ),
        "already_stopped": "ℹ️ Ти не підписаний. Натисни кнопку нижче.",
        "btn_all": "🇵🇱 Вся Польша",
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
        "vip_promo": (
            "⭐ <b>Хочеш більше вакансій?</b>\n\n"
            "З <b>VIP</b> бот шукає на <b>5 сайтах замість 3</b> — додаються "
            "<b>Lento.pl</b> та <b>Infopraca.pl</b>, а пошук працює "
            "<b>без зупинок кожні 3 дні</b>. А ще — <b>стильні VIP-шаблони резюме</b>.\n\n"
            "💰 <b>{price} ⭐ (≈{price_pln} zł) = {days} днів VIP</b>\n\n"
            "👇 Докладніше — кнопка нижче або команда /vip"
        ),
        "vip_info": (
            "⭐ <b>VIP-версія</b>\n\n"
            "<b>Що дає VIP:</b>\n"
            "• <b>+2 сайти в пошуку</b> — разом 5 замість 3:\n"
            "   🔹 <b>Lento.pl</b> — великий польський сайт локальних оголошень (щось на кшталт OLX). "
            "У розділі «Praca» часто трапляються вакансії від невеликих роботодавців.\n"
            "   🔹 <b>Infopraca.pl</b> — польський портал вакансій: пропозиції від роботодавців "
            "та агенцій по всій Польщі.\n"
            "• <b>Пошук без зупинок</b> — у безкоштовному плані пошук треба підтверджувати кожні 3 дні, "
            "у VIP цього немає.\n"
            "• <b>Стильні шаблони резюме</b> — у конструкторі резюме (#⃣) відкриваються ексклюзивні VIP-шаблони з сучаснішим дизайном.\n\n"
            "<b>Порівняння:</b>\n"
            "🆓 Безкоштовно: OLX, Praca.pl, RocketJobs + підтвердження кожні 3 дні + базові шаблони резюме\n"
            "⭐ VIP: OLX, Praca.pl, RocketJobs + Lento.pl + Infopraca.pl, без зупинок + стильні шаблони резюме\n\n"
            "💰 <b>Ціна: {price} ⭐ (≈{price_pln} zł) Telegram Stars за {days} днів VIP.</b>\n"
            "Разовий платіж, без автопродовження."
        ),
        "vip_active_line": "✅ <b>VIP активний до {until}.</b>\nЯкщо продовжиш зараз, нові {days} днів додадуться до поточного терміну.\n\n",
        "btn_buy_vip": "⭐ Купити VIP — {price} ⭐ (≈{price_pln} zł) / {days} днів",
        "btn_extend_vip": "⭐ Продовжити на {days} днів — {price} ⭐ (≈{price_pln} zł)",
        "btn_vip_details": "ℹ️ Докладніше про VIP",
        "vip_invoice_title": "VIP на {days} днів",
        "vip_invoice_desc": "5 сайтів замість 3 (+ Lento.pl та Infopraca.pl) і пошук без зупинок кожні 3 дні + стильні шаблони резюме. Термін дії — {days} днів.",
        "vip_invoice_label": "VIP {days} днів",
        "vip_thanks": (
            "🎉 <b>Оплата пройшла — VIP активовано!</b>\n\n"
            "VIP діє до <b>{until}</b>.\n"
            "Тепер бот шукає на 5 сайтах (додалися Lento.pl та Infopraca.pl) "
            "і не зупиняється кожні 3 дні. Також тобі відкрилися стильні VIP-шаблони резюме. Дякую за підтримку! ⭐"
        ),
        "vip_pay_error": "⚠️ Оплата пройшла, але під час активації VIP сталася помилка. Напиши @Hriaker1 — швидко все виправимо.",
        "paysupport": "💬 З питань оплати пиши: @Hriaker1\nВкажи свій Telegram ID і час платежу.",
        "btn_cv": "#⃣ Створити резюме",
        "btn_reset": "🔄 Скинути фільтри",
        "btn_stop": "⏹ Зупинити",
        "btn_help": "ℹ️ Допомога/VIP",
        "btn_restart": "🚀 Запустити знову",
    },
}


ALL_BTN_RESET = {TEXTS[l]["btn_reset"] for l in TEXTS}
ALL_BTN_STOP = {TEXTS[l]["btn_stop"] for l in TEXTS}
ALL_BTN_HELP = {TEXTS[l]["btn_help"] for l in TEXTS}
ALL_BTN_RESTART = {TEXTS[l]["btn_restart"] for l in TEXTS}
ALL_MENU_BTNS = ALL_BTN_RESET | ALL_BTN_STOP | ALL_BTN_HELP | ALL_BTN_RESTART


def t(lang, key, **kwargs):
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, "")
    return text.format(**kwargs) if kwargs else text


def vip_t(lang, key, **kwargs):
    """t() для VIP-текстов: сама подставляет цену и срок."""
    kwargs.setdefault("price", VIP_PRICE_STARS)
    kwargs.setdefault("price_pln", VIP_PRICE_PLN)
    kwargs.setdefault("days", VIP_DURATION_DAYS)
    kwargs.setdefault("until", "")
    return t(lang, key, **kwargs)


def format_vip_date(dt):
    try:
        return dt.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m.%Y")
    except Exception:
        return dt.strftime("%d.%m.%Y")


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
    if any(x in e for x in ["full", "pełny", "pelny", "pełen", "pelen", "cały etat", "caly etat"]):
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


# ==================== DATABASE (FAIL-SAFE + FULL RETRY) ====================

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


def db_get_city_audience():
    """
    Аудитория для таргетированной рассылки: {slug: {"label": str, "ids": [telegram_id, ...]}}.
    Берутся только живые пользователи (telegram_id > 0, is_active=True) с сохранённым фильтром.
    Города группируются по slug ("Wrocław" и "wroclaw" — один город), city='all' -> slug 'all'.
    При ошибке БД возвращает None.
    """
    from collections import Counter
    try:
        active = set(db_get_all_active_users())
        rows, offset, page = [], 0, 1000
        while True:
            r = supabase.table("user_filters").select("telegram_id, city").range(offset, offset + page - 1).execute()
            if not r.data:
                break
            rows.extend(r.data)
            if len(r.data) < page:
                break
            offset += page
    except Exception as e:
        logger.error(f"db_get_city_audience error: {e}")
        return None

    groups = {}
    for row in rows:
        tid = row.get("telegram_id")
        if not tid or tid < 0 or tid not in active:
            continue
        raw = (row.get("city") or "all").strip() or "all"
        slug = get_city_slug(raw)
        g = groups.setdefault(slug, {"ids": set(), "names": Counter()})
        g["ids"].add(tid)
        g["names"][raw] += 1

    return {
        slug: {"label": g["names"].most_common(1)[0][0], "ids": sorted(g["ids"])}
        for slug, g in groups.items()
    }


def db_get_sent_job_ids(tid) -> set:
    """
    Бронебойный запрос истории отправки.
    Передаёт ID напрямую в Supabase, без лишней MD5-магии.
    """
    for attempt in range(3):
        try:
            r = supabase.table("sent_jobs").select("job_id").eq("telegram_id", tid).execute()
            return {row["job_id"] for row in r.data} if r.data else set()
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.error(f"❌ db_get_sent_job_ids failed for {tid} after 3 attempts: {e}")
            return None
    return None


def db_mark_sent_batch(tid, job_ids: list) -> bool:
    """Пакетная фиксация отправки напрямую в Supabase"""
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
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.error(f"❌ db_mark_sent_batch error for {tid}: {e}")
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
        now_str = datetime.now(timezone.utc).isoformat()
        supabase.table("user_filters").update({
            "is_paused": True
        }).eq("telegram_id", tid).execute()
        return True
    except Exception as e:
        logger.error(f"db_pause_search_filter: {e}")
        return False


# ==================== VIP (DATABASE) ====================

def db_get_vip_until(tid):
    """Дата окончания VIP (datetime) или None. Отдельный запрос — чтобы не ломать db_get_user."""
    try:
        r = supabase.table("users").select("vip_until").eq("telegram_id", tid).execute()
        if r.data and r.data[0].get("vip_until"):
            return parse_iso_datetime(r.data[0]["vip_until"])
    except Exception as e:
        logger.error(f"db_get_vip_until({tid}): {e}")
    return None


def db_is_vip(tid) -> bool:
    until = db_get_vip_until(tid)
    return bool(until and until > datetime.now(timezone.utc))


def db_get_active_vip_ids():
    """Множество telegram_id с действующим VIP. При ошибке БД возвращает None (а не пустое множество!)."""
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        r = supabase.table("users").select("telegram_id").gt("vip_until", now_str).execute()
        return {row["telegram_id"] for row in r.data} if r.data else set()
    except Exception as e:
        logger.error(f"db_get_active_vip_ids: {e}")
        return None


def db_activate_vip(tid, days):
    """Выдаёт или продлевает VIP. Если VIP ещё действует — новый срок прибавляется к текущему.
    Возвращает новую дату окончания или None, если записать не удалось."""
    for attempt in range(3):
        try:
            r = supabase.table("users").select("vip_until").eq("telegram_id", tid).execute()
            current = None
            if r.data and r.data[0].get("vip_until"):
                current = parse_iso_datetime(r.data[0]["vip_until"])
            now = datetime.now(timezone.utc)
            base = current if (current and current > now) else now
            new_until = base + timedelta(days=days)
            supabase.table("users").upsert(
                {"telegram_id": tid, "vip_until": new_until.isoformat()},
                on_conflict="telegram_id"
            ).execute()
            return new_until
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            logger.error(f"❌ db_activate_vip({tid}) failed: {e}")
    return None


def db_log_vip_payment(tid, charge_id, stars, vip_until):
    """Журнал платежей (нужен charge_id для возвратов). Не критично: если таблицы нет — просто лог."""
    try:
        supabase.table("vip_payments").insert({
            "telegram_id": tid,
            "telegram_charge_id": charge_id,
            "stars": stars,
            "vip_until": vip_until.isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"db_log_vip_payment({tid}, {charge_id}): {e}")


def db_get_jobs_for_city(city, limit=150, hours=24):
    """С автоповтором на любые сетевые ошибки"""
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
    """Возвращает реальную статистику зарегистрированных и активных людей"""
    for attempt in range(3):
        try:
            total_res = supabase.table("users").select("telegram_id", count="exact").limit(1).execute()
            active_res = supabase.table("users").select("telegram_id", count="exact").eq("is_active", True).limit(1).execute()
            
            total = total_res.count if total_res.count is not None else 0
            active = active_res.count if active_res.count is not None else 0
            
            return {"total": total, "active": active}
        except Exception as e:
            if attempt < 2:
                time.sleep(0.5)
                continue
            logger.error(f"db_get_bot_stats error: {e}")
            return {"total": 0, "active": 0}
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


# ==================== SIGNATURE & ANTI-SPAM PROTECTION ====================

def verify_telegram_webapp_data(init_data: str, token: str) -> dict:
    """
    Валидирует переданные initData от Telegram с помощью HMAC-SHA256,
    гарантируя защиту от накрутки и взлома user_id.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in parsed:
            return {}
        
        data_hash = parsed.pop("hash")
        sorted_keys = sorted(parsed.keys())
        data_check_string = "\n".join([f"{k}={parsed[k]}" for k in sorted_keys])
        
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == data_hash:
            return json.loads(parsed.get("user", "{}"))
    except Exception as e:
        logger.error(f"Error validating telegram initData: {e}")
    return {}


def is_upload_allowed(user_id: int) -> bool:
    """
    Проверяет лимит создания резюме (максимум 3 резюме в сутки).
    """
    now = datetime.now(timezone.utc)
    if user_id not in CV_LIMIT_TRACKER:
        CV_LIMIT_TRACKER[user_id] = []
    
    # Очищаем старые временные метки (старше 24 часов)
    CV_LIMIT_TRACKER[user_id] = [t for t in CV_LIMIT_TRACKER[user_id] if now - t < timedelta(days=1)]
    
    if len(CV_LIMIT_TRACKER[user_id]) >= 3:
        return False
        
    CV_LIMIT_TRACKER[user_id].append(now)
    return True


# ==================== WEB SERVER (УМНАЯ СИНХРОНИЗАЦИЯ С СОВМЕСТИМОСТЬЮ CORS) ====================

async def health_check(request):
    return web.Response(text="OK", status=200)


async def vip_status_handler(request: web.Request):
    """
    Отдаёт веб-приложению (конструктору резюме) статус VIP по telegram_id,
    чтобы фронтенд мог показать/скрыть доступ к VIP-шаблонам.
    """
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Requested-With",
    }

    if request.method == "OPTIONS":
        return web.Response(status=200, headers=headers)

    try:
        uid_param = request.query.get("uid") or request.query.get("user_id")
        if not uid_param:
            return web.json_response({"error": "missing_uid"}, status=400, headers=headers)

        try:
            user_id = int(str(uid_param).strip())
        except ValueError:
            return web.json_response({"error": "invalid_uid"}, status=400, headers=headers)

        is_vip = await asyncio.to_thread(db_is_vip, user_id)
        return web.json_response({"is_vip": is_vip}, headers=headers)
    except Exception as e:
        logger.error(f"Error in vip_status_handler: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=headers)


async def upload_cv_handler(request: web.Request):
    """
    Принимает Blob-файл напрямую с Netlify без левых файлообменников,
    проверяет подпись Telegram, лимиты, и отправляет резюме напрямую в чат.
    """
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Requested-With",
    }
    
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=headers)
        
    try:
        reader = await request.post()
        init_data = reader.get("init_data")
        user_id_param = reader.get("user_id") or reader.get("uid")
        file_field = reader.get("file")
        filename = reader.get("filename", "CV_Resume.pdf")
        tpl = str(reader.get("tpl") or "").strip()
        
        if not file_field:
            return web.json_response({"error": "missing_parameters"}, status=400, headers=headers)
            
        user_id = None

        # 1. Сначала пробуем валидацию крипто-подписи Telegram (если init_data передан)
        if init_data:
            user_data = verify_telegram_webapp_data(init_data, BOT_TOKEN)
            if user_data and "id" in user_data:
                user_id = user_data["id"]

        # 2. Если init_data пустой, берем прямой user_id из query-параметра
        if not user_id and user_id_param:
            try:
                user_id = int(str(user_id_param).strip())
            except ValueError:
                pass

        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401, headers=headers)

        # Серверная защита VIP-шаблонов: даже если фронтенд обойти, здесь всё равно проверим права
        if tpl in VIP_ONLY_CV_TEMPLATES:
            is_vip = await asyncio.to_thread(db_is_vip, user_id)
            if not is_vip:
                return web.json_response({"error": "vip_required"}, status=403, headers=headers)

        # Защита от лимитов (макс 3 резюме в день)
        if not is_upload_allowed(user_id):
            return web.json_response({"error": "limit_exceeded"}, status=429, headers=headers)
            
        # Бронебойное чтение байтов
        file_bytes = b""
        if isinstance(file_field, web.FileField):
            try:
                file_bytes = file_field.file.read()
            except Exception:
                pass
            if not file_bytes:
                try:
                    file_bytes = file_field.value
                except Exception:
                    pass
        elif isinstance(file_field, (bytes, bytearray)):
            file_bytes = bytes(file_field)
            
        if not file_bytes:
            return web.json_response({"error": "empty_file"}, status=400, headers=headers)

        doc = BufferedInputFile(file_bytes, filename=str(filename))
        
        lang = await asyncio.to_thread(get_user_lang, user_id)
        msg_caption = {
            "ru": "📄 <b>Ваше резюме успешно создано!</b>\nФайл прикреплен ниже 👇",
            "pl": "📄 <b>Twoje CV zostało pomyślnie utworzone!</b>\nPlik znajduje się poniżej 👇",
            "ua": "📄 <b>Ваше резюме успішно створено!</b>\nФайл прикріплено нижче 👇"
        }.get(lang, "📄 <b>Ваше резюме готово!</b>")
        
        await bot.send_document(chat_id=user_id, document=doc, caption=msg_caption, parse_mode="HTML")
        logger.info(f"✅ Резюме успешно доставлено юзеру {user_id}")
        return web.json_response({"success": True}, headers=headers)
        
    except Exception as e:
        logger.error(f"Error in upload_cv_handler: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=headers)


async def start_web_server():
    app = web.Application(client_max_size=1024**2 * 50)
    app.router.add_get("/", health_check)
    app.router.add_get("/ping", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_post("/api/upload_cv", upload_cv_handler)
    app.router.add_options("/api/upload_cv", upload_cv_handler)
    app.router.add_get("/api/vip_status", vip_status_handler)
    app.router.add_options("/api/vip_status", vip_status_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Web server started on port {port}")


# ==================== FORMAT & SEND ====================

# Текст кнопки со ссылкой на вакансию (единый для всех сообщений)
JOB_BUTTON_TEXT = "🔗 Przejdź do oferty
"


def is_valid_job_url(url) -> bool:
    """Telegram принимает в url-кнопках только http(s)-ссылки с доменом, до 2048 символов."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url or len(url) > 2048:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def get_job_keyboard(job_url, button_text: str = JOB_BUTTON_TEXT):
    """
    Собирает inline-клавиатуру с одной кнопкой-ссылкой.
    Возвращает None, если ссылка пустая/битая — тогда сообщение уходит просто без кнопки.
    Универсальный билдер: можно использовать и для одиночных вакансий, и для дайджестов.
    """
    if not is_valid_job_url(job_url):
        return None
    safe_url = job_url.strip().replace(" ", "%20")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, url=safe_url)
    ]])


def format_job(job):
    """
    Карточка вакансии: заголовок сверху, все подробности внутри цитаты blockquote.
    Ссылки на вакансию в тексте нет — она вынесена в inline-кнопку (см. get_job_keyboard).
    Спецсимволы HTML экранируются через html.escape.
    """
    def clean(text):
        if not text:
            return ""
        cleaned = strip_html(str(text))
        return html.escape(cleaned)

    title = clean(job.get('title')) or "Без названия"
    ut = UMOWY_DISPLAY.get(job.get("umowa")) or job.get("umowa")
    et = ETAT_DISPLAY.get(job.get("etat")) or job.get("etat")

    details = []
    if ut:
        details.append(f"📄 {clean(ut)}")
    if et:
        details.append(f"⏰ {clean(et)}")
    if job.get("city"):
        details.append(f"📍 {clean(job['city'])}")
    if job.get("salary"):
        details.append(f"💰 {clean(job['salary'])}")

    details.append(f"📌 {clean(job.get('source')) or '—'}")

    quote_content = "\n".join(details)

    message = (
        f"💼 <b>{title}</b>\n\n"
        f"<blockquote>{quote_content}</blockquote>\n\n"
        f"<a href='https://t.me/szukam_pracy_bot'>@szukam_pracy_bot</a> — Świeże oferty pracy w Polsce 🇵🇱"
    )

    return message.strip()


async def send_job_card(chat_id, job, **kwargs):
    """
    Отправляет карточку вакансии с кнопкой-ссылкой.
    Если ссылка битая/отсутствует — шлёт без кнопки.
    Если Telegram всё же отклонил кнопку (BUTTON_URL_INVALID) — повторяет отправку без неё.
    Остальные ошибки (RetryAfter, Forbidden и т.д.) пробрасываются наверх как раньше.
    """
    url = job.get("url")
    kb = get_job_keyboard(url)
    if kb is None:
        logger.warning(f"Job {job.get('id')}: invalid or missing url ({url!r}), sending without button")

    try:
        return await bot.send_message(
            chat_id,
            format_job(job),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
            **kwargs,
        )
    except TelegramBadRequest as e:
        if kb is not None and "BUTTON_URL_INVALID" in str(e).upper():
            logger.warning(f"Job {job.get('id')}: Telegram rejected button url {url!r}, resending without button")
            return await bot.send_message(
                chat_id,
                format_job(job),
                parse_mode="HTML",
                disable_web_page_preview=True,
                **kwargs,
            )
        raise


async def send_promo(chat_id, lang="ru"):
    """Рекламное сообщение с VIP (закрепляется в чате). Тем, у кого VIP уже есть, не показываем."""
    try:
        if await asyncio.to_thread(db_is_vip, chat_id):
            return
        sent_msg = await bot.send_message(
            chat_id, vip_t(lang, "vip_promo"),
            reply_markup=kb_vip_promo(lang), parse_mode="HTML"
        )
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"promo error: {e}")


async def send_jobs_to_user(tid, jobs, user_filter=None, limit=15, is_initial=False, is_vip=False):
    async with get_user_lock(tid):
        sent, sf, ss, blocked, vip_only = 0, 0, 0, 0, 0
        
        # Получаем историю отправки с предохранителем
        already_sent_ids = await asyncio.to_thread(db_get_sent_job_ids, tid)
        
        # ЕСЛИ БЫЛ СБОЙ СЕТИ И БАЗА НЕ ОТВЕТИЛА — ПРОПУСКАЕМ ЮЗЕРА (НОЛЬ СПАМА ДУБЛЯМИ!)
        if already_sent_ids is None:
            logger.warning(f"⚠️ Skipping user {tid} in this cycle due to DB history fetch error.")
            return 0

        sent_job_ids_batch = []

        for job in jobs:
            if sent >= limit:
                break

            job_id = job.get("id")
            if job_id is None:
                continue

            # Lento / Infopraca — только для VIP (не помечаем как отправленные: после покупки VIP они дойдут)
            if not is_vip and job.get("source") in VIP_ONLY_SOURCES:
                vip_only += 1
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
                await send_job_card(tid, job)
                sent_job_ids_batch.append(job_id)
                already_sent_ids.add(job_id)
                sent += 1

                await asyncio.sleep(0.05)

            except TelegramRetryAfter as e:
                retry_after = max(1, int(getattr(e, "retry_after", 1)))
                logger.warning(f"Telegram rate limit for {tid}; sleeping {retry_after}s")
                await asyncio.sleep(retry_after)
                try:
                    await send_job_card(tid, job)
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
            f"User {tid}: Sent={sent} filtered={sf} already={ss} blocked={blocked} vip_only_skipped={vip_only} vip={is_vip}"
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


# ==================== AUTO-POSTING TO CHANNELS ====================

async def post_jobs_to_channels():
    """
    Фоновая задача автопостинга свежих вакансий в Telegram-каналы сателлиты.
    """
    logger.info("📢 Starting channel auto-posting process...")
    for city, config in CHANNELS_MAPPING.items():
        try:
            # Извлекаем параметры конфигурации каждого канала
            channel_id = config["id"]
            limit = config.get("limit", 5)
            thread_id = config.get("thread_id", None)

            # Сначала ОБЯЗАТЕЛЬНО регистрируем канал в таблице users,
            # чтобы удовлетворить ограничение внешнего ключа (Foreign Key) в sent_jobs.
            await asyncio.to_thread(db_upsert_user, channel_id, f"Channel_{city}")

            # Забираем вакансии за последние 2 часа (СВЕЖАК!), убирая отправку старья
            jobs = await asyncio.to_thread(db_get_jobs_for_city, city, limit=50, hours=2)
            if not jobs:
                continue

            already_sent_ids = await asyncio.to_thread(db_get_sent_job_ids, channel_id)
            if already_sent_ids is None:
                continue

            sent_count = 0
            sent_job_ids_batch = []
            skipped_job_ids_batch = []

            # 1. Сначала отфильтровываем дубликаты
            new_jobs = []
            for job in reversed(jobs):
                job_id = job.get("id")
                if job_id is None or job_id in already_sent_ids:
                    continue
                if is_invalid_olx_url(job.get("url")) or is_delivery_job(job):
                    continue
                if not CHANNELS_ALLOW_VIP_SOURCES and job.get("source") in VIP_ONLY_SOURCES:
                    continue
                new_jobs.append(job)

            # 2. Идем по списку уникальных свежих вакансий
            for job in new_jobs:
                job_id = job["id"]
                if sent_count < limit:
                    # Отправляем только до достижения лимита
                    try:
                        await send_job_card(channel_id, job, message_thread_id=thread_id)
                        sent_job_ids_batch.append(job_id)
                        already_sent_ids.add(job_id)
                        sent_count += 1
                        await asyncio.sleep(3.0)
                    except Exception as post_error:
                        logger.error(f"Failed to send post to channel {channel_id}: {post_error}")
                        break
                else:
                    # Остальные помечаем как "отправленные" (записываем в базу, чтобы не всплывали в следующем цикле)
                    skipped_job_ids_batch.append(job_id)

            # 3. Фиксируем и отправленные, и пропущенные вакансии в Supabase
            all_to_mark = sent_job_ids_batch + skipped_job_ids_batch
            if all_to_mark:
                await asyncio.to_thread(db_mark_sent_batch, channel_id, all_to_mark)
                logger.info(f"📢 Posted {sent_count} jobs and skipped {len(skipped_job_ids_batch)} jobs into channel/topic: {channel_id}")

        except Exception as city_error:
            logger.error(f"Error in channel posting for city {city}: {city_error}")


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


def kb_active_menu(lang="ru", tid=None):
    web_url = f"https://myworkcvapp.netlify.app/index.html?uid={tid}" if tid else "https://myworkcvapp.netlify.app/index.html"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_reset")), KeyboardButton(text=t(lang, "btn_stop"))],
            [KeyboardButton(text=t(lang, "btn_help")), KeyboardButton(text=t(lang, "btn_cv"), web_app=WebAppInfo(url=web_url))],
        ],
        resize_keyboard=True
    )


def kb_stopped_menu(lang="ru"):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t(lang, "btn_restart"))]], resize_keyboard=True)


def kb_renew_search(lang):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(lang, "btn_continue"), callback_data="renew_search")]])


def kb_vip_promo(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=vip_t(lang, "btn_buy_vip"), callback_data="buy_vip")],
        [InlineKeyboardButton(text=vip_t(lang, "btn_vip_details"), callback_data="vip_info")],
    ])


def kb_vip_buy(lang, is_vip=False):
    key = "btn_extend_vip" if is_vip else "btn_buy_vip"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=vip_t(lang, key), callback_data="buy_vip")],
    ])


def kb_admin_confirm():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Всем (все города + все каналы)", callback_data="admin_send")],
        [InlineKeyboardButton(text="🎯 По городам", callback_data="admin_cities")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")],
    ])


def build_city_options(audience: dict) -> list:
    """Список городов для выбора: города пользователей + города каналов-сателлитов (даже если людей там 0)."""
    channel_by_slug = {get_city_slug(c): c for c in CHANNELS_MAPPING}
    opts = []
    for slug, g in audience.items():
        label = "🇵🇱 Вся Польша" if slug == "all" else (channel_by_slug.get(slug) or g["label"])
        opts.append({"slug": slug, "label": label, "ids": list(g["ids"]), "channel": slug in channel_by_slug})
    for slug, city in channel_by_slug.items():
        if slug not in audience:
            opts.append({"slug": slug, "label": city, "ids": [], "channel": True})
    opts.sort(key=lambda o: (-len(o["ids"]), o["label"].lower()))
    return opts[:90]  # лимит Telegram — 100 кнопок на клавиатуру


def admin_city_targets(opts: list, sel):
    """По выбранным индексам возвращает (список user_id без дублей, список городов-каналов, список названий)."""
    chosen = [opts[i] for i in sorted(sel) if 0 <= i < len(opts)]
    users = sorted({uid for o in chosen for uid in o["ids"]})
    channels = [o["label"] for o in chosen if o["channel"]]
    return users, channels, [o["label"] for o in chosen]


def kb_admin_cities(opts: list, sel):
    rows, row = [], []
    for i, o in enumerate(opts):
        mark = "✅" if i in sel else "☐"
        tv = " 📺" if o["channel"] else ""
        row.append(InlineKeyboardButton(
            text=f"{mark} {o['label']} ({len(o['ids'])}){tv}",
            callback_data=f"adm_ct_{i}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    users, channels, _ = admin_city_targets(opts, sel)
    rows.append([InlineKeyboardButton(
        text=f"🚀 Отправить ({len(users)} чел. + {len(channels)} 📺)",
        callback_data="admin_send_cities"
    )])
    rows.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== BROADCASTER ====================

async def run_broadcast(bot: Bot, admin_id: int, from_chat_id: int, message_id: int, users: list,
                        channels: list = None, audience_note: str = ""):
    """
    Рассылка рекламы: Сначала мгновенно публикует во всех каналах сателлитах,
    а затем плавно рассылает всем активным пользователям в ЛС с подробным отчетом админу.
    """
    sent_users, failed_users = 0, 0
    sent_channels, failed_channels = 0, 0

    # channels=None -> все каналы (как раньше); иначе только каналы указанных городов
    channel_items = [
        (c, cfg) for c, cfg in CHANNELS_MAPPING.items()
        if channels is None or c in channels
    ]
    
    logger.info("📢 Copying broadcast post to all satellite channels...")
    for city, config in channel_items:
        try:
            channel_id = config["id"]
            thread_id = config.get("thread_id", None)
            await bot.copy_message(
                chat_id=channel_id, 
                from_chat_id=from_chat_id, 
                message_id=message_id,
                message_thread_id=thread_id
            )
            sent_channels += 1
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Failed to copy broadcast to channel {city} ({channel_id}): {e}")
            failed_channels += 1

    logger.info("👥 Sending broadcast to all active PM users...")
    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent_users += 1
            await asyncio.sleep(0.05)
        except Exception:
            await asyncio.to_thread(db_set_user_active, uid, False)
            failed_users += 1
            
    try:
        await bot.send_message(
            admin_id,
            f"📢 <b>Рассылка успешно завершена!</b>\n"
            + (f"🎯 Города: {html.escape(audience_note)}\n" if audience_note else "")
            + "\n"
            f"<b>👥 Пользователи в ЛС:</b>\n"
            f"✅ Получили: {sent_users}\n"
            f"❌ Заблокировали: {failed_users}\n\n"
            f"<b>📺 Каналы-сателлиты:</b>\n"
            f"✅ Опубликовано: {sent_channels} из {len(channel_items)}\n"
            f"❌ Ошибки: {failed_channels}"
        )
    except Exception:
        pass


# ==================== VIP AUTOMATIC GITHUB TRIGGER ====================

async def auto_trigger_github_scraper():
    """
    Каждые 25 минут пинает GitHub через VIP API для мгновенного и точного парсинга по расписанию.
    """
    logger.info("🚀 Triggering scheduled instant scrape via GitHub API...")
    ok = await trigger_scraper_for_city("")
    if ok:
        logger.info("✅ GitHub Scraper successfully triggered via VIP API!")
    else:
        logger.warning("⚠️ Failed to trigger GitHub Scraper via API.")


# ==================== AUTOMATIC BOT DESCRIPTION UPDATE ====================

async def update_bot_description():
    """
    Раз в час обновляет описание бота (What can this bot do?) в Telegram,
    подставляя реальную статистику базы данных.
    """
    try:
        stats = await asyncio.to_thread(db_get_bot_stats)
        total = stats.get("total", 0)
        active = stats.get("active", 0)
        
        if total == 0:
            return
            
        description_text = (
            "Зачем пахать над поиском работы, чилль на диване "
            "пока твой цифровой раб пылесосит вакансии 24/7\n"
            f"👥 Всего: {total} / 🟢 Активных: {active}"
        )
        
        await bot.set_my_description(description=description_text)
        await bot.set_my_description(description=description_text, language_code="ru")
        await bot.set_my_description(description=description_text, language_code="pl")
        await bot.set_my_description(description=description_text, language_code="uk")
        
        logger.info(f"✅ Bot Description updated: {total} total / {active} active")
    except Exception as e:
        logger.error(f"Failed to update bot description: {e}")


# ==================== INITIALIZE CHANNELS ====================

def db_init_channels():
    """
    Принудительно регистрирует все каналы-сателлиты в базе данных
    как вечных и активных VIP-пользователей.
    Благодаря этому каналы никогда не уходят в заморозку,
    а парсер на Гитхабе ВСЕГДА видит и сканирует их города 24/7!
    """
    logger.info("📡 Initializing satellite channels in database...")
    for city, config in CHANNELS_MAPPING.items():
        try:
            channel_id = config["id"]
            # 1. Регистрируем канал в таблице users
            supabase.table("users").upsert(
                {"telegram_id": channel_id, "username": f"Channel_{city}", "is_active": True},
                on_conflict="telegram_id"
            ).execute()
            
            # 2. Создаем для него вечный активный фильтр (is_paused = False)
            supabase.table("user_filters").upsert(
                {
                    "telegram_id": channel_id,
                    "city": city,
                    "etat_full": True,
                    "etat_part": True,
                    "umowa": "any",
                    "is_paused": False,
                    "last_renewal": datetime.now(timezone.utc).isoformat()
                },
                on_conflict="telegram_id"
            ).execute()
        except Exception as e:
            logger.error(f"Failed to init channel {city} ({channel_id}): {e}")


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
        kb = kb_admin_confirm()
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


@router.callback_query(AdminStates.confirm_ad, F.data == "admin_cities", F.from_user.id == ADMIN_ID)
async def admin_pick_cities(c: CallbackQuery, state: FSMContext):
    try:
        audience = await asyncio.to_thread(db_get_city_audience)
        if audience is None:
            await c.answer("❌ Не удалось загрузить города из БД", show_alert=True)
            return
        opts = build_city_options(audience)
        if not opts:
            await c.answer("Нет ни одного города с пользователями", show_alert=True)
            return
        await state.update_data(city_opts=opts, city_sel=[])
        await c.message.edit_text(
            "🎯 <b>Выбери города для рассылки</b>\n\n"
            "В скобках — сколько активных людей в городе, 📺 — есть канал-сателлит.",
            parse_mode="HTML", reply_markup=kb_admin_cities(opts, set())
        )
        await c.answer()
    except Exception as e:
        logger.warning(f"admin_pick_cities error: {e}")


@router.callback_query(AdminStates.confirm_ad, F.data.startswith("adm_ct_"), F.from_user.id == ADMIN_ID)
async def admin_toggle_city(c: CallbackQuery, state: FSMContext):
    try:
        idx = int(c.data.split("_")[-1])
        data = await state.get_data()
        opts = data.get("city_opts") or []
        sel = set(data.get("city_sel") or [])
        if not (0 <= idx < len(opts)):
            await c.answer()
            return
        sel.symmetric_difference_update({idx})
        await state.update_data(city_sel=sorted(sel))
        await c.message.edit_reply_markup(reply_markup=kb_admin_cities(opts, sel))
        await c.answer()
    except Exception as e:
        logger.warning(f"admin_toggle_city error: {e}")


@router.callback_query(AdminStates.confirm_ad, F.data == "admin_back", F.from_user.id == ADMIN_ID)
async def admin_back(c: CallbackQuery):
    try:
        await c.message.edit_text("Запустить отправку?", reply_markup=kb_admin_confirm())
        await c.answer()
    except Exception as e:
        logger.warning(f"admin_back error: {e}")


@router.callback_query(AdminStates.confirm_ad, F.data == "admin_send_cities", F.from_user.id == ADMIN_ID)
async def admin_send_to_cities(c: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        opts = data.get("city_opts") or []
        sel = set(data.get("city_sel") or [])
        if not sel:
            await c.answer("Выбери хотя бы один город", show_alert=True)
            return
        users, channels, labels = admin_city_targets(opts, sel)
        if not users and not channels:
            await c.answer("В выбранных городах некому отправлять", show_alert=True)
            return

        msg_id, chat_id = data.get("ad_msg_id"), data.get("ad_chat_id")
        await state.clear()
        await c.message.edit_text(
            f"🚀 Рассылка запущена!\n\n🎯 Города: {html.escape(', '.join(labels))}\n"
            f"👥 Людей: <b>{len(users)}</b>\n📺 Каналов: <b>{len(channels)}</b>",
            parse_mode="HTML"
        )
        await c.answer()
        asyncio.create_task(run_broadcast(
            bot, ADMIN_ID, chat_id, msg_id, users,
            channels=channels, audience_note=", ".join(labels)
        ))
    except Exception as e:
        logger.warning(f"admin_send_to_cities error: {e}")
        await c.message.answer(f"❌ Ошибка: {e}")


# ==================== VIP: КОМАНДЫ И ОПЛАТА (Telegram Stars) ====================

async def grant_vip(tid, days):
    """Выдаёт/продлевает VIP и снимает паузу поиска, если она была. Возвращает дату окончания или None."""
    new_until = await asyncio.to_thread(db_activate_vip, tid, days)
    if not new_until:
        return None
    flt = await asyncio.to_thread(db_get_filter, tid)
    if flt and flt.get("is_paused"):
        await asyncio.to_thread(db_renew_search_filter, tid)
    return new_until


async def send_vip_info(tid):
    lang = await asyncio.to_thread(get_user_lang, tid)
    until = await asyncio.to_thread(db_get_vip_until, tid)
    is_vip = bool(until and until > datetime.now(timezone.utc))
    text = vip_t(lang, "vip_info")
    if is_vip:
        text = vip_t(lang, "vip_active_line", until=format_vip_date(until)) + text
    await bot.send_message(tid, text, parse_mode="HTML", reply_markup=kb_vip_buy(lang, is_vip))


@router.message(Command("vip"))
async def cmd_vip(m: Message):
    try:
        await send_vip_info(m.from_user.id)
    except Exception as e:
        logger.warning(f"cmd_vip error: {e}")


@router.callback_query(F.data == "vip_info")
async def on_vip_info(c: CallbackQuery):
    try:
        await send_vip_info(c.from_user.id)
        await c.answer()
    except Exception as e:
        logger.warning(f"on_vip_info error: {e}")


@router.callback_query(F.data == "buy_vip")
async def on_buy_vip(c: CallbackQuery):
    try:
        lang = await asyncio.to_thread(get_user_lang, c.from_user.id)
        await bot.send_invoice(
            chat_id=c.from_user.id,
            title=vip_t(lang, "vip_invoice_title"),
            description=vip_t(lang, "vip_invoice_desc"),
            payload=VIP_PAYLOAD,
            provider_token="",   # для Stars токен провайдера пустой
            currency="XTR",
            prices=[LabeledPrice(label=vip_t(lang, "vip_invoice_label"), amount=VIP_PRICE_STARS)],
        )
        await c.answer()
    except Exception as e:
        logger.warning(f"on_buy_vip error: {e}")
        await c.answer("Error. Try again later.", show_alert=True)


@router.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    try:
        if q.invoice_payload != VIP_PAYLOAD or q.currency != "XTR" or q.total_amount != VIP_PRICE_STARS:
            await q.answer(ok=False, error_message="Invalid invoice. Please request /vip again.")
            return
        await q.answer(ok=True)
    except Exception as e:
        logger.warning(f"on_pre_checkout error: {e}")


@router.message(F.successful_payment)
async def on_successful_payment(m: Message):
    sp = m.successful_payment
    tid = m.from_user.id
    if sp.invoice_payload != VIP_PAYLOAD:
        return
    lang = await asyncio.to_thread(get_user_lang, tid)

    new_until = await grant_vip(tid, VIP_DURATION_DAYS)
    if not new_until:
        # Деньги списаны, а VIP не записался — сообщаем админу charge_id для ручной выдачи/возврата
        logger.error(f"🚨 VIP not activated after payment: user={tid} charge={sp.telegram_payment_charge_id}")
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚨 Оплата прошла, но VIP не активирован!\nuser: {tid}\n"
                f"charge_id: {sp.telegram_payment_charge_id}\nstars: {sp.total_amount}"
            )
        except Exception:
            pass
        await m.answer(vip_t(lang, "vip_pay_error"))
        return

    await asyncio.to_thread(db_log_vip_payment, tid, sp.telegram_payment_charge_id, sp.total_amount, new_until)
    logger.info(f"⭐ VIP activated: user={tid} until={new_until.isoformat()}")
    await m.answer(vip_t(lang, "vip_thanks", until=format_vip_date(new_until)), parse_mode="HTML")


@router.message(Command("paysupport"))
async def cmd_paysupport(m: Message):
    try:
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        await m.answer(vip_t(lang, "paysupport"))
    except Exception as e:
        logger.warning(f"cmd_paysupport error: {e}")


@router.message(Command("givevip"), F.from_user.id == ADMIN_ID)
async def cmd_givevip(m: Message, command: CommandObject):
    """Админ: /givevip <telegram_id> [дней] — выдать VIP вручную (владелец бота не может сам платить Stars своему боту)."""
    try:
        parts = (command.args or "").split()
        if not parts:
            await m.answer("Использование: /givevip <telegram_id> [дней]")
            return
        target = int(parts[0])
        days = int(parts[1]) if len(parts) > 1 else VIP_DURATION_DAYS
        until = await grant_vip(target, days)
        if until:
            await m.answer(f"✅ VIP для {target} до {format_vip_date(until)}")
        else:
            await m.answer("❌ Не удалось записать VIP (см. логи).")
    except ValueError:
        await m.answer("Использование: /givevip <telegram_id> [дней]")
    except Exception as e:
        logger.warning(f"cmd_givevip error: {e}")


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


# ПРЯМАЯ КОМАНДА ДЛЯ МОМЕНТАЛЬНОГО ПОЛУЧЕНИЯ КНОПКИ РЕЗЮМЕ
@router.message(Command("cv"))
async def cmd_cv(m: Message, state: FSMContext):
    try:
        await state.clear()
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        await m.answer("🟢 Кнопка конструктора обновлена внизу 👇", reply_markup=kb_active_menu(lang, tid=m.from_user.id))
    except Exception as e:
        logger.warning(f"cmd_cv error: {e}")


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
            await m.answer(t(lang, "already_stopped"), reply_markup=kb_stopped_menu(lang))
            return
        await asyncio.to_thread(db_delete_filter, m.from_user.id)
        await asyncio.to_thread(db_set_user_active, m.from_user.id, False)
        await m.answer(t(lang, "stop_donate"), parse_mode="HTML", reply_markup=kb_stopped_menu(lang))
    except Exception as e:
        logger.warning(f"cmd_stop error: {e}")


@router.message(Command("help"))
async def cmd_help(m: Message):
    try:
        lang = await asyncio.to_thread(get_user_lang, m.from_user.id)
        await m.answer(t(lang, "help"), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"cmd_help error: {e}")


@router.message(F.text.in_(ALL_BTN_RESET))
async def btn_reset(m: Message, state: FSMContext):
    await cmd_reset(m, state)


@router.message(F.text.in_(ALL_BTN_STOP))
async def btn_stop(m: Message, state: FSMContext):
    await cmd_stop(m, state)


@router.message(F.text.in_(ALL_BTN_HELP))
async def btn_help(m: Message):
    await cmd_help(m)


@router.message(F.text.in_(ALL_BTN_RESTART))
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


@router.message(SetupStates.city_custom, ~F.text.in_(ALL_MENU_BTNS))
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

        await bot.send_message(c.from_user.id, t(lang, "menu_active"), reply_markup=kb_active_menu(lang, tid=c.from_user.id))
        await send_promo(c.from_user.id, lang)
        await asyncio.sleep(1)

        jobs = await asyncio.to_thread(db_get_jobs_for_city, city, 150, 24)

        if not jobs and city != "all":
            await bot.send_message(c.from_user.id, t(lang, "loading_city"))
            ok = await trigger_scraper_for_city(city)
            if ok:
                jobs = await wait_for_city_jobs(city)

        is_vip = await asyncio.to_thread(db_is_vip, c.from_user.id)
        await send_jobs_to_user(c.from_user.id, jobs, user_filter=uf, limit=8, is_initial=True, is_vip=is_vip)
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
    logger.info("⏰ Check started")

    try:
        filters = await asyncio.to_thread(db_get_active_filters)
        logger.info(f"👥 Active filters: {len(filters)}")

        if not filters:
            logger.info("No active filters found.")
            await post_jobs_to_channels()
            return

        now = datetime.now(timezone.utc)
        active_filters = []

        # VIP-пользователи: без 3-дневной паузы + получают Lento/Infopraca
        vip_ids = await asyncio.to_thread(db_get_active_vip_ids)
        vip_lookup_ok = vip_ids is not None
        if vip_ids is None:
            vip_ids = set()
            logger.warning("⚠️ VIP lookup failed: пауза никого не трогаем в этом цикле, шлём только бесплатные источники")

        for f in filters:
            tid = f["telegram_id"]
            
            # ИСКЛЮЧАЕМ ГРУППЫ И КАНАЛЫ ИЗ ОБЩЕЙ РАССЫЛКИ ЛЮДЯМ (они шлются только через автопостинг)
            if tid < 0:
                continue

            last_renewal_str = f.get("last_renewal")

            if last_renewal_str and tid > 0 and vip_lookup_ok and tid not in vip_ids:
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
            await post_jobs_to_channels()
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

            # ЛИМИТ: 5 вакансий за цикл для каналов (не спамить!), 15 для юзеров в ЛС
            user_limit = 5 if tid < 0 else 15

            async with semaphore:
                try:
                    return await send_jobs_to_user(
                        tid, fresh_jobs, user_filter=uf, limit=user_limit,
                        is_vip=(tid in vip_ids)
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
        
        # ЗАПУСК АВТОПОСТИНГА В КАНАЛЫ (он больше не плодит дубли благодаря Foreign Key!)
        await post_jobs_to_channels()

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


# ==================== АВТОМАТИЧЕСКАЯ ОЧИСТКА БАЗЫ ДАННЫХ И ИСТОРИИ ОТПРАВКИ ====================

async def db_cleanup_database():
    """
    Ежедневная асинхронная очистка базы данных Supabase от устаревших вакансий и логов истории отправки.
    - Обычные вакансии (OLX, Praca.pl и др.) и история их отправки удаляются через 3 дня.
    - Вакансии RocketJobs и история их отправки хранятся дольше и удаляются только через 30 дней.
    """
    logger.info("🗑 Запуск планировщика очистки базы данных от устаревших данных...")
    try:
        now = datetime.now(timezone.utc)
        cutoff_standard = (now - timedelta(days=3)).isoformat()
        cutoff_rocket = (now - timedelta(days=30)).isoformat()

        # 1. Сбор ID обычных вакансий (OLX, Praca.pl), созданных более 3 дней назад
        offset = 0
        page_size = 1000
        standard_ids = []
        while True:
            r = await asyncio.to_thread(
                lambda: supabase.table("jobs")
                .select("id")
                .neq("source", "RocketJobs")
                .lt("created_at", cutoff_standard)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            if not r or not r.data:
                break
            standard_ids.extend([row["id"] for row in r.data])
            if len(r.data) < page_size:
                break
            offset += page_size

        # 2. Сбор ID вакансий RocketJobs, созданных более 30 дней назад
        offset = 0
        rocket_ids = []
        while True:
            r = await asyncio.to_thread(
                lambda: supabase.table("jobs")
                .select("id")
                .eq("source", "RocketJobs")
                .lt("created_at", cutoff_rocket)
                .range(offset, offset + page_size - 1)
                .execute()
            )
            if not r or not r.data:
                break
            rocket_ids.extend([row["id"] for row in r.data])
            if len(r.data) < page_size:
                break
            offset += page_size

        total_old_ids = standard_ids + rocket_ids

        if total_old_ids:
            logger.info(f"🗑 Найдено {len(total_old_ids)} устаревших вакансий для полной очистки.")
            
            # 1. Очищаем логи отправки (sent_jobs) пакетами по 100
            for i in range(0, len(total_old_ids), 100):
                batch = total_old_ids[i:i+100]
                await asyncio.to_thread(
                    lambda: supabase.table("sent_jobs")
                    .delete()
                    .in_("job_id", batch)
                    .execute()
                )
            logger.info("✅ Устаревшая история отправки (sent_jobs) успешно очищена.")

            # 2. Удаляем сами вакансии из таблицы jobs пакетами по 100
            for i in range(0, len(total_old_ids), 100):
                batch = total_old_ids[i:i+100]
                await asyncio.to_thread(
                    lambda: supabase.table("jobs")
                    .delete()
                    .in_("id", batch)
                    .execute()
                )
            logger.info("✅ Устаревшие вакансии успешно удалены из таблицы jobs.")
        else:
            logger.info("✅ База данных чиста. Нет устаревших вакансий для удаления.")

    except Exception as e:
        logger.error(f"❌ Ошибка во время выполнения db_cleanup_database: {e}")


# ==================== MAIN ====================

async def main():
    logger.info("🚀 Bot starting...")
    await start_web_server()

    try:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=20))
        logger.info("⚙️ Thread pool executor limited to 20 workers for Render stability.")
    except Exception as e:
        logger.warning(f"Failed to set custom thread pool executor: {e}")

    # Принудительная разовая инициализация каналов сателлитов в БД на старте!
    logger.info("📡 Running startup channel synchronization...")
    await asyncio.to_thread(db_init_channels)

    s = AsyncIOScheduler(timezone="UTC")
    
    # 1. Рассылка юзерам и каналам каждые 15 минут
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
    
    # 2. VIP запуск парсера на GitHub каждые 25 минут
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
    
    # 3. Автоматическое обновление описания бота раз в час
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

    # 4. ЕЖЕДНЕВНАЯ автоматическая очистка базы данных от старья в 03:00 по UTC
    s.add_job(
        db_cleanup_database,
        "cron",
        hour=3,
        minute=0,
        id="database_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    
    s.start()

    logger.info("⏰ Scheduler started: first check in 10s, VIP scraper trigger in 20s, db cleanup daily at 03:00 UTC")

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
