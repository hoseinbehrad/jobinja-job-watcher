"""
Collect Jobinja company URLs (Tehran only) from a company list category.

Usage:
    python jobinja_companies.py 20
    python jobinja_companies.py 20 "https://jobinja.ir/company/list/<category>"

Scrapes pages 1..N in parallel (see WORKERS), keeps companies located in
Tehran or with no location, and writes <company-url>/jobs to companies.txt, one per line.

Requires: pip install requests beautifulsoup4
"""

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = (
    "https://jobinja.ir/company/list/"
    "%D9%85%D8%A7%D9%84%DB%8C-%D9%88-%D8%A7%D8%B9%D8%AA%D8%A8%D8%A7%D8%B1%DB%8C"
)
OUTPUT_FILE = "companies.txt"
CITY = "تهران"
WORKERS = 2           # pages fetched at the same time. Lower it if you get 429/403
DELAY_SECONDS = 2   # base wait before a retry
RETRIES = 3
TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}


def fetch(session, base_url, page):
    """Return page HTML, or None if it could not be fetched."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(base_url, params={"page": page}, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            print(f"  page {page}: HTTP {r.status_code} (attempt {attempt})")
            if r.status_code == 429:  # rate limited: wait longer
                time.sleep(DELAY_SECONDS * attempt * 4)
                continue
        except requests.RequestException as e:
            print(f"  page {page}: {e} (attempt {attempt})")
        time.sleep(DELAY_SECONDS * attempt)
    return None


def norm(text):
    """Strip spaces, hyphens and zero-width chars so tag text can be compared."""
    return re.sub(r"[\s\-\u200c\u200b]+", "", text or "")


def category_from_url(base_url):
    """Category name as it appears in the tags, taken from the URL slug."""
    slug = unquote(urlparse(base_url).path.rstrip("/").split("/")[-1])
    return norm(slug)


def parse(html, category):
    """Return (total_cards, no_location_count, [company_urls]) for one page.

    Tags block holds up to three spans: location, category, hiring status.
    Location = a span that is not the category and has no class (status is
    class="green"). No location span -> treated as Tehran.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("#browse a.c-companyOverview")
    urls = []
    no_location = 0
    for card in cards:
        href = (card.get("href") or "").strip().rstrip("/")
        if not href:
            continue
        location = None
        for span in card.select(".c-companyOverview__tags span"):
            text = span.get_text(strip=True)
            if not text or span.get("class") or norm(text) == category:
                continue
            location = text
            break
        if location is None:
            no_location += 1
            urls.append(href + "/jobs")
        elif CITY in location:
            urls.append(href + "/jobs")
    return len(cards), no_location, urls


def main():
    pages_arg = sys.argv[1] if len(sys.argv) > 1 else input("Number of pages: ")
    try:
        pages = int(pages_arg)
    except ValueError:
        sys.exit("Page count must be a number, e.g. 20")
    base_url = sys.argv[2] if len(sys.argv) > 2 else BASE_URL

    category = category_from_url(base_url)

    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=WORKERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    def work(page):
        html = fetch(session, base_url, page)
        return None if html is None else parse(html, category)

    by_page = {}
    failed = []
    start = time.time()
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {pool.submit(work, p): p for p in range(1, pages + 1)}
    try:
        for fut in as_completed(futures):
            page = futures[fut]
            res = fut.result()
            if res is None:
                failed.append(page)
                print(f"page {page}: failed")
                continue
            total, no_loc, urls = res
            by_page[page] = urls
            print(
                f"page {page}: {total} companies, {len(urls)} kept "
                f"({no_loc} had no location)"
            )
    except KeyboardInterrupt:
        print("Interrupted, saving what was collected")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        # Write in page order, without duplicates
        results = list(dict.fromkeys(u for p in sorted(by_page) for u in by_page[p]))
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(results))
            if results:
                f.write("\n")
        print(f"Saved {len(results)} URLs to {OUTPUT_FILE} in {time.time() - start:.0f}s")
        if failed:
            print(f"Failed pages: {sorted(failed)}")


if __name__ == "__main__":
    main()