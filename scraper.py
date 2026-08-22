import os
import sys
import asyncio
import hashlib
import logging
import re
import argparse
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
    "szczecin": "szczecin", "bydgoszcz": "bydgoszcz", "białystok": "bialystok", "bialystok": "bialystok",
    "gdynia": "gdynia", "częstochowa": "czestochowa", "czestochowa": "czestochowa",
    "sosnowiec": "sosnowiec", "rzeszów": "rzeszow", "rzeszow": "rzeszow",
    "kielce": "kielce", "gliwice": "gliwice", "zabrze": "zabrze",
    "olsztyn": "olsztyn", "opole": "opole", "zielona góra": "zielona-gora", "zielona gora": "zielona-gora",
    "radom": "radom", "tychy": "tychy", "tarnów": "tarnow", "tarnow": "tarnow",
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
    if any(x in t for x in ["zlecenie", "zlecenia", "zlece"]): return "umowa_zlecenie"
    if re.search(r'\b(uz)\b', t): return "umowa_zlecenie"
    if any(x in t for x in ["o pracę", "o prace", "praca etatowa"]): return "umowa_o_prace"
    if re.search(r'\b(uop)\b', t): return "umowa_o_prace"
    if any(x in t for x in ["b2b", "selfemployment", "kontrakt b2b", "kontrakt gospodarczy"]): return "b2b"
    if any(x in t for x in ["dzieło", "dzielo"]): return "umowa_o_dzielo"
    if re.search(r'\b(uod)\b', t): return "umowa_o_dzielo"
    if any(x in t for x in ["staż", "staz", "praktyk", "praktyka", "internship"]): return "staz"
    return None


def normalize_etat(text, salary_text=None):
    if not text:
        text = ""
    if isinstance(text, (list, tuple, set)):
        text = " ".join(str(v) for v in text)
    t = str(text).lower().strip()
    if any(x in t for x in ["parttime", "part time", "niepełny", "niepelny", "1/2", "3/4", "1/4", "pół etatu", "pol etatu", "czesc etatu", "część etatu", "dodatkowa", "dorywcza", "student"]):
        return "part"
    if any(x in t for x in ["fulltime", "full time", "pełny", "pelny", "pełen", "pelen", "cały etat", "caly etat", "1/1", "etatowa"]):
        return "full"
    if salary_text and any(x in str(salary_text).lower() for x in ["mies", "m-c", "mc", "/ m", "zł/mies"]):
        return "full"
    return None


def city_matches(job_city, filter_city):
    if not filter_city: return True
    if not job_city: return False
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
            },
            impersonate="chrome120",
            timeout=15,
        )
        return r.status_code, r.text
    except Exception as e:
        logger.error(f"fetch_url error: {e}")
        return 0, ""


# ==================== DATABASE ====================

def get_all_existing_ids() -> set:
    try:
        existing = set()
        page_size = 1000
        offset = 0
        while True:
            r = supabase.table("jobs").select("external_id").range(offset, offset + page_size - 1).execute()
            if not r.data: break
            for row in r.data:
                if row.get("external_id"): existing.add(row["external_id"])
            if len(r.data) < page_size: break
            offset += page_size
        logger.info(f"📦 Loaded {len(existing)} existing IDs.")
        return existing
    except Exception as e:
        logger.error(f"get_all_existing_ids: {e}")
        return set()


def db_insert_job_sync(ext_id, title, city, salary, url, source, umowa=None, etat=None):
    try:
        r = supabase.table("jobs").upsert(
            {"external_id": ext_id, "title": title, "city": city, "salary": salary,
             "url": url, "source": source, "umowa": umowa, "etat": etat},
            on_conflict="external_id", ignore_duplicates=True
        ).execute()
        return r.data[0]["id"] if r.data else True
    except Exception as e:
        logger.error(f"db_insert: {e}")
        return None


async def db_insert_job(ext_id, title, city, salary, url, source, umowa=None, etat=None):
    return await asyncio.to_thread(db_insert_job_sync, ext_id, title, city, salary, url, source, umowa, etat)


def cleanup_old_jobs():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old = supabase.table("jobs").select("id").lt("created_at", cutoff).execute()
        if old.data:
            old_ids = [j["id"] for j in old.data]
            logger.info(f"🗑 Cleaning {len(old_ids)} old jobs")
            for i in range(0, len(old_ids), 100):
                supabase.table("sent_jobs").delete().in_("job_id", old_ids[i:i+100]).execute()
            supabase.table("jobs").delete().lt("created_at", cutoff).execute()
            logger.info("✅ Cleaned")
    except Exception as e:
        logger.error(f"cleanup: {e}")


def get_active_cities_from_db() -> list:
    try:
        r = supabase.table("user_filters").select("city").eq("is_paused", False).execute()
        if not r.data: return []
        cities = {row["city"] for row in r.data if row.get("city")}
        if "all" in cities: return MAIN_SCAN_CITIES
        return list(cities)
    except Exception as e:
        logger.error(f"get_active_cities: {e}")
        return []


# ==================== OLX (ПРОСТОЙ HTML-ПАРСЕР, КАК ДВА ДРУГИХ САЙТА) ====================

async def parse_olx(city: str, existing_ids: set, lock: asyncio.Lock):
    saved = 0
    try:
        slug = get_city_slug(city)
        url = f"https://www.olx.pl/praca/{slug}/?search%5Border%5D=created_at:desc" if slug else "https://www.olx.pl/praca/?search%5Border%5D=created_at:desc"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200: return saved

        soup = BeautifulSoup(html, "html.parser")
        
        # Ищем карточки. OLX использует data-cy="l-card" для E2E-тестов — это стабильный атрибут.
        cards = soup.find_all("div", attrs={"data-cy": "l-card"})
        if not cards:
            cards = soup.select("div.jobs-ad-card")
        if not cards:
            return saved

        for card in cards[:40]:
            try:
                # Заголовок и ссылка
                title_tag = card.find("h4") or card.select_one("a[data-testid='card-title-link']")
                link_tag = card.find("a", href=True)
                if not title_tag or not link_tag: continue

                title = strip_html(title_tag.get_text(strip=True))
                if not title: continue

                link = link_tag["href"]
                if not link.startswith("http"):
                    link = "https://www.olx.pl" + link
                link = link.split("?")[0].split("#")[0]

                ext_id = hashlib.md5(f"olx_{link}".encode()).hexdigest()

                async with lock:
                    if ext_id in existing_ids: continue
                    existing_ids.add(ext_id)

                # Берём ВЕСЬ текст карточки одной строкой
                card_text = card.get_text(" ", strip=True)

                # Зарплата
                salary = None
                for p in card.find_all("p"):
                    pt = p.get_text(strip=True).lower()
                    if "zł" in pt or "pln" in pt or "eur" in pt:
                        salary = strip_html(p.get_text())
                        break

                # Умова и Этат — просто ищем в тексте карточки, как на Praca.pl и GoWork
                umowa_key = normalize_umowa(card_text) or normalize_umowa(title)
                etat_key = normalize_etat(card_text, salary) or normalize_etat(title, salary)

                job_id = await db_insert_job(
                    ext_id, title, city, salary, link, "OLX",
                    umowa=umowa_key, etat=etat_key
                )
                if job_id: saved += 1

            except Exception as e:
                logger.error(f"OLX card error: {e}")

        logger.info(f"OLX saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_olx({city}): {e}")
    return saved


# ==================== PRACA.PL ====================

async def parse_praca_pl(city: str, existing_ids: set, lock: asyncio.Lock):
    saved = 0
    try:
        url = f"https://www.praca.pl/m-{get_city_slug(city)}_d-1.html?m={city}"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200: return saved

        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select("li.listing__item")[:40]:
            try:
                title_el = card.select_one("a.listing__title")
                if not title_el: continue
                title = title_el.get_text(strip=True)
                if not title: continue

                link = title_el.get("href", "").split("#")[0]
                if not link: continue
                if not link.startswith("http"): link = "https://www.praca.pl" + link

                ext_id = hashlib.md5(f"pracapl_{link}".encode()).hexdigest()
                async with lock:
                    if ext_id in existing_ids: continue
                    existing_ids.add(ext_id)

                job_city = city
                loc_el = card.select_one("span.listing__location-name")
                if loc_el and loc_el.contents:
                    job_city = str(loc_el.contents[0]).replace("\xa0", "").strip() or city
                if not city_matches(job_city, city): continue

                dt = card.select_one("div.listing__main-details").get_text(" ", strip=True).lower() if card.select_one("div.listing__main-details") else ""

                job_id = await db_insert_job(
                    ext_id, title, job_city, None, link, "Praca.pl",
                    umowa=normalize_umowa(dt) or normalize_umowa(title),
                    etat=normalize_etat(dt) or normalize_etat(title)
                )
                if job_id: saved += 1
            except Exception:
                pass
        logger.info(f"Praca.pl saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_praca_pl({city}): {e}")
    return saved


# ==================== GOWORK ====================

async def parse_gowork(city: str, existing_ids: set, lock: asyncio.Lock):
    saved = 0
    try:
        url = f"https://www.gowork.pl/praca/{get_city_slug(city)};l"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200: return saved

        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select(".g-job-item")[:30]:
            try:
                title_el = card.select_one(".g-job-item__offer-title a")
                if not title_el: continue
                title = strip_html(title_el.get_text(strip=True))
                if not title: continue

                link = title_el.get("href", "")
                if not link: continue
                if not link.startswith("http"): link = "https://www.gowork.pl" + link

                ext_id = hashlib.md5(f"gowork_{link}".encode()).hexdigest()
                async with lock:
                    if ext_id in existing_ids: continue
                    existing_ids.add(ext_id)

                job_city = city
                loc_el = card.select_one(".g-job-location span")
                if loc_el:
                    t = loc_el.get_text(strip=True)
                    if len(t) > 2: job_city = strip_html(t)
                if not city_matches(job_city, city): continue

                tags = [sp.get_text(strip=True) for tag in card.select(".g-job-item-content__tag") for sp in tag.select("span")]
                salary = next((strip_html(t) for t in tags if "zł" in t.lower() or "pln" in t.lower()), None)
                combined = " ".join(tags)

                job_id = await db_insert_job(
                    ext_id, title, job_city, salary, link, "GoWork.pl",
                    umowa=normalize_umowa(combined) or normalize_umowa(title),
                    etat=normalize_etat(combined, salary) or normalize_etat(title, salary)
                )
                if job_id: saved += 1
            except Exception:
                pass
        logger.info(f"GoWork saved={saved} city={city}")
    except Exception as e:
        logger.error(f"parse_gowork({city}): {e}")
    return saved


# ==================== ДИСПЕТЧЕР ====================

async def scrape_city_task(city, existing_ids, semaphore, lock):
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
        logger.info("🌙 Night mode. Skipping.")
        sys.exit(0)

    try:
        lt = datetime.now(ZoneInfo("Europe/Warsaw"))
        if not args.city and lt.hour == 8 and lt.minute < 35:
            cleanup_old_jobs()
    except Exception:
        pass

    existing_ids = get_all_existing_ids()
    cities = [args.city] if args.city else (get_active_cities_from_db() or MAIN_SCAN_CITIES[:5])

    sem = asyncio.Semaphore(3)
    lock = asyncio.Lock()
    tasks = [scrape_city_task(c, existing_ids, sem, lock) for c in cities]
    results = await asyncio.gather(*tasks)

    logger.info(f"✅ Done. Total saved: {sum(results)}")


if __name__ == "__main__":
    asyncio.run(main())
