import os
import sys
import asyncio
import hashlib
import logging
import re
import argparse
import random
import time
import unicodedata
import urllib.parse  # Добавлен импорт для экранирования URL
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from supabase import create_client, Client
from curl_cffi import requests as cr
from bs4 import BeautifulSoup

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
CF_WORKER_URL = os.getenv("CF_WORKER_URL")  # Читаем адрес Cloudflare воркера

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Города твоих Telegram-каналов (парсер будет шерстить их ВСЕГВА!)
CHANNEL_CITIES = [
    "Lublin", "Białystok", "Radom", "Częstochowa", "Gdynia",
    "Poznań", "Rzeszów", "Bydgoszcz"
]

MAIN_SCAN_CITIES = [
    "Warszawa", "Kraków", "Wrocław", "Poznań", "Gdańsk",
    "Łódź", "Katowice", "Lublin", "Toruń", "Szczecin",
    "Bydgoszcz", "Gdynia", "Białystok", "Rzeszów",
    "Kielce", "Gliwice", "Zabrze", "Olsztyn", "Opole",
    "Częstochowa", "Radom", "Zielona Góra"
]

CITY_SLUGS = {
    "warszawa": "warszawa", "kraków": "krakow", "krakow": "krakow",
    "wrocław": "wroclaw", "wroclaw": "wroclaw", "poznań": "poznan", "poznan": "poznan",
    "gdańsk": "gdansk", "gdansk": "gdansk", "łódź": "lodz", "lodz": "lodz",
    "katowice": "katowice", "lublin": "lublin", "toruń": "torun", "torun": "torun",
    "szczecin": "szczecin", "bydgoszcz": "bydgoszcz", "białystok": "bialystok",
    "gdynia": "gdynia", "częstochowa": "czestochowa", "sosnowiec": "sosnowiec",
    "rzeszów": "rzeszow", "rzeszow": "rzeszow", "kielce": "kielce",
    "gliwice": "gliwice", "zabrze": "zabrze", "olsztyn": "olsztyn", "opole": "opole",
    "zielona góra": "zielona-gora", "radom": "radom", "tychy": "tychy",
    "tarnów": "tarnow", "tarnow": "tarnow",
}


def get_city_slug(city: str) -> str:
    if not city:
        return ""
    cl = city.lower().strip()
    if cl in CITY_SLUGS:
        return CITY_SLUGS[cl]
    for k, v in {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z', ' ': '-'}.items():
        cl = cl.replace(k, v)
    return cl


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = re.sub(r"\.css-[a-z0-9]+\{[^}]*\}", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return text.strip()


def normalize_umowa(text):
    if not text:
        return None
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip().replace("_", " ").replace("-", " ")
    if any(x in t for x in ["zlecenie", "zlecenia", "zlece", "mandate contract", "доручення", "договор злецения"]) or re.search(r'\b(uz)\b', t):
        return "umowa_zlecenie"
    if any(x in t for x in ["o pracę", "o prace", "praca etatowa", "employment contract", "трудовой", "трудовий"]) or re.search(r'\b(uop)\b', t):
        return "umowa_o_prace"
    if any(x in t for x in ["b2b", "selfemployment", "self-employment", "kontrakt b2b", "kontrakt gospodarczy"]):
        return "b2b"
    if any(x in t for x in ["dzieło", "dzielo"]) or re.search(r'\b(uod)\b', t):
        return "umowa_o_dzielo"
    if any(x in t for x in ["staż", "staz", "praktyk", "praktyki", "internship"]):
        return "staz"
    return None


def normalize_etat(text, salary_text=None):
    if not text:
        text = ""
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip()
    if re.search(r"0[,.]5\s*etat", t):
        return "part"
    if any(x in t for x in ["parttime", "part time", "niepełny", "niepelny", "неполный", "неповний", "1/2", "3/4", "1/4", "pół etatu", "pol etatu", "czesc etatu", "część etatu", "cześć etatu", "dodatkowa", "dorywcza", "student"]):
        return "part"
    if any(x in t for x in ["fulltime", "full time", "pełny", "pelny", "pełen", "pelen", "cały etat", "caly etat"]):
        return "full"
    if salary_text and any(x in str(salary_text).lower() for x in ["mies", "m-c", "mc", "/ m", "zł/mies"]):
        return "full"
    return None


def city_matches(job_city, filter_city):
    if not filter_city:
        return True
    if not job_city:
        return False
    fc = get_city_slug(filter_city)
    jc = get_city_slug(job_city)
    return fc == jc or fc in jc or jc in fc


# ==================== СЕТЕВОЙ КЛИЕНТ ====================

def fetch_url(url: str, impersonate_target: str = "chrome120", referer: str = None):
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "upgrade-insecure-requests": "1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site" if referer else "none",
        "sec-fetch-user": "?1",
    }
    if referer:
        headers["referer"] = referer

    try:
        r = cr.get(
            url,
            headers=headers,
            impersonate=impersonate_target,
            timeout=22,
        )
        return r.status_code, r.text
    except Exception as e:
        logger.error(f"fetch_url error for {url} ({impersonate_target}): {e}")
        return 0, ""


TARGET_BROWSERS = ["chrome120", "chrome110", "edge101", "safari184"]

def fetch_url_with_retry(url: str, referer: str = None):
    # 1. Пробуем получить страницу стандартным бронебойным путем (через curl_cffi)
    for browser in TARGET_BROWSERS:
        status, html = fetch_url(url, browser, referer)
        if status == 200 and html:
            return status, html
        if status == 403:
            logger.warning(f"Got 403 with {browser} for {url}. Retrying with next profile...")
            time.sleep(1.0)
            continue
        # Если статус не 403 и не 200 (например, 500 или 404), выходим
        if status != 0:
            return status, html
            
    # 2. АВАРИЙНЫЙ РЕЖИМ (ПЛАН Б): Если все браузеры поймали 403 (блокировка IP), задействуем Cloudflare Worker
    if CF_WORKER_URL:
        try:
            logger.warning(f"🚨 АВАРИЙНЫЙ РЕЖИМ: IP заблокирован. Пробуем пробить через Cloudflare Worker для {url}")
            encoded_url = urllib.parse.quote(url, safe='')
            worker_target_url = f"{CF_WORKER_URL}?url={encoded_url}"
            
            # Делаем запрос к нашему прокси-воркеру
            status, html = fetch_url(worker_target_url, "chrome120")
            if status == 200 and html:
                logger.info(f"✅ Cloudflare Worker успешно пробил блокировку для {url}!")
                return 200, html
            else:
                logger.error(f"❌ Cloudflare Worker тоже вернул ошибку: {status}")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка аварийного шлюза Cloudflare Worker: {e}")

    return 403, ""


# ==================== DATABASE ====================

def get_all_existing_ids() -> set:
    try:
        existing = set()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        page_size = 1000
        offset = 0
        while True:
            r = supabase.table("jobs").select("external_id").gt("created_at", cutoff).range(offset, offset + page_size - 1).execute()
            if not r.data:
                break
            for row in r.data:
                if row.get("external_id"):
                    existing.add(row["external_id"])
            if len(r.data) < page_size:
                break
            offset += page_size
        logger.info(f"📦 Loaded {len(existing)} active job IDs from database.")
        return existing
    except Exception as e:
        logger.error(f"get_all_existing_ids error: {e}")
        return set()


def db_insert_jobs_batch_sync(jobs_list: list) -> int:
    if not jobs_list:
        return 0
    try:
        logger.info(f"💾 Saving batch of {len(jobs_list)} jobs to Supabase...")
        r = supabase.table("jobs").upsert(
            jobs_list,
            on_conflict="external_id",
            ignore_duplicates=True
        ).execute()
        return len(r.data) if r.data else len(jobs_list)
    except Exception as e:
        logger.error(f"❌ db_insert_jobs_batch error: {e}")
        return 0


async def db_insert_jobs_batch(jobs_list: list) -> int:
    return await asyncio.to_thread(db_insert_jobs_batch_sync, jobs_list)


def get_active_cities_from_db() -> list:
    """
    Выбирает города активных юзеров, и ГАРАНТИРОВАННО добавляет города твоих каналов,
    чтобы они наполнялись контентом 24/7!
    """
    try:
        r = supabase.table("user_filters").select("city").eq("is_paused", False).execute()
        cities = {row["city"] for row in r.data if row.get("city")}
        
        # Если кто-то ищет во всей Польше, берем полный базовый список городов
        if "all" in cities:
            final_cities = set(MAIN_SCAN_CITIES)
        else:
            final_cities = cities
            
        # Гарантированно добавляем города каналов в скан-лист, даже если сработал режим "all"
        for c in CHANNEL_CITIES:
            final_cities.add(c)
            
        return list(final_cities)
    except Exception as e:
        logger.error(f"get_active_cities_from_db: {e}")
        return CHANNEL_CITIES


# ==================== ПАРСЕРЫ ====================

async def parse_olx(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    try:
        slug = get_city_slug(city)
        url = f"https://www.olx.pl/praca/{slug}/?search%5Border%5D=created_at:desc" if slug else "https://www.olx.pl/praca/?search%5Border%5D=created_at:desc"
        status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")
        if status != 200 or not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", attrs={"data-cy": "l-card"}) or soup.select("div.jobs-ad-card")
        if not cards:
            return 0

        jobs_to_save = []

        for card in cards:
            try:
                title_tag = card.find("h4") or card.select_one("a[data-testid='card-title-link']")
                link_tag = card.find("a", href=True)
                if not title_tag or not link_tag:
                    continue

                title = strip_html(title_tag.get_text(strip=True))
                if not title:
                    continue

                link = link_tag["href"]
                if not link.startswith("http"):
                    link = "https://www.olx.pl" + link
                link = link.split("?")[0].split("#")[0]

                link_lower = link.lower()
                if "olx.pl" in link_lower:
                    if "/oferta/" not in link_lower:
                        continue
                    if "/uzytkownik/" in link_lower:
                        continue

                ext_id = hashlib.md5(f"olx_{link}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        continue
                    existing_ids.add(ext_id)

                card_text = card.get_text(" ", strip=True)

                salary = None
                for p in card.find_all("p"):
                    pt = p.get_text(strip=True).lower()
                    if "zł" in pt or "pln" in pt or "eur" in pt:
                        salary = strip_html(p.get_text())
                        break

                umowa_key = normalize_umowa(card_text) or normalize_umowa(title)
                etat_key = normalize_etat(card_text, salary) or normalize_etat(title, salary)

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": title,
                    "city": city,
                    "salary": salary,
                    "url": link,
                    "source": "OLX",
                    "umowa": umowa_key,
                    "etat": etat_key
                })
            except Exception as e:
                logger.error(f"OLX card item error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(f"OLX saved={saved} city={city}")
        return saved
    except Exception as e:
        logger.error(f"parse_olx({city}) error: {e}")
        return 0


async def parse_praca_pl(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    try:
        slug = get_city_slug(city)
        url = f"https://www.praca.pl/m-{slug}.html?m={city}"
        status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")
        if status != 200 or not html:
            logger.warning(f"Praca.pl returned status {status} for {city}")
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.listing__item")
        
        logger.info(f"🔍 Praca.pl DEBUG: status={status}, cards_found={len(cards)} on page for {city}")

        if not cards:
            logger.warning(f"⚠️ Praca.pl: 0 cards found for {city}. Page title: {soup.title.string if soup.title else 'N/A'}")
            return 0

        jobs_to_save = []
        ignored_duplicate = 0
        ignored_city = 0

        for card in cards[:40]:
            try:
                title_el = card.select_one("a.listing__title")
                if not title_el:
                    continue
                title = strip_html(title_el.get_text(strip=True))
                if not title or len(title) < 3:
                    continue

                link = title_el.get("href", "").split("#")[0]
                if not link.startswith("http"):
                    link = "https://www.praca.pl" + link

                ext_id = hashlib.md5(f"pracapl_{link}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        ignored_duplicate += 1
                        continue
                    existing_ids.add(ext_id)

                job_city = city
                loc_el = card.select_one("span.listing__location-name")
                if loc_el:
                    loc_text = strip_html(loc_el.get_text(" ", strip=True))
                    if loc_text:
                        job_city = loc_text.split()[0].replace(",", "").strip()

                if not city_matches(job_city, city):
                    ignored_city += 1
                    continue

                dt_el = card.select_one("div.listing__main-details")
                dt = strip_html(dt_el.get_text(" ", strip=True)).lower() if dt_el else ""

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": title,
                    "city": job_city,
                    "salary": None,
                    "url": link,
                    "source": "Praca.pl",
                    "umowa": normalize_umowa(dt) or normalize_umowa(title),
                    "etat": normalize_etat(dt) or normalize_etat(title)
                })
            except Exception as e:
                logger.debug(f"Praca.pl card error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(
            f"Praca.pl saved={saved} (duplicates={ignored_duplicate}, city_mismatch={ignored_city}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_praca_pl({city}) error: {e}")
        return 0


def rj_norm(text) -> str:
    """Нормализация для ключа дедупликации: без диакритики, регистра, пунктуации и лишних пробелов.
    ł/Ł не раскладываются через NFKD — заменяем вручную. \\w оставляет любые буквы/цифры (в т.ч. кириллицу)."""
    text = strip_html(text or "")
    text = text.replace("\u0141", "L").replace("\u0142", "l")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[\W_]+", " ", text.casefold())
    return text.strip()


def rj_company(card) -> str:
    """Название компании: <p> рядом с иконкой svg.lucide-building (не building-2!)."""
    icon = card.select_one("svg.lucide-building")
    node = icon.parent if icon else None
    for _ in range(3):
        if node is None:
            break
        p = node.find_next_sibling("p")
        if p and p.get_text(strip=True):
            return p.get_text(strip=True)
        node = node.parent
    return ""


def rj_slug(link: str) -> str:
    """Слаг из пути ссылки без домена, query, хэша и хвостового слэша."""
    path = urllib.parse.urlparse(link).path
    return path.rstrip("/").rsplit("/", 1)[-1].lower()


async def parse_rocketjobs(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    try:
        slug = get_city_slug(city)
        if not slug:
            return 0

        categories = ["support", "gastronomia", "praca-w-sklepie"]
        jobs_to_save = []
        total_found = 0
        already_in_db = 0
        dupes_in_run = 0
        parse_errors = 0
        seen_this_run = set()

        for category in categories:
            await asyncio.sleep(random.uniform(1.5, 3.5))
            
            url = f"https://rocketjobs.pl/oferty-pracy/{slug}/{category}?radius=0&sortBy=newest"
            status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")
            
            if status != 200 or not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            
            # Находим карточки по стабильному семантическому классу a.offer-card
            offer_links = soup.select("a.offer-card")
            total_found += len(offer_links)

            for a in offer_links:
                try:
                    # Сайт больше не оборачивает карточки в <li> — раньше здесь было
                    # a.find_parent("li"), из-за чего 100% карточек улетали в errors,
                    # даже не доходя до извлечения title. Берём саму ссылку-карточку
                    # как контейнер; если в ней подозрительно мало текста (похоже,
                    # это просто обёртка картинки, а не вся карточка) — поднимаемся
                    # к ближайшему li/article/div-родителю.
                    card = a
                    if len(card.get_text(strip=True)) < 20:
                        card = a.find_parent(["li", "article", "div"]) or a

                    # 1. Заголовок
                    # Самый надёжный источник — атрибут title на самой карточке-ссылке
                    # (формат "Zobacz ofertę <Название>"), он не зависит от внутренних
                    # CSS-классов, которые на сайте похожи на автогенерируемые и могут
                    # меняться при каждом деплое. Внутренние селекторы — как бонус/уточнение.
                    title = a.get("title", "").replace("Zobacz ofertę", "").strip()

                    title_el = card.select_one("a.offer_list_offer_title_link") or card.select_one("h3 a") or card.find("h3")
                    if title_el:
                        inner_title = strip_html(title_el.get_text(strip=True))
                        if inner_title:
                            title = inner_title

                    if not title or len(title) < 3:
                        parse_errors += 1
                        continue

                    # 2. Ссылка
                    link = a.get("href", "").strip()
                    if not link and title_el:
                        link = title_el.get("href", "").strip()
                    if not link:
                        parse_errors += 1
                        continue
                    if not link.startswith("http"):
                        link = "https://rocketjobs.pl" + link
                    link = link.split("?")[0].split("#")[0]

                    # 3. Извлекаем город через иконку svg.lucide-map-pin
                    # Сама иконка обёрнута в свой собственный <div class="MuiBox-root ...">,
                    # у которого кроме иконки ничего нет — он тоже подпадает под класс
                    # "MuiBox|mui-", поэтому find_parent(class_=...) останавливался на этой
                    # пустой обёртке и loc_text выходил пустым (а .split()[0] на пустой
                    # строке падал с IndexError). Поднимаемся по предкам, пока не найдём
                    # такого, где реально есть текст.
                    job_city = city
                    pin_icon = card.select_one("svg.lucide-map-pin")
                    if pin_icon:
                        container = pin_icon.parent
                        hops = 0
                        while container is not None and hops < 5:
                            loc_text = strip_html(container.get_text(" ", strip=True))
                            if loc_text:
                                job_city = loc_text.split(",")[0].split()[0].replace(",", "").strip() or city
                                break
                            container = container.parent
                            hops += 1

                    if not city_matches(job_city, city):
                        continue

                    # ID = компания + название + город карточки. URL в ключе НЕ участвует:
                    # слаг у RocketJobs = компания + название + город + категория оффера
                    # (см. href карточки), т.е. меняется при смене города/категории, а поля — нет.
                    # Если компанию достать не удалось — берём слаг из URL как запасной "идентификатор".
                    identity = rj_norm(rj_company(card)) or rj_slug(link)
                    ext_id = hashlib.md5(
                        f"rocketjobs2|{identity}|{rj_norm(title)}|{rj_norm(job_city)}".encode("utf-8")
                    ).hexdigest()

                    if ext_id in seen_this_run:
                        dupes_in_run += 1
                        continue
                    seen_this_run.add(ext_id)

                    async with lock:
                        if ext_id in existing_ids:
                            already_in_db += 1
                            continue
                        existing_ids.add(ext_id)

                    # 4. Извлекаем зарплату через Regex
                    card_full_text = card.get_text(" ", strip=True)
                    salary = None
                    if "nieujawnione" not in card_full_text.lower():
                        sal_match = re.search(r"(\d[\d\s]*(?:\s*[-–]\s*\d[\d\s]*)?\s*(?:PLN|zł|EUR)(?:/[a-zA-Zа-яА-Я]+)*)", card_full_text, re.IGNORECASE)
                        if sal_match:
                            salary = strip_html(sal_match.group(0))

                    jobs_to_save.append({
                        "external_id": ext_id,
                        "title": title,
                        "city": job_city,
                        "salary": salary,
                        "url": link,
                        "source": "RocketJobs",
                        "umowa": normalize_umowa(card_full_text) or normalize_umowa(title),
                        "etat": normalize_etat(card_full_text, salary) or normalize_etat(title, salary)
                    })
                except Exception as e:
                    parse_errors += 1
                    logger.debug(f"RocketJobs card parse error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(
            f"RocketJobs saved={saved} (found={total_found}, in_db={already_in_db}, dupes_in_run={dupes_in_run}, errors={parse_errors}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_rocketjobs({city}) error: {e}")
        return 0


# ==================== LENTO.PL ====================

# Если у какого-то города поддомен на Lento отличается от обычного slug — добавь сюда.
# Формат: "slug из get_city_slug": "поддомен на lento.pl"
LENTO_SUBDOMAIN_OVERRIDES = {
    # "zielona-gora": "zielonagora",
}


def is_lento_promo(card) -> bool:
    """Промо-объявление: класс tablelist-tr-promo или плашка 'Promowane'."""
    if "tablelist-tr-promo" in (card.get("class") or []):
        return True
    return card.select_one(".promo-label") is not None


def extract_lento_card(card):
    """Разбирает одну карточку Lento. Возвращает dict с полями или None, если карточка битая."""
    title_el = card.select_one("a.title-list-item")
    if not title_el:
        return None

    title = strip_html(title_el.get_text(" ", strip=True))
    link = (title_el.get("href") or "").strip().split("?")[0].split("#")[0]
    if not title or len(title) < 3 or not link:
        return None
    if not link.startswith("http"):
        link = "https://lento.pl" + link

    # Стабильный id объявления: data-id на карточке, запасной вариант — число в конце ссылки
    ad_id = card.get("data-id")
    if not ad_id:
        m = re.search(r",(\d+)\.html$", link)
        ad_id = m.group(1) if m else None

    # Город
    job_city = None
    loc_el = card.select_one(".licon-pin-f")
    if loc_el:
        loc_text = re.sub(r"\s+", " ", loc_el.get_text(" ", strip=True)).strip()
        m = re.search(r"\(([^)]+)\)", loc_text)  # формат "Cała Polska (Wrocław)"
        job_city = (m.group(1) if m else loc_text).strip() or None

    # Зарплата: "4 806 zł /mies. brutto" или "od 4806 zł do 5100 zł /mies. brutto"
    salary = None
    sal_el = card.select_one("div.param-list-row div.padding-top-2")
    if sal_el:
        sal_text = re.sub(r"\s+", " ", sal_el.get_text(" ", strip=True)).strip().replace(" /", "/")
        if re.search(r"\d", sal_text):
            salary = sal_text

    # Теги: [Категория(ссылка), "Pełny etat", "Umowa o pracę"] — категорию (с <a>) пропускаем
    tabs = [
        t.get_text(" ", strip=True)
        for t in card.select("span.list-atrr-item-tab")
        if not t.find("a")
    ]
    attrs_text = " ".join(tabs)

    return {
        "ad_id": ad_id,
        "title": title,
        "link": link,
        "city": job_city,
        "salary": salary,
        "umowa": normalize_umowa(attrs_text) or normalize_umowa(title),
        "etat": normalize_etat(attrs_text, salary) or normalize_etat(title, salary),
    }


async def parse_lento(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    try:
        slug = get_city_slug(city)
        if not slug:
            return 0
        subdomain = LENTO_SUBDOMAIN_OVERRIDES.get(slug, slug)
        url = f"https://{subdomain}.lento.pl/praca/dam-prace.html"

        status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")
        if status != 200 or not html:
            logger.warning(f"Lento returned status {status} for {city} ({url})")
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("div.tablelist-tr")
        if not cards:
            logger.warning(f"⚠️ Lento: 0 cards found for {city}. Page title: {soup.title.string if soup.title else 'N/A'}")
            return 0

        jobs_to_save = []
        ignored_promo = 0
        ignored_duplicate = 0
        ignored_city = 0
        parse_errors = 0

        for card in cards:
            try:
                # Promowane — выкидываем сразу
                if is_lento_promo(card):
                    ignored_promo += 1
                    continue

                data = extract_lento_card(card)
                if not data:
                    parse_errors += 1
                    continue

                job_city = data["city"] or city
                if not city_matches(job_city, city):
                    ignored_city += 1
                    continue

                ext_id = hashlib.md5(f"lento_{data['ad_id'] or data['link']}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        ignored_duplicate += 1
                        continue
                    existing_ids.add(ext_id)

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": data["title"],
                    "city": job_city,
                    "salary": data["salary"],
                    "url": data["link"],
                    "source": "Lento",
                    "umowa": data["umowa"],
                    "etat": data["etat"],
                })
            except Exception as e:
                parse_errors += 1
                logger.debug(f"Lento card error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(
            f"Lento saved={saved} (cards={len(cards)}, promo={ignored_promo}, duplicates={ignored_duplicate}, "
            f"city_mismatch={ignored_city}, errors={parse_errors}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_lento({city}) error: {e}")
        return 0


# ==================== FACHPRACA.PL ====================

# На Fachpraca город в URL пишется польскими буквами: /oferty-pracy/l/toruń/
# Если у какого-то города адрес отличается — впиши сюда готовый кусок URL.
FACHPRACA_CITY_OVERRIDES = {
    # "Zielona Góra": "zielona-góra",
}

# Сайт стабильно банит по ASN/IP-репутации (403 на всех профилях браузера) и даже через
# Cloudflare Worker — то есть это не просто "подобрать заголовки", а либо полноценный
# JS-челлендж, либо блокировка облачных диапазонов на уровне WAF. Пока нет чистого
# (не датацентрового) прокси или headless-браузера — гонять его смысла нет, только
# тратим запросы впустую. Поставь True, если появится решение под это.
ENABLE_FACHPRACA = False


def build_fachpraca_url(city: str) -> str:
    raw = FACHPRACA_CITY_OVERRIDES.get(city) or city.lower().strip().replace(" ", "-")
    return f"https://www.fachpraca.pl/oferty-pracy/l/{urllib.parse.quote(raw)}/"


def is_fachpraca_promo(card) -> bool:
    """Выделенные/промо-объявления: любой класс-модификатор кроме базового job-list__offer."""
    classes = [c for c in (card.get("class") or []) if c != "job-list__offer"]
    return any(x in " ".join(classes).lower() for x in ["promo", "wyroznion", "wyróżnion", "featured", "highlight", "top"])


def extract_fachpraca_card(card):
    """Разбирает одну карточку Fachpraca (li.job-list__offer). Возвращает dict или None."""
    title_el = card.select_one("a.job-list__job-name")
    if not title_el:
        return None

    title = strip_html(title_el.get("title") or title_el.get_text(" ", strip=True))
    link = (title_el.get("href") or "").strip().split("?")[0]
    if not title or len(title) < 3 or not link:
        return None
    if not link.startswith("http"):
        link = "https://www.fachpraca.pl" + link

    # Стабильный id: data-secret у кнопки "Obserwuj", запасной вариант — число в конце ссылки
    ad_id = None
    btn = card.select_one("button.job-list__watch[data-secret]")
    if btn:
        ad_id = btn.get("data-secret")
    if not ad_id:
        m = re.search(r"-(\d+)/?$", link)
        ad_id = m.group(1) if m else None

    def text_of(selector):
        el = card.select_one(selector)
        if not el:
            return None
        t = el.get_text(" ", strip=True).replace("\xa0", " ")
        t = re.sub(r"\s+", " ", t).strip()
        return t or None

    job_city = text_of("p.job-list__job-location")

    # Зарплата: "od 4 806,00 do 5 600,00 PLN miesięcznie brutto"
    salary = text_of("p.job-list__job-pay")
    if salary and not re.search(r"\d", salary):
        salary = None

    # Условия: [должность, категория, тип договора, этат] — например "umowa o pracę", "pełny etat"
    conditions = " ".join(
        re.sub(r"\s+", " ", li.get_text(" ", strip=True))
        for li in card.select("ul.job-list__conditions li")
    )

    return {
        "ad_id": ad_id,
        "title": title,
        "link": link,
        "city": job_city,
        "salary": salary,
        "umowa": normalize_umowa(conditions) or normalize_umowa(title),
        "etat": normalize_etat(conditions, salary) or normalize_etat(title, salary),
    }


async def parse_fachpraca(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    if not ENABLE_FACHPRACA:
        return 0
    try:
        if not city:
            return 0

        url = build_fachpraca_url(city)
        status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")

        soup = BeautifulSoup(html, "html.parser") if (status == 200 and html) else None
        cards = soup.select("li.job-list__offer") if soup else []

        # Запасной вариант для составных названий: пробелы вместо дефиса ("zielona góra")
        if not cards and " " in city.strip() and city not in FACHPRACA_CITY_OVERRIDES:
            alt_url = f"https://www.fachpraca.pl/oferty-pracy/l/{urllib.parse.quote(city.lower().strip())}/"
            logger.info(f"Fachpraca: пробуем запасной URL для {city}: {alt_url}")
            status, html = await asyncio.to_thread(fetch_url_with_retry, alt_url, "https://www.google.com/")
            if status == 200 and html:
                soup = BeautifulSoup(html, "html.parser")
                cards = soup.select("li.job-list__offer")
                if cards:
                    url = alt_url

        if not cards:
            logger.warning(
                f"⚠️ Fachpraca: 0 cards for {city} (status={status}, url={url}, "
                f"title={soup.title.string if soup and soup.title else 'N/A'})"
            )
            return 0

        jobs_to_save = []
        ignored_promo = 0
        ignored_duplicate = 0
        ignored_city = 0
        parse_errors = 0

        for card in cards[:50]:
            try:
                if is_fachpraca_promo(card):
                    ignored_promo += 1
                    continue

                data = extract_fachpraca_card(card)
                if not data:
                    parse_errors += 1
                    continue

                job_city = data["city"] or city
                if not city_matches(job_city, city):
                    ignored_city += 1
                    continue

                ext_id = hashlib.md5(f"fachpraca_{data['ad_id'] or data['link']}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        ignored_duplicate += 1
                        continue
                    existing_ids.add(ext_id)

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": data["title"],
                    "city": job_city,
                    "salary": data["salary"],
                    "url": data["link"],
                    "source": "Fachpraca",
                    "umowa": data["umowa"],
                    "etat": data["etat"],
                })
            except Exception as e:
                parse_errors += 1
                logger.debug(f"Fachpraca card error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(
            f"Fachpraca saved={saved} (cards={len(cards)}, promo={ignored_promo}, duplicates={ignored_duplicate}, "
            f"city_mismatch={ignored_city}, errors={parse_errors}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_fachpraca({city}) error: {e}")
        return 0


# ==================== INFOPRACA.PL ====================

# Воеводства, которые приезжают хвостом в строке локации и городом не являются
POLISH_VOIVODESHIPS = {
    "dolnośląskie", "kujawsko-pomorskie", "lubelskie", "lubuskie", "łódzkie",
    "małopolskie", "mazowieckie", "opolskie", "podkarpackie", "podlaskie",
    "pomorskie", "śląskie", "świętokrzyskie", "warmińsko-mazurskie",
    "wielkopolskie", "zachodniopomorskie",
}

# Зарплата в тексте: "4 806 zł brutto/mies", "5000-6000 PLN", "2 600 € netto"
SALARY_RE = re.compile(
    r"\d[\d\s\u00a0.,]*(?:\s*[-–]\s*\d[\d\s\u00a0.,]*)?\s*(?:zł|pln|eur|€)"
    r"(?:\s*(?:brutto|netto))?(?:\s*/\s*[a-ząćęłńóśźż.]+)?",
    re.IGNORECASE,
)


def clean_infopraca_city(loc_text: str, search_city: str):
    """'65-548 Zielona Góra, lubuskie' / 'Zielona Góra, Gorzów Wielkopolski, lubuskie' -> город."""
    if not loc_text:
        return None
    parts = []
    for chunk in loc_text.split(","):
        chunk = re.sub(r"\b\d{2}-\d{3}\b", "", chunk).strip()  # убираем почтовый индекс
        if not chunk or chunk.lower() in POLISH_VOIVODESHIPS:
            continue
        parts.append(chunk)
    if not parts:
        return None
    # Если объявление на несколько городов, берём тот, который мы и искали
    for part in parts:
        if city_matches(part, search_city):
            return part
    return parts[0]


def extract_infopraca_card(card, search_city: str):
    """Разбирает одну карточку Infopraca (article.job-card). Возвращает dict или None."""
    title_el = card.select_one("a.job-card__title-link")
    if not title_el:
        return None

    title = strip_html(title_el.get_text(" ", strip=True))
    link = (title_el.get("href") or "").strip().split("?")[0].split("#")[0]
    if not title or len(title) < 3 or not link:
        return None
    if not link.startswith("http"):
        link = "https://www.infopraca.pl" + link

    ad_id = card.get("data-job-card-job-offer-id-value")
    if not ad_id:
        m = re.search(r"/(\d+)/?$", link)
        ad_id = m.group(1) if m else None

    def clean(el):
        if not el:
            return None
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        return t or None

    # Первая метка — локация, вторая (если есть) — режим занятости: Full time / Part time / Indifferent
    metas = [clean(m) for m in card.select("span.job-card__meta-item")]
    metas = [m for m in metas if m]
    job_city = clean_infopraca_city(metas[0] if metas else None, search_city)
    etat_label = metas[1] if len(metas) > 1 else None

    description = clean(card.select_one("p.job-card__description")) or ""
    badges = clean(card.select_one("div.job-card__badges")) or ""

    # Зарплаты отдельным полем тут нет — ищем в плашках, затем в тексте объявления
    salary = None
    for source in (badges, description):
        if not source:
            continue
        m = SALARY_RE.search(source)
        if m:
            salary = re.sub(r"\s+", " ", m.group(0)).strip()
            break

    # Договор и этат ловим по ключевым словам: метка -> текст -> заголовок
    haystack = " ".join(x for x in [etat_label, badges, description] if x)

    return {
        "ad_id": ad_id,
        "title": title,
        "link": link,
        "city": job_city,
        "salary": salary,
        "umowa": normalize_umowa(badges) or normalize_umowa(description) or normalize_umowa(title),
        "etat": (normalize_etat(etat_label, salary) if etat_label else None)
                or normalize_etat(haystack, salary)
                or normalize_etat(title, salary),
    }


async def parse_infopraca(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    try:
        if not city:
            return 0

        query = urllib.parse.urlencode({"q": "", "lc": city, "d": 0, "sort": "last_update"})
        url = f"https://www.infopraca.pl/praca?{query}"

        status, html = await asyncio.to_thread(fetch_url_with_retry, url, "https://www.google.com/")
        if status != 200 or not html:
            logger.warning(f"Infopraca returned status {status} for {city}")
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("article.job-card")
        if not cards:
            logger.warning(f"⚠️ Infopraca: 0 cards for {city}. Page title: {soup.title.string if soup.title else 'N/A'}")
            return 0

        jobs_to_save = []
        ignored_duplicate = 0
        ignored_city = 0
        parse_errors = 0

        for card in cards[:50]:
            try:
                data = extract_infopraca_card(card, city)
                if not data:
                    parse_errors += 1
                    continue

                job_city = data["city"] or city
                if not city_matches(job_city, city):
                    ignored_city += 1
                    continue

                ext_id = hashlib.md5(f"infopraca_{data['ad_id'] or data['link']}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        ignored_duplicate += 1
                        continue
                    existing_ids.add(ext_id)

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": data["title"],
                    "city": job_city,
                    "salary": data["salary"],
                    "url": data["link"],
                    "source": "Infopraca",
                    "umowa": data["umowa"],
                    "etat": data["etat"],
                })
            except Exception as e:
                parse_errors += 1
                logger.debug(f"Infopraca card error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(
            f"Infopraca saved={saved} (cards={len(cards)}, duplicates={ignored_duplicate}, "
            f"city_mismatch={ignored_city}, errors={parse_errors}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_infopraca({city}) error: {e}")
        return 0


# ==================== ДИСПЕТЧЕР И MAIN ====================

async def scrape_city_task(city: str, existing_ids: set, semaphore: asyncio.Semaphore, lock: asyncio.Lock) -> int:
    async with semaphore:
        results = await asyncio.gather(
            parse_olx(city, existing_ids, lock),
            parse_praca_pl(city, existing_ids, lock),
            parse_rocketjobs(city, existing_ids, lock),
            parse_lento(city, existing_ids, lock),
            parse_fachpraca(city, existing_ids, lock),
            parse_infopraca(city, existing_ids, lock)
        )
        return sum(results)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default=None)
    args = parser.parse_args()

    # Очистка базы полностью передана боту (крон 03:00 UTC в bot.py).
    # Парсер работает как спецназ: залетел ➔ собрал свежак ➔ ушёл.

    existing_ids = get_all_existing_ids()
    cities = [args.city] if args.city else (get_active_cities_from_db() or MAIN_SCAN_CITIES[:5])

    city_sem = asyncio.Semaphore(3)
    ids_lock = asyncio.Lock()
    tasks = [scrape_city_task(c, existing_ids, city_sem, ids_lock) for c in cities]
    results = await asyncio.gather(*tasks)

    logger.info(f"✅ Done. Total saved across all cities: {sum(results)}")


if __name__ == "__main__":
    asyncio.run(main())
