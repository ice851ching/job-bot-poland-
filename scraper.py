import os
import sys
import asyncio
import hashlib
import logging
import re
import argparse
import random
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from supabase import create_client, Client
from curl_cffi import requests as cr
from bs4 import BeautifulSoup

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAIN_SCAN_CITIES = [
    "Warszawa", "Kraków", "Wrocław", "Poznań", "Gdańsk",
    "Łódź", "Katowice", "Lublin", "Toruń", "Szczecin",
    "Bydgoszcz", "Gdynia", "Białystok", "Rzeszów",
    "Kielce", "Gliwice", "Zabrze", "Olsztyn", "Opole",
    "Częstochowa"
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


def is_night_time() -> bool:
    try:
        local_time = datetime.now(ZoneInfo("Europe/Warsaw"))
        return local_time.hour >= 23 or local_time.hour < 8
    except Exception:
        return False


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
    text = re.sub(r"\s+", " ", text)
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
    if any(x in t for x in ["staż", "staz", "praktyk", "praktyка", "internship"]):
        return "staz"
    return None


def normalize_etat(text, salary_text=None):
    if not text:
        text = ""
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip()
    if any(x in t for x in ["parttime", "part time", "niepełny", "niepelny", "неполный", "неповний", "1/2", "3/4", "1/4", "pół etatu", "czesc etatu", "dodatkowa", "dorywcza", "student"]):
        return "part"
    if any(x in t for x in ["fulltime", "full time", "pełny", "pelny", "pełен", "pelen", "cały etat", "полный", "повний", "1/1", "etatowa"]):
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
            timeout=15,
        )
        return r.status_code, r.text
    except Exception as e:
        logger.error(f"fetch_url error for {url} ({impersonate_target}): {e}")
        return 0, ""


TARGET_BROWSERS = ["chrome120", "chrome110", "edge101", "safari_mac_12_0"]

def fetch_url_with_retry(url: str, referer: str = None):
    for browser in TARGET_BROWSERS:
        status, html = fetch_url(url, browser, referer)
        if status == 403:
            logger.warning(f"Got 403 with {browser} for {url}. Retrying with next profile...")
            time.sleep(1.0)
            continue
        return status, html
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


def cleanup_old_jobs():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old = supabase.table("jobs").select("id").lt("created_at", cutoff).execute()
        if old.data:
            old_ids = [j["id"] for j in old.data]
            logger.info(f"🗑 Cleaning {len(old_ids)} old jobs")
            for i in range(0, len(old_ids), 100):
                supabase.table("sent_jobs").delete().in_("job_id", old_ids[i:i + 100]).execute()
            supabase.table("jobs").delete().lt("created_at", cutoff).execute()
            logger.info("✅ Cleaned old jobs")
    except Exception as e:
        logger.error(f"cleanup_old_jobs error: {e}")


def get_active_cities_from_db() -> list:
    try:
        r = supabase.table("user_filters").select("city").eq("is_paused", False).execute()
        if not r.data:
            return []
        cities = {row["city"] for row in r.data if row.get("city")}
        if "all" in cities:
            return MAIN_SCAN_CITIES
        return list(cities)
    except Exception as e:
        logger.error(f"get_active_cities_from_db: {e}")
        return []


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


async def parse_rocketjobs(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    """
    Абсолютно неубиваемый парсер RocketJobs.pl.
    Ищет карточки по тегу a.offer-card и парсит город через иконку map-pin, а зарплату через Regex.
    Полная независимость от динамических MUI-классов!
    """
    try:
        slug = get_city_slug(city)
        if not slug:
            return 0

        categories = ["support", "gastronomia", "praca-w-sklepie"]
        jobs_to_save = []
        total_found = 0
        already_in_db = 0
        parse_errors = 0

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
                    # Поднимаемся до родительского контейнера li
                    card = a.find_parent("li")
                    if not card:
                        parse_errors += 1
                        continue

                    # 1. Заголовок
                    title_el = card.select_one("a.offer_list_offer_title_link") or card.select_one("h3 a")
                    if not title_el:
                        parse_errors += 1
                        continue
                    
                    title = strip_html(title_el.get_text(strip=True))
                    if not title:
                        title = a.get("title", "").replace("Zobacz ofertę", "").strip()

                    if not title or len(title) < 3:
                        parse_errors += 1
                        continue

                    # 2. Ссылка
                    link = title_el.get("href", "").strip()
                    if not link:
                        link = a.get("href", "").strip()
                    if not link:
                        parse_errors += 1
                        continue
                    if not link.startswith("http"):
                        link = "https://rocketjobs.pl" + link
                    link = link.split("?")[0].split("#")[0]

                    ext_id = hashlib.md5(f"rocketjobs_{link}".encode()).hexdigest()
                    
                    async with lock:
                        if ext_id in existing_ids:
                            already_in_db += 1
                            continue
                        existing_ids.add(ext_id)

                    # 3. Извлекаем город через иконку svg.lucide-map-pin (неубиваемый семантический путь!)
                    job_city = city
                    pin_icon = card.select_one("svg.lucide-map-pin")
                    if pin_icon:
                        # Поднимаемся до контейнера (Box или Stack), держащего иконку и город
                        container = pin_icon.find_parent(class_=re.compile(r"MuiStack|MuiBox|mui-"))
                        if container:
                            loc_text = strip_html(container.get_text(" ", strip=True))
                            if loc_text:
                                # Очищаем "Toruń , +4 Lokalizacje" -> "Toruń"
                                job_city = loc_text.split(",")[0].split()[0].replace(",", "").strip()

                    if not city_matches(job_city, city):
                        continue

                    # 4. Извлекаем зарплату через Regex (находим цифры + PLN/zł/EUR, игнорируя mui-хеш классы)
                    card_full_text = card.get_text(" ", strip=True)
                    salary = None
                    if "nieujawnione" not in card_full_text.lower():
                        # Ищет одиночные суммы и диапазоны типа: "3 500 - 10 500 PLN/mies." или "6 000 zł"
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
            f"RocketJobs saved={saved} (found={total_found}, in_db={already_in_db}, errors={parse_errors}) city={city}"
        )
        return saved
    except Exception as e:
        logger.error(f"parse_rocketjobs({city}) error: {e}")
        return 0


# ==================== ДИСПЕТЧЕР И MAIN ====================

async def scrape_city_task(city: str, existing_ids: set, semaphore: asyncio.Semaphore, lock: asyncio.Lock) -> int:
    async with semaphore:
        results = await asyncio.gather(
            parse_olx(city, existing_ids, lock),
            parse_praca_pl(city, existing_ids, lock),
            parse_rocketjobs(city, existing_ids, lock)
        )
        return sum(results)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default=None)
    args = parser.parse_args()

    if not args.city and is_night_time():
        logger.info("🌙 Night mode active. Skipping.")
        sys.exit(0)

    try:
        lt = datetime.now(ZoneInfo("Europe/Warsaw"))
        if not args.city and lt.hour == 8 and lt.minute < 35:
            cleanup_old_jobs()
    except Exception:
        pass

    existing_ids = get_all_existing_ids()
    cities = [args.city] if args.city else (get_active_cities_from_db() or MAIN_SCAN_CITIES[:5])

    city_sem = asyncio.Semaphore(3)
    ids_lock = asyncio.Lock()
    tasks = [scrape_city_task(c, existing_ids, city_sem, ids_lock) for c in cities]
    results = await asyncio.gather(*tasks)

    logger.info(f"✅ Done. Total saved across all cities: {sum(results)}")


if __name__ == "__main__":
    asyncio.run(main())
