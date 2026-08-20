import os
import sys
import asyncio
import hashlib
import logging
import json
import re
import argparse
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

# Список городов для планового сканирования
MAIN_SCAN_CITIES = [
    "Warszawa", "Kraków", "Wrocław", "Poznań", "Gdańsk",
    "Łódź", "Katowice", "Lublin", "Toruń", "Szczecin",
    "Bydgoszcz", "Gdynia"
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
    "szczecin": "szczecin",
    "bydgoszcz": "bydgoszcz",
    "białystok": "bialystok", "bialystok": "bialystok",
    "gdynia": "gdynia",
}

# ==================== ХЕЛПЕРЫ СВЕЖЕСТИ И ВРЕМЕНИ ====================

def is_night_time() -> bool:
    """
    Определяет, ночь ли сейчас в Польше (с 23:00 до 08:00).
    Учитывает переход на летнее/зимнее время (UTC+1 / UTC+2).
    """
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset_hours = 2 if 3 < month < 11 else 1
    local_time = now_utc + timedelta(hours=offset_hours)
    hour = local_time.hour
    return hour >= 23 or hour < 8


def is_praca_pl_fresh(text: str) -> bool:
    """Проверяет дату публикации на Praca.pl (не старше 2 дней)"""
    t_raw = text.lower().strip()
    if any(x in t_raw for x in ["min", "godz", "dziś", "dzis", "wczoraj", "1 dzień", "2 dni", "1 dzien"]):
        return True
    if "dni" in t_raw:
        match = re.search(r"(\d+)", t_raw)
        if match:
            days = int(match.group(1))
            return days <= 2
    return False


def is_gowork_fresh(text: str) -> bool:
    """Проверяет дату публикации на GoWork.pl (не старше 2 дней)"""
    t_raw = text.lower().replace("publikowano:", "").replace("opublikowano:", "").strip()
    if any(x in t_raw for x in ["dzisiaj", "wczoraj", "godz", "min", "sek"]):
        return True
    
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t_raw)
    if match:
        try:
            day, month, year = map(int, match.groups())
            pub_date = datetime(year, month, day, tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - pub_date).days <= 2
        except:
            pass
            
    if "dni temu" in t_raw:
        match = re.search(r"(\d+)", t_raw)
        if match:
            return int(match.group(1)) <= 2
            
    return False


def get_city_slug(city: str) -> str:
    if not city:
        return ""
    cl = city.lower().strip()
    if cl in CITY_SLUGS:
        return CITY_SLUGS[cl]
    for k, v in {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z', ' ': '-'
    }.items():
        cl = cl.replace(k, v)
    return cl


def city_matches(job_city, filter_city):
    if not filter_city:
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


# ==================== НОРМАЛИЗАЦИЯ ДАННЫХ ====================

def normalize_umowa(umowa):
    if not umowa:
        return None
    if isinstance(umowa, list):
        umowa = " ".join(str(v) for v in umowa)
    u = str(umowa).lower().strip().replace("_", " ").replace("-", " ")
    if "zlecenie" in u:
        return "umowa_zlecenie"
    if "o pracę" in u or "o prace" in u:
        return "umowa_o_prace"
    if "b2b" in u or "selfemployment" in u or "kontrakt b2b" in u:
        return "b2b"
    if "dzieło" in u or "dzielo" in u:
        return "umowa_o_dzielo"
    if "staż" in u or "staz" in u or "praktyk" in u:
        return "staz"
    return None


def normalize_etat(etat):
    if not etat:
        return None
    if isinstance(etat, list):
        etat = " ".join(str(v) for v in etat)
    e = str(etat).lower().strip().replace("_", " ").replace("-", " ")
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


# ==================== DATABASE HELPERS ====================

def job_exists_in_db(ext_id: str) -> bool:
    try:
        r = supabase.table("jobs").select("id").eq("external_id", ext_id).limit(1).execute()
        return bool(r.data)
    except Exception as e:
        logger.error(f"job_exists_in_db error: {e}")
        return False


def db_insert_job(ext_id, title, city, salary, url, source, umowa=None, etat=None):
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
            
        r2 = supabase.table("jobs").select("id").eq("external_id", ext_id).execute()
        return r2.data[0]["id"] if r2.data else None
    except Exception as e:
        logger.error(f"db_insert_job error: {e}")
    return None


def cleanup_old_jobs():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old_jobs = supabase.table("jobs").select("id").lt("created_at", cutoff).execute()
        
        if old_jobs.data:
            old_ids = [j["id"] for j in old_jobs.data]
            logger.info(f"🗑 Cleaning {len(old_ids)} old jobs from database...")
            for i in range(0, len(old_ids), 100):
                batch = old_ids[i:i + 100]
                try:
                    supabase.table("sent_jobs").delete().in_("job_id", batch).execute()
                except Exception as e:
                    logger.error(f"cleanup batch sent_jobs error: {e}")
            supabase.table("jobs").delete().lt("created_at", cutoff).execute()
            logger.info("✅ Database cleanup successfully finished")
        else:
            logger.info("🗑 Database is clean, nothing to delete")
    except Exception as e:
        logger.error(f"cleanup_old_jobs error: {e}")


# ==================== PARSING ENGINE ====================

def fetch_url(url: str):
    from curl_cffi import requests as cr
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
        timeout=30
    )
    return r.status_code, r.text


def extract_olx_label(html, label):
    pattern = rf">{re.escape(label)}</p>.*?<p[^>]*>(.*?)</p>"
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1)
    raw = re.sub(r'<span[^>]*>', '', raw)
    raw = re.sub(r'</span>', '', raw)
    result = strip_html(raw)
    if "}" in result:
        result = result.split("}")[-1].strip()
    return result


async def fetch_olx_details(url):
    try:
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200:
            return {}
        return {
            "city": extract_olx_label(html, "Lokalizacja"),
            "etat": extract_olx_label(html, "Wymiar pracy"),
            "umowa": extract_olx_label(html, "Typ umowy"),
        }
    except Exception as e:
        logger.error(f"fetch_olx_details error: {e}")
        return {}


async def parse_olx(city="all"):
    jobs = []
    try:
        slug = get_city_slug(city)
        base = f"https://www.olx.pl/praca/{slug}/" if slug else "https://www.olx.pl/praca/"
        url = f"{base}?search%5Border%5D=created_at:desc"

        status, html = await asyncio.to_thread(fetch_url, url)
        logger.info(f"OLX status: {status} for city: {city}")
        if status != 200:
            return len(jobs)

        data = None
        m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*"(.*?)";', html, re.DOTALL)
        if m:
            raw = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(m.group(1).encode().decode('unicode_escape'))
                except Exception:
                    pass

        if not data:
            logger.warning(f"OLX no parseable window data found for {city}")
            return len(jobs)

        listing = data.get("listing", {}).get("listing", data.get("listing", {}))
        ads = listing.get("ads", [])
        if not ads:
            for k in ["adverts", "data", "items"]:
                v = listing.get(k)
                if v and isinstance(v, list):
                    ads = v
                    break

        logger.info(f"OLX listings found: {len(ads)}")

        # Фильтр жесткой свежести OLX (не старше 2 дней)
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=2)
        fresh_ads = []
        for ad in ads:
            created = ad.get("createdTime") or ad.get("lastRefreshTime") or ad.get("pushupTime")
            if created:
                try:
                    ad_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if ad_time >= cutoff_time:
                        fresh_ads.append(ad)
                    continue
                except:
                    pass
            fresh_ads.append(ad)

        logger.info(f"OLX fresh ads (<= 2 days): {len(fresh_ads)}/{len(ads)}")

        raw_jobs = []
        for item in fresh_ads[:30]:
            try:
                title = strip_html(item.get("title") or "")
                if not title:
                    continue
                link = item.get("url") or ""
                if not link:
                    sid, iid = item.get("slug", ""), item.get("id", "")
                    if sid and iid:
                        link = f"https://www.olx.pl/oferta/{sid}-ID{iid}.html"
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.olx.pl" + link

                ext_id = hashlib.md5(link.encode()).hexdigest()
                
                # Ленивый парсинг: если уже в БД — пропускаем шаг детального перехода
                if job_exists_in_db(ext_id):
                    continue

                location = item.get("location", {})
                job_city = ""
                if isinstance(location, dict):
                    cd = location.get("city", {})
                    job_city = cd.get("name", "") if isinstance(cd, dict) else (cd if isinstance(cd, str) else location.get("cityName", ""))

                if city != "all" and job_city and not city_matches(job_city, city):
                    continue

                raw_jobs.append({
                    "ext_id": ext_id,
                    "title": title,
                    "city": strip_html(job_city or city),
                    "salary": strip_html(item.get("salary", {}).get("displayValue") if isinstance(item.get("salary"), dict) else item.get("price", {}).get("displayValue")),
                    "url": link
                })
            except Exception as e:
                logger.error(f"OLX list item processing error: {e}")

        # Дозапрос деталей только для НОВЫХ вакансий
        logger.info(f"OLX fetching details for {len(raw_jobs)} new vacancies...")
        for job in raw_jobs:
            try:
                details = await fetch_olx_details(job["url"])
                dc = details.get("city")
                de = details.get("etat")
                du = details.get("umowa")

                if dc:
                    job["city"] = dc
                if city != "all" and not city_matches(job["city"], city):
                    continue

                umowa_key = normalize_umowa(du) if du else None
                etat_key = normalize_etat(de) if de else None

                db_insert_job(
                    job["ext_id"],
                    job["title"],
                    job["city"],
                    job["salary"],
                    job["url"],
                    "OLX",
                    umowa=umowa_key,
                    etat=etat_key
                )
                jobs.append(job)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"OLX detail processor error: {e}")

        logger.info(f"OLX total successfully saved: {len(jobs)}")
    except Exception as e:
        logger.error(f"parse_olx general error: {e}")
    return len(jobs)


async def parse_praca_pl(city="all"):
    from bs4 import BeautifulSoup
    saved = 0
    try:
        url = "https://www.praca.pl/oferty-pracy" if city == "all" else f"https://www.praca.pl/m-{get_city_slug(city)}_d-1.html?m={city}"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200:
            return 0
            
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.listing__item")
        
        for card in cards[:30]:
            try:
                title_el = card.select_one("a.listing__title")
                if not title_el:
                    continue
                link = title_el.get("href", "").split("#")[0]
                if not link.startswith("http"):
                    link = "https://www.praca.pl" + link
                
                ext_id = hashlib.md5(link.encode()).hexdigest()
                if job_exists_in_db(ext_id):
                    continue

                # Жесткий фильтр даты Praca.pl
                date_el = card.select_one(".listing__secondary-details span")
                date_text = date_el.text if date_el else ""
                if date_text and not is_praca_pl_fresh(date_text):
                    continue

                dt = card.select_one("div.listing__main-details").get_text(" ", strip=True).lower() if card.select_one("div.listing__main-details") else ""
                job_city = city
                
                job_id = db_insert_job(
                    ext_id,
                    strip_html(title_el.text),
                    job_city,
                    None,
                    link,
                    "Praca.pl",
                    umowa=normalize_umowa(dt),
                    etat=normalize_etat(dt)
                )
                if job_id:
                    saved += 1
            except Exception as e:
                logger.error(f"Praca.pl card error: {e}")
    except Exception as e:
        logger.error(f"parse_praca_pl general error: {e}")
    return saved


async def parse_gowork(city="all"):
    from bs4 import BeautifulSoup
    saved = 0
    try:
        url = "https://www.gowork.pl/praca" if city == "all" else f"https://www.gowork.pl/praca/{get_city_slug(city)};l"
        status, html = await asyncio.to_thread(fetch_url, url)
        if status != 200:
            return 0
            
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".g-job-item")
        
        for card in cards[:30]:
            try:
                title_el = card.select_one(".g-job-item__offer-title a")
                if not title_el:
                    continue
                link = "https://www.gowork.pl" + title_el.get('href', "")
                
                ext_id = hashlib.md5(link.encode()).hexdigest()
                if job_exists_in_db(ext_id):
                    continue

                # Жесткий фильтр даты GoWork
                date_el = card.select_one(".g-job-item__apply-button span:last-child")
                date_text = date_el.text if date_el else ""
                if date_text and not is_gowork_fresh(date_text):
                    continue

                tags = [t.get_text(strip=True).lower() for t in card.select(".g-job-item-content__tag span")]
                umowa = next((t for t in tags if "umowa" in t or "b2b" in t), None)
                etat = next((t for t in tags if "etat" in t), None)
                
                job_id = db_insert_job(
                    ext_id,
                    strip_html(title_el.text),
                    city,
                    None,
                    link,
                    "GoWork.pl",
                    umowa=normalize_umowa(umowa),
                    etat=normalize_etat(etat)
                )
                if job_id:
                    saved += 1
            except Exception as e:
                logger.error(f"GoWork card error: {e}")
    except Exception as e:
        logger.error(f"parse_gowork general error: {e}")
    return saved


# ==================== PIPELINE EXECUTION ====================

async def main():
    # Проверка на ночной режим (с 23:00 до 08:00 по Варшаве)
    if is_night_time():
        logger.info("🌙 Night sleep mode active (23:00 - 08:00). Shutting down scraper cleanly.")
        sys.exit(0)

    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default=None)
    args = parser.parse_args()

    cleanup_old_jobs()

    cities = [args.city] if args.city else MAIN_SCAN_CITIES
    logger.info(f"🔁 Starting scrape job for: {cities}")

    total = 0
    for city in cities:
        try:
            n1 = await parse_olx(city)
            await asyncio.sleep(2)
            n2 = await parse_praca_pl(city)
            await asyncio.sleep(2)
            n3 = await parse_gowork(city)
            await asyncio.sleep(2)
            total += n1 + n2 + n3
            logger.info(f"Finished {city}. New entries: OLX={n1}, Praca.pl={n2}, GoWork={n3}")
        except Exception as e:
            logger.error(f"Failed to scrape city {city}: {e}")

    logger.info(f"✅ Scraping workflow successfully completed. Total saved: {total}")


if __name__ == "__main__":
    asyncio.run(main())
