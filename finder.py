"""
Stuttgart Café Space Finder (Free Edition)
============================================
100% kostenlose Version – kein Server, kein Twilio, keine API-Kosten.

Stack:
- Läuft auf GitHub Actions (kostenlos für Public Repos)
- Benachrichtigung via Telegram Bot (komplett kostenlos)
- KI-Analyse via Claude API (optional – Tool funktioniert auch ohne)
- Daten werden als JSON in GitHub gespeichert (Git als Datenbank)

Platforms monitored:
1. ImmoScout24 – Gastronomie & Ladenflächen
2. Kleinanzeigen – Gewerbeimmobilien
3. Immowelt – Ladenflächen
4. meinestadt.de – Gastronomie & Einzelhandel
5. roomstr (Stadt Stuttgart) – Gastroflächen
6. immobilo – Gastgewerbe
7. Stuttgarter Zeitung Immobilien – Gewerberäume
"""

import os
import re
import json
import hashlib
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ─── Configuration ───────────────────────────────────────────────────────────

SEARCH_CONFIG = {
    "city": "Stuttgart",
    "districts": [
        "Stuttgart-Mitte", "Stuttgart-Süd", "Stuttgart-West", "Stuttgart-Ost",
        "Mitte", "Süd", "West", "Ost", "Heusteigviertel", "Bohnenviertel",
        "Kernerviertel", "Lehenviertel", "Karlshöhe", "Marienplatz",
    ],
    "max_rent_eur": 2000,
    "min_area_sqm": 50,
    "max_area_sqm": 100,
}

# Telegram config (free!)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Optional: Claude API for AI scoring (costs ~1€/month)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, "data", "listings.json")
EXPORT_FILE = os.path.join(SCRIPT_DIR, "data", "listings_export.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("StuttgartFinder")

# Browser headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─── Data Model ──────────────────────────────────────────────────────────────

@dataclass
class Listing:
    platform: str
    title: str
    url: str
    price_eur: Optional[float] = None
    area_sqm: Optional[float] = None
    district: Optional[str] = None
    description: Optional[str] = None
    listing_id: Optional[str] = None
    ai_score: Optional[int] = None
    ai_summary: Optional[str] = None
    found_at: Optional[str] = None

    def __post_init__(self):
        if not self.found_at:
            self.found_at = datetime.now().isoformat()
        if not self.listing_id:
            self.listing_id = hashlib.md5(self.url.encode()).hexdigest()[:12]

    def fingerprint(self) -> str:
        return hashlib.md5(f"{self.platform}:{self.url}".encode()).hexdigest()

    def matches_criteria(self) -> bool:
        if self.price_eur and self.price_eur > SEARCH_CONFIG["max_rent_eur"]:
            return False
        if self.area_sqm:
            if self.area_sqm < SEARCH_CONFIG["min_area_sqm"] * 0.8:
                return False
            if self.area_sqm > SEARCH_CONFIG["max_area_sqm"] * 1.3:
                return False
        if self.district:
            district_lower = self.district.lower()
            valid = [d.lower() for d in SEARCH_CONFIG["districts"]]
            if not any(d in district_lower for d in valid):
                if not any(p in district_lower for p in ["mitte", "süd", "west", "ost"]):
                    return False
        return True

    def basic_score(self) -> int:
        """Simple rule-based scoring (no AI needed)."""
        score = 5  # Start at neutral

        # Price scoring
        if self.price_eur:
            if self.price_eur <= 1200:
                score += 2
            elif self.price_eur <= 1600:
                score += 1
            elif self.price_eur > 1800:
                score -= 1

        # Area scoring
        if self.area_sqm:
            if 60 <= self.area_sqm <= 90:
                score += 1  # Sweet spot
            elif self.area_sqm < 50 or self.area_sqm > 100:
                score -= 1

        # District scoring
        if self.district:
            dl = self.district.lower()
            premium = ["mitte", "bohnenviertel", "heusteigviertel", "marienplatz"]
            good = ["süd", "west"]
            if any(p in dl for p in premium):
                score += 2
            elif any(g in dl for g in good):
                score += 1

        # Keyword scoring (from title + description)
        text = f"{self.title} {self.description or ''}".lower()
        positive = ["gastro", "café", "cafe", "restaurant", "küche", "fettabluft",
                     "erdgeschoss", "eg", "laufkundschaft", "schaufenster", "terrasse"]
        negative = ["ablöse", "obergeschoss", "og", "keller", "sanierungsbedürftig",
                     "renovierungsbedürftig", "1. og", "2. og", "3. og"]
        for kw in positive:
            if kw in text:
                score += 1
        for kw in negative:
            if kw in text:
                score -= 1

        return max(1, min(10, score))

    def basic_summary(self) -> str:
        """Generate a simple German summary without AI."""
        parts = []
        if self.price_eur:
            if self.price_eur <= 1200:
                parts.append("Günstiger Preis")
            elif self.price_eur <= 1600:
                parts.append("Fairer Preis")
            else:
                parts.append("Am oberen Preislimit")

        if self.district:
            dl = self.district.lower()
            if "mitte" in dl:
                parts.append("zentrale Lage")
            elif "süd" in dl:
                parts.append("Stuttgart-Süd")
            elif "west" in dl:
                parts.append("Stuttgart-West")

        text = f"{self.title} {self.description or ''}".lower()
        if "gastro" in text or "restaurant" in text or "café" in text:
            parts.append("Gastro-Nutzung möglich")
        if "terrasse" in text:
            parts.append("mit Terrasse")
        if "erdgeschoss" in text or " eg " in text:
            parts.append("Erdgeschoss")
        if "ablöse" in text:
            parts.append("Achtung: Ablöse erforderlich")

        return ". ".join(parts) + "." if parts else "Prüfe die Anzeige für Details."


# ─── JSON Database (no SQLite needed – works with GitHub Actions) ────────────

def load_db() -> dict:
    """Load the JSON database of seen listings."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen": {}, "listings": []}


def save_db(db: dict):
    """Save the JSON database."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def is_new(db: dict, listing: Listing) -> bool:
    return listing.fingerprint() not in db["seen"]


def add_listing(db: dict, listing: Listing):
    db["seen"][listing.fingerprint()] = listing.found_at
    db["listings"].append(asdict(listing))
    # Keep only last 90 days
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    db["listings"] = [l for l in db["listings"] if l.get("found_at", "") > cutoff]


# ─── Scrapers ────────────────────────────────────────────────────────────────

def extract_number(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace(".", "").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def scrape_immoscout24() -> list[Listing]:
    listings = []
    urls = [
        "https://www.immobilienscout24.de/Suche/de/baden-wuerttemberg/stuttgart/gastronomie-mieten?price=-2000.0&livingspace=50.0-100.0",
        "https://www.immobilienscout24.de/Suche/de/baden-wuerttemberg/stuttgart/ladenflaeche-mieten?price=-2000.0&livingspace=50.0-100.0",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                log.warning(f"ImmoScout24 returned {resp.status_code}")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.result-list__listing, article.result-list-entry, [data-item='result']")
            for item in items:
                try:
                    title_el = item.select_one("a.result-list-entry__brand-title-container, h2 a, a[href*='/expose/']")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    if href and not href.startswith("http"):
                        href = f"https://www.immobilienscout24.de{href}"
                    price_el = item.select_one("[data-is24-qa='attributes-price'], .result-list-entry__primary-criterion:first-child")
                    price = extract_number(price_el.get_text()) if price_el else None
                    area_el = item.select_one("[data-is24-qa='attributes-area'], .result-list-entry__primary-criterion:nth-child(2)")
                    area = extract_number(area_el.get_text()) if area_el else None
                    address_el = item.select_one(".result-list-entry__address, [data-is24-qa='attributes-address']")
                    district = address_el.get_text(strip=True) if address_el else None
                    listings.append(Listing(platform="ImmoScout24", title=title, url=href,
                                           price_eur=price, area_sqm=area, district=district))
                except Exception as e:
                    log.debug(f"Parse error: {e}")
        except Exception as e:
            log.error(f"ImmoScout24 error: {e}")
    log.info(f"ImmoScout24: {len(listings)} raw")
    return listings


def scrape_kleinanzeigen() -> list[Listing]:
    listings = []
    url = "https://www.kleinanzeigen.de/s-gewerbeimmobilien/stuttgart/gewerbeimmobilie-mieten/k0c277l9280"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Kleinanzeigen returned {resp.status_code}")
            return listings
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("article.aditem, li.ad-listitem")
        for item in items:
            try:
                title_el = item.select_one("a.ellipsis, h2 a, a[href*='/anzeige/']")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://www.kleinanzeigen.de{href}"
                price_el = item.select_one(".aditem-main--middle--price-shipping--price, .aditem-main--middle p strong")
                price = extract_number(price_el.get_text()) if price_el else None
                desc_el = item.select_one(".aditem-main--middle--description, p.text-module-begin")
                desc = desc_el.get_text(strip=True) if desc_el else None
                loc_el = item.select_one(".aditem-main--top--left, .aditem-main--top")
                district = loc_el.get_text(strip=True) if loc_el else None
                area = None
                full_text = f"{title} {desc or ''}"
                area_match = re.search(r"(\d+)\s*(?:m²|qm|m2)", full_text)
                if area_match:
                    area = float(area_match.group(1))
                listings.append(Listing(platform="Kleinanzeigen", title=title, url=href,
                                       price_eur=price, area_sqm=area, district=district, description=desc))
            except Exception as e:
                log.debug(f"Parse error: {e}")
    except Exception as e:
        log.error(f"Kleinanzeigen error: {e}")
    log.info(f"Kleinanzeigen: {len(listings)} raw")
    return listings


def scrape_immowelt() -> list[Listing]:
    listings = []
    url = "https://www.immowelt.de/suche/stuttgart/ladenflaechen/mieten?pma=2000&ama=50&ami=100"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Immowelt returned {resp.status_code}")
            return listings
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("[data-testid='serp-core-classified-card'], .listitem_wrap, .EstateItem")
        for item in items:
            try:
                title_el = item.select_one("h2, [data-testid='classified-card-title'], .listcontent a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                link_el = item.select_one("a[href*='/expose/']") or title_el.find_parent("a")
                href = link_el.get("href", "") if link_el else ""
                if href and not href.startswith("http"):
                    href = f"https://www.immowelt.de{href}"
                price_el = item.select_one("[data-testid='classified-card-price'], .hardfact.price")
                price = extract_number(price_el.get_text()) if price_el else None
                area_el = item.select_one("[data-testid='classified-card-area'], .hardfact.area")
                area = extract_number(area_el.get_text()) if area_el else None
                loc_el = item.select_one("[data-testid='classified-card-location'], .listlocation")
                district = loc_el.get_text(strip=True) if loc_el else None
                if href:
                    listings.append(Listing(platform="Immowelt", title=title, url=href,
                                           price_eur=price, area_sqm=area, district=district))
            except Exception as e:
                log.debug(f"Parse error: {e}")
    except Exception as e:
        log.error(f"Immowelt error: {e}")
    log.info(f"Immowelt: {len(listings)} raw")
    return listings


def scrape_meinestadt() -> list[Listing]:
    listings = []
    urls = [
        "https://www.meinestadt.de/stuttgart/immobilien/gewerbeimmobilien/gastronomie",
        "https://www.meinestadt.de/stuttgart/immobilien/gewerbeimmobilien/einzelhandel",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".ms-ResultList--item, article, [class*='ResultItem']")
            for item in items:
                try:
                    title_el = item.select_one("h2 a, a[href*='immobilien'], .ms-ResultList--itemTitle")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    if href and not href.startswith("http"):
                        href = f"https://www.meinestadt.de{href}"
                    details = item.get_text()
                    price_match = re.search(r"(\d[\d.,]*)\s*€", details)
                    price = extract_number(price_match.group(1)) if price_match else None
                    area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", details)
                    area = extract_number(area_match.group(1)) if area_match else None
                    if href:
                        listings.append(Listing(platform="meinestadt.de", title=title, url=href,
                                               price_eur=price, area_sqm=area))
                except Exception as e:
                    log.debug(f"Parse error: {e}")
        except Exception as e:
            log.error(f"meinestadt.de error: {e}")
    log.info(f"meinestadt.de: {len(listings)} raw")
    return listings


def scrape_immobilo() -> list[Listing]:
    listings = []
    url = "https://www.immobilo.de/mieten/gewerbe/gastgewerbe/stuttgart"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return listings
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select(".object-list-item, .search-result-entry, article")
        for item in items:
            try:
                title_el = item.select_one("h2 a, a[href*='expose'], .title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://www.immobilo.de{href}"
                details = item.get_text()
                price_match = re.search(r"(\d[\d.,]*)\s*€", details)
                price = extract_number(price_match.group(1)) if price_match else None
                area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", details)
                area = extract_number(area_match.group(1)) if area_match else None
                if href:
                    listings.append(Listing(platform="immobilo", title=title, url=href,
                                           price_eur=price, area_sqm=area))
            except Exception as e:
                log.debug(f"Parse error: {e}")
    except Exception as e:
        log.error(f"immobilo error: {e}")
    log.info(f"immobilo: {len(listings)} raw")
    return listings


def scrape_stuttgarter_zeitung() -> list[Listing]:
    listings = []
    url = "https://immobilien.stuttgarter-zeitung.de/mieten/gewerbe/stuttgart"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return listings
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select(".result-list-item, .immo-item, article")
        for item in items:
            try:
                title_el = item.select_one("h2 a, a[href*='expose'], .title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://immobilien.stuttgarter-zeitung.de{href}"
                details = item.get_text()
                price_match = re.search(r"(\d[\d.,]*)\s*€", details)
                price = extract_number(price_match.group(1)) if price_match else None
                area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", details)
                area = extract_number(area_match.group(1)) if area_match else None
                if href:
                    listings.append(Listing(platform="Stuttgarter Zeitung", title=title, url=href,
                                           price_eur=price, area_sqm=area))
            except Exception as e:
                log.debug(f"Parse error: {e}")
    except Exception as e:
        log.error(f"Stuttgarter Zeitung error: {e}")
    log.info(f"Stuttgarter Zeitung: {len(listings)} raw")
    return listings


# ─── AI Analysis (Optional – works without it) ──────────────────────────────

def analyze_with_ai(listing: Listing) -> tuple[int, str]:
    """Use Claude API if available, otherwise use rule-based scoring."""
    if not ANTHROPIC_API_KEY:
        return (listing.basic_score(), listing.basic_summary())

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Analysiere dieses Gewerbeangebot für ein Café in Stuttgart.
Bewerte 1-10 (10 = perfekt) und gib eine kurze Zusammenfassung auf Deutsch.

Kriterien: Bezirk Mitte/Süd/West/Ost, 50-100m², max 2000€, Gastro-Eignung.
Positiv: Erdgeschoss, Fettabluft, Küche, Laufkundschaft, Terrasse.
Negativ: Hohe Ablöse, Obergeschoss, schlechter Zustand.

Titel: {listing.title}
Preis: {listing.price_eur}€/Monat
Fläche: {listing.area_sqm}m²
Bezirk: {listing.district or 'Unbekannt'}
Beschreibung: {(listing.description or 'Keine')[:400]}

Antworte NUR als JSON: {{"score": <zahl>, "summary": "<text>"}}"""

        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"```json\s*|\s*```", "", text)
        data = json.loads(text)
        return (int(data["score"]), data["summary"])
    except Exception as e:
        log.warning(f"AI analysis failed, using rule-based: {e}")
        return (listing.basic_score(), listing.basic_summary())


# ─── Telegram Notifications (FREE!) ─────────────────────────────────────────

def send_telegram(message: str):
    """Send a Telegram message. Completely free, no limits."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured – printing instead:")
        print(f"\n📱 Message:\n{message}\n")
        return

    # Telegram has a 4096 char limit per message – split if needed
    chunks = []
    if len(message) <= 4096:
        chunks = [message]
    else:
        lines = message.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            chunks.append(current)

    for chunk in chunks:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10)
            if resp.status_code != 200:
                log.error(f"Telegram error: {resp.text}")
            else:
                log.info("Telegram message sent!")
        except Exception as e:
            log.error(f"Telegram send failed: {e}")


def format_telegram_message(listings: list[Listing]) -> str:
    if not listings:
        return "☕ *Stuttgart Café Finder*\nKeine neuen passenden Objekte heute."

    msg = f"☕ *Stuttgart Café Finder*\n"
    msg += f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    msg += f"🆕 *{len(listings)} neue Objekte gefunden!*\n\n"

    for i, l in enumerate(listings, 1):
        score = l.ai_score or 5
        emoji = "🟢" if score >= 7 else "🟡" if score >= 4 else "🔴"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"*{i}. {l.title[:55]}*\n"
        msg += f"📍 {l.platform}"
        if l.district:
            msg += f" · {l.district}"
        msg += "\n"
        if l.price_eur:
            msg += f"💰 {l.price_eur:.0f}€/Monat"
        if l.area_sqm:
            msg += f" · 📐 {l.area_sqm:.0f}m²"
        msg += "\n"
        msg += f"{emoji} Score: {score}/10\n"
        if l.ai_summary:
            msg += f"💡 _{l.ai_summary}_\n"
        msg += f"[🔗 Anzeige öffnen]({l.url})\n\n"

    return msg


# ─── Export for Dashboard ────────────────────────────────────────────────────

def export_json(db: dict):
    os.makedirs(os.path.dirname(EXPORT_FILE), exist_ok=True)
    export_data = {
        "last_updated": datetime.now().isoformat(),
        "total_listings": len(db["listings"]),
        "search_config": SEARCH_CONFIG,
        "listings": db["listings"],
    }
    with open(EXPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    log.info(f"Exported {len(db['listings'])} listings")


# ─── Main Pipeline ───────────────────────────────────────────────────────────

def run():
    log.info("=" * 50)
    log.info("Stuttgart Café Finder – Starting scan")
    log.info("=" * 50)

    db = load_db()

    scrapers = [
        ("ImmoScout24", scrape_immoscout24),
        ("Kleinanzeigen", scrape_kleinanzeigen),
        ("Immowelt", scrape_immowelt),
        ("meinestadt.de", scrape_meinestadt),
        ("immobilo", scrape_immobilo),
        ("Stuttgarter Zeitung", scrape_stuttgarter_zeitung),
    ]

    all_listings = []
    for name, scraper in scrapers:
        try:
            log.info(f"Scraping {name}...")
            results = scraper()
            all_listings.extend(results)
        except Exception as e:
            log.error(f"{name} failed: {e}")

    log.info(f"Total raw: {len(all_listings)}")

    new_listings = []
    for listing in all_listings:
        if not listing.matches_criteria():
            continue
        if not is_new(db, listing):
            continue

        score, summary = analyze_with_ai(listing)
        listing.ai_score = score
        listing.ai_summary = summary

        add_listing(db, listing)
        new_listings.append(listing)

    log.info(f"New matches: {len(new_listings)}")

    new_listings.sort(key=lambda l: l.ai_score or 0, reverse=True)

    if new_listings:
        message = format_telegram_message(new_listings)
        send_telegram(message)

    save_db(db)
    export_json(db)

    log.info("Scan complete!")
    return new_listings


if __name__ == "__main__":
    run()
