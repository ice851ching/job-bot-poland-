import os
import sys
import asyncio
import hashlib
import logging
import json
import re
import argparse
import random
from datetime import datetime, timezone, timedelta
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
    "warszawa": "warszawa",
    "kraków": "krakow", "krakow": "krakow",
    "wrocław": "wroclaw", "wroclaw": "wroclaw",
    "poznań": "poznan", "poznan": "poznan",
    "gdańsk": "gdansk", "gdansk": "gdansk",
    "łódź": "lodz", "lodz": "lodz",
    "katowice": "katowice", "lublin": "lublin",
    "toruń": "torun", "torun": "torun",
    "szczecin": "szczecin", "bydgoszcz": "bydgoszcz",
    "białystok": "bialystok", "bialystok": "bialystok",
    "gdynia": "gdynia",
    "częstochowa": "czestochowa",
    "sosnowiec": "sosnowiec",
    "rzeszów": "rzeszow", "rzeszow": "rzeszow",
    "kielce": "kielce", "gliwice": "gliwice",
    "zabrze": "zabrze", "olsztyn": "olsztyn", "opole": "opole",
    "zielona góra": "zielona-gora", "zielona gora": "zielona-gora",
    "radom": "radom", "tychy": "tychy",
    "tarnów": "tarnow", "tarnow": "tarnow",
}


def is_night_time() -> bool:
    """Определяет, ночь ли сейчас в Польше (с 23:00 до 08:00)"""
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset_hours = 2 if 3 < month < 11 else 1
    local_time = now_utc + timedelta(hours=offset_hours)
    hour = local_time.hour
    return hour >= 23 or hour < 8


def get_city_slug(city: str) -> str:
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


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = re.sub(r"\.css-[a-z0-9]+\{[^}]*\}", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ==================== УМНАЯ НОРМАЛИЗАЦИЯ И РАСПОЗНАВАНИЕ ====================

def normalize_umowa(text):
    if not text:
        return None
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip().replace("_", " ").replace("-", " ")
    
    # Umowa zlecenie
    if any(x in t for x in ["zlecenie", "zlecenia", "zlece", "mandate contract", "доручення", "договор злецения"]):
        return "umowa_zlecenie"
    if re.search(r'\b(uz)\b', t):
        return "umowa_zlecenie"

    # Umowa o pracę
    if any(x in t for x in ["o pracę", "o prace", "praca etatowa", "employment contract", "трудовой", "трудовий"]):
        return "umowa_o_prace"
    if re.search(r'\b(uop)\b', t):
        return "umowa_o_prace"

    # B2B
    if any(x in t for x in ["b2b", "selfemployment", "self-employment", "kontrakt b2b", "kontrakt gospodarczy"]):
        return "b2b"

    # Umowa o dzieło
    if any(x in t for x in ["dzieło", "dzielo"]):
        return "umowa_o_dzielo"
    if re.search(r'\b(uod)\b', t):
        return "umowa_o_dzielo"

    # Staż / Praktyki
    if any(x in t for x in ["staż", "staz", "praktyk", "praktyka", "internship"]):
        return "staz"

    return None


def normalize_etat(text, salary_text=None):
    if not text:
        text = ""
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip()

    # Неполный день / Part-time
    if any(x in t for x in [
        "parttime", "part time", "niepełny", "niepelny", "неполный", "неповний",
        "1/2", "3/4", "1/4", "2/3", "pół etatu", "pol etatu", "czesc etatu", "część etatu",
        "dodatkowa", "dorywcza", "student", "студент"
    ]):
        return "part"

    # Полный день / Full-time
    if any(x in t for x in [
        "fulltime", "full time", "pełny", "pelny", "pełen", "pelen", "cały etat", "caly etat",
        "полный", "повний", "1/1", "etatowa"
    ]):
        return "full"

    # Умный Fallback по зарплате
    if salary_text:
        s = str(salary_text).lower()
        if any(x in s for x in ["mies", "m-c", "mc", "/ m", "zł/mies"]):
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
    r = cr.get(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "pl-PL,pl;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        },
        impersonate="chrome120",
        timeout=15,
    )
    return r.status_code, r.text


# ==================== DATABASE ====================

def get_all_existing_ids() -> set:
    try:
        existing = set()
        page_size = 1000
        offset = 0
        while True:
            r = supabase.table("jobs").select("external_id").range(offset, offset + page_size - 1).execute()
            if not r.data:
                break
            for row in r.data:
                if row.get("external_id"):
                    existing.add(row["external_id"])
            if len(r.data) < page_size:
                break
            offset += page_size
        logger.info(f"📦 Loaded {len(existing)} existing job IDs from database.")
        return existing
    except Exception as e:
        logger.error(f"get_all_existing_ids error: {e}")
        return set()


def db_insert_job(ext_id, title, city, salary, url, source,
                  umowa=None, etat=None):
    try:
        r = supabase.table("jobs").upsert(
            {
                "external_id": ext_id,
                "title": title,
                "city": city,
                "salary": salary,
                "url": url,
                "source": source,
                "umowa": umowa,
                "etat": etat,
            },
            on_conflict="external_id",
            ignore_duplicates=True
        ).execute()
        if r.data:
            return r.data[0]["id"]
        return True
    except Exception as e:
        logger.error(f"db_insert_job: {e}")
    return None


def cleanup_old_jobs():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old = supabase.table("jobs").select("id").lt(
            "created_at", cutoff
        ).execute()
        if old.data:
            old_ids = [j["id"] for j in old.data]
            logger.info(f"🗑 Cleaning {len(old_ids)} old jobs")
            for i in range(0, len(old_ids), 100):
                batch = old_ids[i:i + 100]
                try:
                    supabase.table("sent_jobs").delete().in_(
                        "job_id", batch
                    ).execute()
                except Exception as e:
                    logger.error(f"cleanup batch: {e}")
            supabase.table("jobs").delete().lt(
                "created_at", cutoff
            ).execute()
            logger.info(f"✅ Cleaned {len(old_ids)} old jobs")
        else:
            logger.info("🗑 Nothing to clean")
    except Exception as e:
        logger.error(f"cleanup_old_jobs: {e}")


def get_active_cities_from_db() -> list:
    try:
        r = supabase.table("user_filters").select("city").eq("is_paused", False).execute()
        if not r.data:
            return []
        cities = set()
        for row in r.data:
            c = row.get("city")
            if c:
                if c == "all":
                    return MAIN_SCAN_CITIES
                cities.add(c)
        return list(cities)
    except Exception as e:
        logger.error(f"get_active_cities_from_db error: {e}")
        return []


# ==================== OLX (УЛЬТРА-СОВРЕМЕННЫЙ ПАРСЕР КАРТОЧЕК) ====================

def extract_all_text_from_node(obj):
    """Рекурсивный сбор абсолютно всех текстовых значений из вложенных JSON-узлов"""
    texts = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ["label", "name", "value", "key", "title"] and isinstance(v, str):
                texts.append(v)
            else:
                texts.extend(extract_all_text_from_node(v))
    elif isinstance(obj, list):
        for item in obj:
            texts.extend(extract_all_text_from_node(item))
    elif isinstance(obj, str):
        texts.append(obj)
    return texts


def extract_olx_params_json(item: dict, title: str, salary: str):
    """Глубокий разбор массива params без потери сложных и составных типов контрактов"""
    umowa_texts = []
    etat_texts = []
    params = item.get("params", [])
    
    for p in params:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or "").lower()
        name = str(p.get("name") or "").lower()
        
        # Рекурсивно вытягиваем весь текст из узла параметра
        node_texts = extract_all_text_from_node(p)
        joined_text = " ".join(node_texts)
        
        if "contract" in key or "umow" in key or "umow" in name:
            umowa_texts.append(joined_text)
        if "hours" in key or "wymiar" in key or "etat" in name or "time" in key:
            etat_texts.append(joined_text)

    # Запускаем нормализацию сжатых строк
    umowa_key = normalize_umowa(" ".join(umowa_texts))
    etat_key = normalize_etat(" ".join(etat_texts), salary)

    # Fallback по заголовку
    if not umowa_key:
        umowa_key = normalize_umowa(title)
    if not etat_key:
        etat_key = normalize_etat(title, salary)
            
    return umowa_key, etat_key


async def parse_olx(city: str, existing_ids: set):
    saved = 0
    try:
        slug = get_city_slug(city)
        base = f"https://www.olx.pl/praca/{slug}/" if slug else "https://www.olx.pl/praca/"
        url = f"{base}?search%5Border%5D=created_at:desc"

        status, html = await asyncio.to_thread(fetch_url, url)
        logger.info(f"OLX status={status} city={city}")
        if status != 200:
            return saved

        # Напрямую парсим JSON-состояние страницы
        data = None
        m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*"(.*?)";\s*(?:window|</script>)', html, re.DOTALL)
        if m:
            raw = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(m.group(1).encode().decode("unicode_escape"))
                except Exception:
                    pass

        if not data:
            logger.warning(f"OLX __PRERENDERED_STATE__ not found for {city}")
            return saved

        listing = data.get("listing", {}).get("listing", data.get("listing", {}))
        ads = listing.get("ads", [])
        if not ads:
            for k in ["adverts", "data", "items"]:
                v = listing.get(k)
                if v and isinstance(v, list):
                    ads = v
                    break

        for item in ads[:40]:
            try:
                title = strip_html(item.get("title") or "")
                if not title:
                    continue

                link = item.get("url") or ""
                if not link:
                    sid, iid = item.get("slug", ""), item.get("id", "")
                    if sid and iid:
                        link = f"https://www.olx.pl/oferta/{sid}-ID{iid}.html"
                if not link or not link.startswith("http"):
                    if link and not link.startswith("http"):
                        link = "https://www.olx.pl" + link
                    else:
                        continue

                ext_id = hashlib.md5(f"olx_{link}".encode()).hexdigest()
                if ext_id in existing_ids:
                    continue

                salary = None
                sal = item.get("salary")
                if sal:
                    salary = sal.get("displayValue") if isinstance(sal, dict) else str(sal)
                if not salary:
                    price = item.get("price", {})
                    if isinstance(price, dict):
                        salary = price.get("displayValue")

                location = item.get("location", {})
                job_city = ""
                if isinstance(location, dict):
                    cd = location.get("city", {})
                    job_city = cd.get("name", "") if isinstance(cd, dict) else (cd if isinstance(cd, str) else location.get("cityName", ""))

                if city and job_city and not city_matches(job_city, city):
                    continue

                umowa_key, etat_key = extract_olx_params_json(item, title, salary)

                job_id = db_insert_job(
                    ext_id,
                    title,
                    strip_html(job_city or city),
                    strip_html(salary) if salary else None,
                    link,
                    "OLX",
                    umowa=umowa_key,
                    etat=etat_key,
                )
                if job_id:
                    saved += 1
                    existing_ids.add(ext_id)
            except Exception as e:
                logger.error(f"OLX JSON item error: {e}")

        logger.info(f"OLX JSON saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_olx({city}) general error: {e}")

    return saved


# ==================== PRACA.PL ====================

async def parse_praca_pl(city: str, existing_ids: set):
    saved = 0
    try:
        url = f"https://www.praca.pl/m-{get_city_slug(city)}_d-1.html?m={city}"
        status, html = await asyncio.to_thread(fetch_url, url)
        logger.info(f"Praca.pl status={status} city={city}")
        if status != 200:
            return saved

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.listing__item")
        logger.info(f"Praca.pl cards={len(cards)} city={city}")

        for card in cards[:40]:
            try:
                title_el = card.select_one("a.listing__title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title:
                    continue

                link = title_el.get("href", "").split("#")[0]
                if not link or not link.startswith("http"):
                    if link and not link.startswith("http"):
                        link = "https://www.praca.pl" + link
                    else:
                        continue

                ext_id = hashlib.md5(f"pracapl_{link}".encode()).hexdigest()
                if ext_id in existing_ids:
                    continue

                job_city = city
                loc_el = card.select_one("span.listing__location-name")
                if loc_el:
                    for child in loc_el.children:
                        if hasattr(child, '__class__') and child.__class__.__name__ == 'NavigableString':
                            text = str(child).replace("\xa0", "").strip()
                            if text:
                                job_city = text
                                break

                if job_city and not city_matches(job_city, city):
                    continue

                details_el = card.select_one("div.listing__main-details")
                dt = details_el.get_text(" ", strip=True).lower() if details_el else ""

                umowa_key = normalize_umowa(dt) or normalize_umowa(title)
                etat_key = normalize_etat(dt) or normalize_etat(title)

                job_id = db_insert_job(
                    ext_id, title, job_city, None, link,
                    "Praca.pl", umowa=umowa_key, etat=etat_key
                )
                if job_id:
                    saved += 1
                    existing_ids.add(ext_id)

            except Exception as e:
                logger.error(f"Praca.pl item: {e}")

        logger.info(f"Praca.pl saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_praca_pl({city}): {e}")
    return saved


# ==================== GOWORK ====================

async def parse_gowork(city: str, existing_ids: set):
    saved = 0
    try:
        slug = get_city_slug(city)
        url = f"https://www.gowork.pl/praca/{slug};l"
        status, html = await asyncio.to_thread(fetch_url, url)
        logger.info(f"GoWork status={status} city={city}")
        if status != 200:
            return saved

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".g-job-item")
        logger.info(f"GoWork cards={len(cards)} city={city}")

        for card in cards[:30]:
            try:
                title_el = card.select_one(".g-job-item__offer-title a")
                if not title_el:
                    continue
                title = strip_html(title_el.get_text(strip=True))
                if not title:
                    continue

                link = title_el.get("href", "")
                if not link or not link.startswith("http"):
                    if link and not link.startswith("http"):
                        link = "https://www.gowork.pl" + link
                    else:
                        continue

                ext_id = hashlib.md5(f"gowork_{link}".encode()).hexdigest()
                if ext_id in existing_ids:
                    continue

                job_city = city
                loc_el = card.select_one(".g-job-location")
                if loc_el:
                    for sp in loc_el.select("span"):
                        text = sp.get_text(strip=True)
                        if text and len(text) > 2:
                            job_city = strip_html(text)
                            break

                if job_city and not city_matches(job_city, city):
                    continue

                salary = None
                tags_text = []

                for tag in card.select(".g-job-item-content__tag"):
                    for sp in tag.select("span"):
                        text = sp.get_text(strip=True)
                        if not text:
                            continue
                        if "zł" in text.lower() or "pln" in text.lower():
                            salary = strip_html(text)
                        else:
                            tags_text.append(text)

                combined_tags = " ".join(tags_text)
                umowa_key = normalize_umowa(combined_tags) or normalize_umowa(title)
                etat_key = normalize_etat(combined_tags, salary) or normalize_etat(title, salary)

                job_id = db_insert_job(
                    ext_id, title, job_city, salary, link,
                    "GoWork.pl", umowa=umowa_key, etat=etat_key
                )
                if job_id:
                    saved += 1
                    existing_ids.add(ext_id)

            except Exception as e:
                logger.error(f"GoWork item: {e}")

        logger.info(f"GoWork saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_gowork({city}): {e}")
    return saved


# ==================== АСИНХРОННЫЙ ДИСПЕТЧЕР ====================

async def scrape_city_task(city: str, existing_ids: set, semaphore: asyncio.Semaphore) -> int:
    async with semaphore:
        results = await asyncio.gather(
            parse_olx(city, existing_ids),
            parse_praca_pl(city, existing_ids),
            parse_gowork(city, existing_ids)
        )
        return sum(results)


# ==================== MAIN ====================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default=None)
    args = parser.parse_args()

    if not args.city and is_night_time():
        logger.info("🌙 Night sleep mode active. Skipping.")
        sys.exit(0)

    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset_hours = 2 if 3 < month < 11 else 1
    local_time = now_utc + timedelta(hours=offset_hours)
    
    if not args.city and local_time.hour == 8 and local_time.minute < 35:
        cleanup_old_jobs()

    existing_ids = get_all_existing_ids()

    if args.city:
        cities = [args.city]
    else:
        db_cities = get_active_cities_from_db()
        cities = db_cities if db_cities else MAIN_SCAN_CITIES[:5]

    city_sem = asyncio.Semaphore(3)
    tasks = [scrape_city_task(city, existing_ids, city_sem) for city in cities]
    results = await asyncio.gather(*tasks)
    total = sum(results)

    logger.info(f"✅ Done. Total saved across all cities: {total}")


if __name__ == "__main__":
    asyncio.run(main())
