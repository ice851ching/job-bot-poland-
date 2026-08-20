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
    "częstochowa": "czestochowa", "czestochowa": "czestochowa",
    "sosnowiec": "sosnowiec",
    "rzeszów": "rzeszow", "rzeszow": "rzeszow",
    "kielce": "kielce", "gliwice": "gliwice",
    "zabrze": "zabrze", "olsztyn": "olsztyn", "opole": "opole",
    "zielona góra": "zielona-gora", "zielona gora": "zielona-gora",
    "radom": "radom", "tychy": "tychy",
    "tarnów": "tarnow", "tarnow": "tarnow",
}


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
        timeout=30,
    )
    return r.status_code, r.text


def job_exists_in_db(ext_id: str) -> bool:
    try:
        r = supabase.table("jobs").select("id").eq(
            "external_id", ext_id
        ).limit(1).execute()
        return bool(r.data)
    except Exception as e:
        logger.error(f"job_exists_in_db: {e}")
        return False


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
        r2 = supabase.table("jobs").select("id").eq(
            "external_id", ext_id
        ).execute()
        return r2.data[0]["id"] if r2.data else None
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


# ==================== OLX ====================

def extract_olx_label(html, label):
    pattern = rf">{re.escape(label)}</p>.*?<p[^>]*>(.*?)</p>"
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    raw = re.sub(r'<span[^>]*>', '', match.group(1))
    raw = re.sub(r'</span>', '', raw)
    result = strip_html(raw)
    if "}" in result:
        result = result.split("}")[-1].strip()
    return result if result else None


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
        logger.error(f"fetch_olx_details: {e}")
        return {}


async def parse_olx(city: str):
    saved = 0
    try:
        slug = get_city_slug(city)
        base = f"https://www.olx.pl/praca/{slug}/" if slug else "https://www.olx.pl/praca/"
        url = f"{base}?search%5Border%5D=created_at:desc"

        status, html = await asyncio.to_thread(fetch_url, url)
        logger.info(f"OLX status={status} city={city}")
        if status != 200:
            return saved

        data = None
        m = re.search(
            r'window\.__PRERENDERED_STATE__\s*=\s*"(.*?)";\s*(?:window|</script>)',
            html, re.DOTALL
        )
        if m:
            raw = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(
                        m.group(1).encode().decode("unicode_escape")
                    )
                except Exception:
                    pass

        if not data:
            logger.warning(f"OLX no data city={city}")
            return saved

        listing = data.get("listing", {}).get(
            "listing", data.get("listing", {})
        )
        ads = listing.get("ads", [])
        if not ads:
            for k in ["adverts", "data", "items"]:
                v = listing.get(k)
                if v and isinstance(v, list):
                    ads = v
                    break

        logger.info(f"OLX ads={len(ads)} city={city}")

        for item in ads[:30]:
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

                ext_id = hashlib.md5(f"olx_{link}".encode()).hexdigest()

                # Ленивый парсинг — не лезем в карточку если уже есть
                if job_exists_in_db(ext_id):
                    continue

                salary = None
                sal = item.get("salary")
                if sal:
                    salary = (
                        sal.get("displayValue")
                        if isinstance(sal, dict) else str(sal)
                    )
                if not salary:
                    price = item.get("price", {})
                    if isinstance(price, dict):
                        salary = price.get("displayValue")

                location = item.get("location", {})
                job_city = ""
                if isinstance(location, dict):
                    cd = location.get("city", {})
                    job_city = (
                        cd.get("name", "") if isinstance(cd, dict)
                        else (cd if isinstance(cd, str)
                              else location.get("cityName", ""))
                    )

                if city and job_city and not city_matches(job_city, city):
                    continue

                details = await fetch_olx_details(link)
                dc = details.get("city")
                de = details.get("etat")
                du = details.get("umowa")

                if dc:
                    job_city = dc
                if city and not city_matches(job_city, city):
                    continue

                job_id = db_insert_job(
                    ext_id,
                    title,
                    strip_html(job_city or city),
                    strip_html(salary) if salary else None,
                    link,
                    "OLX",
                    umowa=normalize_umowa(du) if du else None,
                    etat=normalize_etat(de) if de else None,
                )
                if job_id:
                    saved += 1

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(f"OLX item: {e}")

        logger.info(f"OLX saved={saved} city={city}")

    except Exception as e:
        logger.error(f"parse_olx({city}): {e}")

    return saved


# ==================== PRACA.PL ====================

async def parse_praca_pl(city: str):
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
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.praca.pl" + link

                ext_id = hashlib.md5(f"pracapl_{link}".encode()).hexdigest()

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
                dt = (
                    details_el.get_text(" ", strip=True).lower()
                    if details_el else ""
                )

                umowa_key = None
                if "umowa o pracę" in dt or "umowa o prace" in dt:
                    umowa_key = "umowa_o_prace"
                elif "umowa zlecenie" in dt:
                    umowa_key = "umowa_zlecenie"
                elif "kontrakt b2b" in dt or " b2b" in dt:
                    umowa_key = "b2b"
                elif "umowa o dzieło" in dt:
                    umowa_key = "umowa_o_dzielo"
                elif "staż" in dt or "praktyk" in dt:
                    umowa_key = "staz"

                etat_key = None
                if any(x in dt for x in [
                    "część etatu", "czesc etatu",
                    "tymczasowa", "dodatkowa", "1/2"
                ]):
                    etat_key = "part"
                elif any(x in dt for x in [
                    "pełny etat", "pelny etat", "pełen etat"
                ]):
                    etat_key = "full"

                job_id = db_insert_job(
                    ext_id, title, job_city, None, link,
                    "Praca.pl", umowa=umowa_key, etat=etat_key
                )
                if job_id:
                    saved += 1

            except Exception as e:
                logger.error(f"Praca.pl item: {e}")

        logger.info(f"Praca.pl saved={saved} city={city}")

    except Exception as e:
        logger.error(f"parse_praca_pl({city}): {e}")

    return saved


# ==================== GOWORK ====================

async def parse_gowork(city: str):
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
                if not link:
                    continue
                if not link.startswith("http"):
                    link = "https://www.gowork.pl" + link

                ext_id = hashlib.md5(f"gowork_{link}".encode()).hexdigest()

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
                umowa_key = None
                etat_key = None

                for tag in card.select(".g-job-item-content__tag"):
                    for sp in tag.select("span"):
                        text = sp.get_text(strip=True).lower()
                        if not text:
                            continue
                        if "zł" in text or "pln" in text:
                            salary = strip_html(sp.get_text(strip=True))
                        if "umowa o pracę" in text or "umowa o prace" in text:
                            umowa_key = "umowa_o_prace"
                        elif "zlecenie" in text:
                            umowa_key = "umowa_zlecenie"
                        elif "b2b" in text or "kontrakt" in text:
                            umowa_key = "b2b"
                        if "pełny etat" in text or "pelny etat" in text:
                            etat_key = "full"
                        elif "część etatu" in text or "niepełny" in text:
                            etat_key = "part"

                job_id = db_insert_job(
                    ext_id, title, job_city, salary, link,
                    "GoWork.pl", umowa=umowa_key, etat=etat_key
                )
                if job_id:
                    saved += 1

            except Exception as e:
                logger.error(f"GoWork item: {e}")

        logger.info(f"GoWork saved={saved} city={city}")

    except Exception as e:
        logger.error(f"parse_gowork({city}): {e}")

    return saved


# ==================== MAIN ====================

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, default=None)
    args = parser.parse_args()

    # Режим сна: если это плановый запуск (без --city) и сейчас ночь по Польше — выходим
    if not args.city and is_night_time():
        logger.info("🌙 Night sleep mode active (23:00 - 08:00 Warsaw time). Skipping scheduled scan.")
        sys.exit(0)

    cleanup_old_jobs()

    if args.city:
        cities = [args.city]
        logger.info(f"🔍 On-demand scrape: {args.city}")
    else:
        cities = MAIN_SCAN_CITIES
        logger.info(f"🔁 Scheduled scrape: {len(cities)} cities")

    total = 0
    for city in cities:
        logger.info(f"=== {city} ===")
        try:
            n1 = await parse_olx(city)
            await asyncio.sleep(2)
            n2 = await parse_praca_pl(city)
            await asyncio.sleep(2)
            n3 = await parse_gowork(city)
            await asyncio.sleep(2)
            total += n1 + n2 + n3
            logger.info(f"City {city}: OLX={n1} Praca={n2} GoWork={n3}")
        except Exception as e:
            logger.error(f"city loop {city}: {e}")

    logger.info(f"✅ Done. Total saved: {total}")


if __name__ == "__main__":
    asyncio.run(main())
