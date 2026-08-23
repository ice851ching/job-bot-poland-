import os
import sys
import asyncio
import hashlib
import logging
import re
import argparse
import random
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
    if any(x in t for x in ["staż", "staz", "praktyk", "praktyka", "internship"]):
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
    if any(x in t for x in ["fulltime", "full time", "pełny", "pelny", "pełеn", "pelen", "cały etat", "полный", "повний", "1/1", "etatowa"]):
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


def fetch_url(url: str):
    try:
        r = cr.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "pl-PL,pl;q=0.9",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            impersonate="chrome120",
            timeout=15,
        )
        return r.status_code, r.text
    except Exception as e:
        logger.error(f"fetch_url error for {url}: {e}")
        return 0, ""


# ==================== DATABASE (BATCH ОПТИМИЗАЦИЯ) ====================

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
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200 or not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", attrs={"data-cy": "l-card"}) or soup.select("div.jobs-ad-card")
        if not cards:
            return 0

        jobs_to_save = []

        for card in cards:  # Считываем все карточки со страницы (глубокий парсинг)
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

                # Фильтрация мусорных линков бренда/профилей
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
        url = f"https://www.praca.pl/m-{get_city_slug(city)}_d-1.html?m={city}"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200 or not html:
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.listing__item")
        jobs_to_save = []

        for card in cards[:40]:
            try:
                title_el = card.select_one("a.listing__title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title:
                    continue

                link = title_el.get("href", "").split("#")[0]
                if not link.startswith("http"):
                    link = "https://www.praca.pl" + link

                ext_id = hashlib.md5(f"pracapl_{link}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        continue
                    existing_ids.add(ext_id)

                job_city = city
                loc_el = card.select_one("span.listing__location-name")
                if loc_el and loc_el.contents:
                    job_city = str(loc_el.contents[0]).replace("\xa0", "").strip() or city
                if not city_matches(job_city, city):
                    continue

                dt = card.select_one("div.listing__main-details").get_text(" ", strip=True).lower() if card.select_one("div.listing__main-details") else ""

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
            except Exception:
                pass

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(f"Praca.pl saved={saved} city={city}")
        return saved
    except Exception as e:
        logger.error(f"parse_praca_pl({city}) error: {e}")
        return 0


async def parse_gowork(city: str, existing_ids: set, lock: asyncio.Lock) -> int:
    """
    Полностью переписанный парсер GoWork под твою верстку.
    Умеет обходить Cloudflare и точно достает Договор (Umowa) и График (Etat).
    """
    try:
        # Случайная пауза для маскировки робота
        await asyncio.sleep(random.uniform(2.0, 4.5))

        slug = get_city_slug(city)
        url = f"https://www.gowork.pl/praca/{slug};l" if slug else "https://www.gowork.pl/praca;l"

        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200 or not html:
            logger.warning(f"GoWork returned status {status} for {city}")
            return 0

        # Детекция блокировки Cloudflare
        if "cloudflare" in html.lower() or "just a moment" in html.lower() or "noscript" in html.lower() and "enable javascript" in html.lower():
            logger.warning(f"⚠️ GoWork page for {city} is protected by Cloudflare. Parsing skipped.")
            return 0

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".g-job-item")
        if not cards:
            logger.info(f"GoWork: 0 cards found in HTML for {city}")
            return 0

        jobs_to_save = []

        for card in cards[:35]:
            try:
                # 1. Заголовок и Ссылка (точно по твоей верстке)
                title_el = card.select_one(".g-job-item__offer-title h3 a.g-button")
                if not title_el:
                    continue

                # Достаем текст из тега <span class="g-button__text">
                title_span = title_el.select_one("span.g-button__text")
                title = strip_html(title_span.get_text(strip=True) if title_span else title_el.get_text(strip=True))
                if not title or len(title) < 3:
                    continue

                link = title_el.get("href", "").strip()
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.gowork.pl" + link
                link = link.split("?")[0].split("#")[0]

                ext_id = hashlib.md5(f"gowork_{link}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids:
                        continue
                    existing_ids.add(ext_id)

                # 2. Город
                job_city = city
                loc_el = card.select_one(".g-job-location")
                if loc_el:
                    loc_text = loc_el.get_text(" ", strip=True)
                    if len(loc_text) > 2:
                        job_city = strip_html(loc_text)

                if not city_matches(job_city, city):
                    continue

                # 3. Достаем ВСЕ теги карточки (Umowa, Etat, Salary)
                tag_els = card.select(".g-job-item-content__tag")
                tags = [strip_html(t.get_text(" ", strip=True)) for t in tag_els]

                salary = None
                umowa_val = None
                etat_val = None

                # Парсим каждый тег отдельно по ключевым словам
                for t in tags:
                    t_lower = t.lower()
                    if "zł" in t_lower or "pln" in t_lower or "eur" in t_lower:
                        salary = t
                    elif any(k in t_lower for k in ["umowa", "b2b", "staż", "staz", "dzieło", "zlecenie"]):
                        umowa_val = t
                    elif any(k in t_lower for k in ["etat", "part", "full", "niepełny", "pełny"]):
                        etat_val = t

                # Если теги пустые — используем заголовок как запасной вариант
                umowa_key = normalize_umowa(umowa_val) if umowa_val else normalize_umowa(title)
                etat_key = normalize_etat(etat_val, salary) if etat_val else normalize_etat(title, salary)

                jobs_to_save.append({
                    "external_id": ext_id,
                    "title": title,
                    "city": job_city,
                    "salary": salary,
                    "url": link,
                    "source": "GoWork.pl",
                    "umowa": umowa_key,
                    "etat": etat_key
                })
            except Exception as e:
                logger.debug(f"GoWork item parse error: {e}")

        saved = await db_insert_jobs_batch(jobs_to_save)
        logger.info(f"GoWork saved={saved} city={city}")
        return saved
    except Exception as e:
        logger.error(f"parse_gowork({city}) error: {e}")
        return 0


# ==================== ДИСПЕТЧЕР И MAIN ====================

async def scrape_city_task(city: str, existing_ids: set, semaphore: asyncio.Semaphore, lock: asyncio.Lock) -> int:
    async with semaphore:
        results = await asyncio.gather(
            parse_olx(city, existing_ids, lock),
            parse_praca_pl(city, existing_ids, lock),
            parse_gowork(city, existing_ids, lock)
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
