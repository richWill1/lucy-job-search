import json, re, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "jobs.json"

SOURCES = [
    ("Teaching Vacancies", "https://teaching-vacancies.service.gov.uk/jobs?location=wakefield", "/jobs/"),
    ("Healthcare Job Search", "https://healthcarejobsearch.co.uk/jobs?location=Wakefield", "/jobs/"),
    ("Wakefield VCSE / Nova", "https://www.nova-wd.org.uk/nova/jobs", "/jobs/"),
    ("Leeds Jobs", "https://www.leedsjobs.com/jobs/admin-secretarial-pa-office", "/job/"),
    ("jobs.ac.uk — Leeds", "https://www.jobs.ac.uk/categories/jobs-in-leeds/1", "/job/"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LucyJobSearch/1.0; +https://lucy-job-search.onrender.com)"}
ALLOWED = re.compile(r"\b(administrator|administration|admin|receptionist|reception|customer service|library|librarian|finance assistant|accounts|procurement|hr officer|human resources|personal assistant|business support|office|coordinator|support officer|research administrator|clerical|pa)\b", re.I)
EXCLUDED = re.compile(r"\b(teaching assistant|ta\b|send|special educational needs|classroom|teacher|lecturer|teaching|pupil support|learning support assistant|pastoral support|one[- ]to[- ]one|1:1)\b", re.I)
LOC = re.compile(r"\b(wakefield|leeds|normanton|ossett|castleford|pudsey|bradford|west yorkshire|remote)\b", re.I)

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def jsonld_job(soup):
    for tag in soup.find_all("script", attrs={"type":"application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text())
            items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
        except Exception:
            pass
    return {}

def infer_coords(location):
    s = (location or "").lower()
    if "bradford" in s: return 53.795, -1.735
    if "leeds" in s: return 53.800, -1.550
    if "normanton" in s: return 53.700, -1.420
    if "ossett" in s: return 53.680, -1.545
    if "castleford" in s: return 53.725, -1.355
    if "pudsey" in s: return 53.795, -1.660
    return 53.712, -1.478

def parse_job(url, source):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        j = jsonld_job(soup)
        title = clean(j.get("title")) or clean(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
        desc = clean(BeautifulSoup(j.get("description",""), "html.parser").get_text(" ", strip=True)) if j.get("description") else clean(soup.get_text(" ", strip=True))
        org = j.get("hiringOrganization") or {}
        company = clean(org.get("name") if isinstance(org, dict) else "") or source
        loc = j.get("jobLocation") or {}
        if isinstance(loc, list): loc = loc[0] if loc else {}
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        location = clean(" ".join(str(addr.get(k,"")) for k in ("addressLocality","addressRegion","postalCode"))) or "Wakefield / Leeds"
        salary = j.get("baseSalary") or {}
        pay = ""
        if isinstance(salary, dict):
            val = salary.get("value", {})
            if isinstance(val, dict):
                lo, hi = val.get("minValue"), val.get("maxValue")
                if lo and hi: pay = f"£{lo:,.0f}–£{hi:,.0f}"
                elif lo: pay = f"£{lo:,.0f}"
        if not pay:
            m = re.search(r"£\s?\d[\d,]*(?:\.\d+)?(?:\s*[–-]\s*£?\s?\d[\d,]*(?:\.\d+)?)?", desc)
            pay = m.group(0).replace(" ", "") if m else "See live advert"
        valid = j.get("validThrough")
        close = valid[:10] if isinstance(valid, str) else ""
        posted = j.get("datePosted")
        posted = posted[:10] if isinstance(posted, str) else ""
        if not title or not ALLOWED.search(title + " " + desc) or EXCLUDED.search(title + " " + desc) or not LOC.search(location):
            return None
        lat, lng = infer_coords(location)
        return {
            "title": title, "company": company, "location": location, "type": "Library" if re.search(r"library|librarian", title, re.I) else "Customer service" if re.search(r"customer service|reception", title, re.I) else "Administration",
            "hours": "part" if re.search(r"part[- ]time|part time", desc, re.I) else "hybrid" if re.search(r"hybrid", desc, re.I) else "full",
            "pay": pay, "close": close or "Open — check live advert", "posted": posted or "Not stated", "deadline": close or "Open — check live advert",
            "fit": "Daily-scraped vacancy matching the search criteria. Check the live advert and person specification before applying.",
            "url": url, "source": source, "lat": lat, "lng": lng
        }
    except Exception:
        return None

def main():
    try:
        existing = json.loads(DATA.read_text())
    except Exception:
        existing = []
    found = {}
    session = requests.Session()
    session.headers.update(HEADERS)
    for source, listing, path in SOURCES:
        try:
            r = session.get(listing, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(listing, a["href"])
                text = clean(a.get_text(" ", strip=True))
                if path in href and text and len(text) > 5:
                    links.append(href)
            for url in list(dict.fromkeys(links))[:40]:
                job = parse_job(url, source)
                if job:
                    found[url] = job
                time.sleep(0.15)
        except Exception:
            continue

    # Keep manually verified vacancies and merge fresh scraped postings.
    merged = {j.get("url"): j for j in existing if j.get("url")}
    for url, job in found.items():
        merged[url] = job

    # Remove jobs whose explicit closing date has passed.
    today = datetime.now(timezone.utc).date()
    clean_jobs = []
    for job in merged.values():
        close = str(job.get("close",""))
        m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", close)
        if m:
            try:
                if datetime.strptime(m.group(0), "%Y-%m-%d").date() < today:
                    continue
            except ValueError:
                pass
        clean_jobs.append(job)

    clean_jobs.sort(key=lambda j: (0 if re.search(r"wakefield|wf3|wf1|wf2|wf6", j.get("location",""), re.I) else 1, j.get("company",""), j.get("title","")))
    if len(clean_jobs) < 8:
        raise SystemExit("Scrape returned too few usable roles; preserving the previous jobs.json rather than publishing a broken feed.")
    DATA.write_text(json.dumps(clean_jobs, ensure_ascii=False, indent=2) + "\n")

if __name__ == "__main__":
    main()
