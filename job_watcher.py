#!/usr/bin/env python3
"""
Jobinja watcher.

Reads company job-list URLs from urls.txt and keywords from keywords.txt,
finds open positions whose title matches a keyword, and reports only the
ones it has not reported before.

Terminal:
    python job_watcher.py          # new positions only
    python job_watcher.py --all    # every open match, new ones flagged
    python job_watcher.py 1-100    # only companies 1 to 100 of urls.txt
    python job_watcher.py 101-200 --all

Jupyter / VS Code notebook cell (live HTML table with clickable titles):
    %run job_watcher.py
    %run job_watcher.py 1-100
    %run job_watcher.py 1-100 --all

Keyword rules (keywords.txt):
    business analyst      every word must be in the title, any order, other
                          words allowed between: matches "Business Data Analyst"
    "business analyst"    in quotes: the words must be next to each other
    Spaces, half-spaces and Arabic/Persian letter variants never matter.

Optional Telegram or Bale notification, set these environment variables:
    JOB_BOT_TOKEN   bot token
    JOB_CHAT_ID     your chat id
    JOB_BOT_API     https://api.telegram.org (default) or https://tapi.bale.ai
"""

import html
import json
import os
import re
import sys
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
URLS_FILE = BASE / "urls.txt"
KEYWORDS_FILE = BASE / "keywords.txt"
SEEN_FILE = BASE / "seen.json"
LOG_FILE = BASE / "new_jobs.log"

BOT_TOKEN = os.getenv("JOB_BOT_TOKEN", "")
CHAT_ID = os.getenv("JOB_CHAT_ID", "")
BOT_API = os.getenv("JOB_BOT_API", "https://api.telegram.org").rstrip("/")

MAX_PAGES = 20          # safety cap per company
WORKERS = 2             # companies fetched at the same time (1 = slow and gentle)
DELAY_SECONDS = 5.0     # pause each worker takes after a request
TIMEOUT = 20
RETRIES = 1             # only for timeouts / 429 / 5xx, never for 404
FULL_PAGE = 10          # a page with fewer jobs than this cannot have a next page

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}

JOB_RE = re.compile(r"/companies/([^/]+)/jobs/([^/?#]+)")
COMPANY_RE = re.compile(r"jobinja\.ir/companies/([^/?#]+)", re.I)

# Arabic letter variants -> Persian ones
_CHAR_MAP = str.maketrans({
    "ي": "ی", "ى": "ی", "ئ": "ی",
    "ك": "ک",
    "أ": "ا", "إ": "ا", "آ": "ا",
    "ة": "ه", "ۀ": "ه",
    "ؤ": "و",
    "ـ": "",   # tatweel
})
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ----------------------------------------------------------------- matching

def fold(text: str) -> str:
    """Lowercase, unify Arabic/Persian letters, drop diacritics and half-spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Mn", "Cf"))
    return unicodedata.normalize("NFC", text).translate(_CHAR_MAP)


def words(text: str) -> list[str]:
    """Folded text split into words (punctuation counts as a separator)."""
    return re.sub(r"[\W_]+", " ", fold(text)).split()


def squash(text: str) -> str:
    """Folded text with every space and punctuation mark removed.

    'تحلیل گر', 'تحلیل‌گر' and 'تحلیلگر' all become the same string.
    """
    return "".join(words(text))


def compile_keyword(raw: str) -> dict:
    quoted = len(raw) > 2 and raw[0] in "\"“”«" and raw[-1] in "\"“”»"
    text = raw.strip("\"“”«» ")
    return {"raw": text, "exact": quoted, "words": words(text), "squashed": squash(text)}


def _word_in_title(word: str, title_words: list[str], title_squashed: str) -> bool:
    if word.isascii():
        if len(word) <= 3:                      # "bi" must not match "mobile"
            return word in title_words
        return any(t.startswith(word) for t in title_words)   # analyst -> analysts
    return word in title_squashed               # Persian: spacing-proof


def match(title: str, keywords: list[dict]) -> str | None:
    t_words, t_squashed = words(title), squash(title)
    for kw in keywords:
        if not kw["words"]:
            continue
        if kw["exact"]:
            if kw["squashed"] in t_squashed:
                return kw["raw"]
        elif all(_word_in_title(w, t_words, t_squashed) for w in kw["words"]):
            return kw["raw"]
    return None


# ----------------------------------------------------------------- scraping

def read_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    lines = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def company_url(line: str) -> str | None:
    """Turn any form of a company link into .../companies/<slug>/jobs.

    Fixes '/jobs/jobs', a missing '/jobs', trailing slashes and query strings.
    A bare slug ('asiatech') is accepted too. Returns None if it is not usable.
    """
    m = COMPANY_RE.search(line)
    slug = m.group(1).strip() if m else (line.strip() if re.fullmatch(r"[\w.%-]+", line.strip()) else "")
    return f"https://jobinja.ir/companies/{slug}/jobs" if slug else None


def load_urls(path: Path) -> tuple[list[str], list[str], int]:
    """Returns (clean unique urls, unusable lines, number of duplicates dropped)."""
    urls, bad, dupes = [], [], 0
    for line in read_lines(path):
        url = company_url(line)
        if not url:
            bad.append(line)
        elif url in urls:
            dupes += 1
        else:
            urls.append(url)
    return urls, bad, dupes


def parse_chunk(arg: str | None, n: int) -> tuple[int, int]:
    """'1-100' -> (1, 100). Empty -> all companies. Positions count from 1."""
    if not arg:
        return 1, n
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", arg)
    if not m:
        raise ValueError(f"Cannot read the range {arg!r}. Write it like 1-100.")
    start, end = int(m.group(1)), int(m.group(2))
    if start < 1 or start > end or start > n:
        raise ValueError(f"Bad range {arg!r}: urls.txt has {n} companies.")
    return start, min(end, n)


def chunk_from_argv() -> str | None:
    """Finds '1-100' (or '1 - 100') among the command-line arguments."""
    m = re.search(r"(?<![\w.\-/\\])(\d+)\s*-\s*(\d+)(?![\w.\-])", " ".join(sys.argv[1:]))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def slug_of(url: str) -> str:
    m = COMPANY_RE.search(url)
    return m.group(1) if m else url


def clean(text: str) -> str:
    return " ".join(text.split())


def age_days(age: str) -> int:
    """'امروز' -> 0, 'دیروز' -> 1, '۲۷ روز پیش' -> 27. Unknown -> 9999."""
    a = age.translate(_DIGITS)
    if "امروز" in a:
        return 0
    if "دیروز" in a:
        return 1
    m = re.search(r"\d+", a)
    return int(m.group()) if m else 9999


def parse_jobs(page_html: str) -> list[dict]:
    """Extract open positions from one company jobs page."""
    soup = BeautifulSoup(page_html, "html.parser")
    jobs = []
    for li in soup.select("li.c-jobListView__item"):
        link = li.select_one("a.c-jobListView__titleLink")
        if not link or not link.get("href"):
            continue
        href = link["href"]
        m = JOB_RE.search(href)
        job_id = f"{m.group(1)}/{m.group(2)}" if m else href

        age_el = li.select_one(".c-jobListView__passedDays")
        age = clean(age_el.get_text()).strip("() ") if age_el else ""
        if "منقضی" in age:  # expired
            continue

        meta = [clean(x.get_text()) for x in li.select("li.c-jobListView__metaItem")]
        contract = meta[2] if len(meta) > 2 else ""
        contract = clean(contract.replace("(برای مشاهده حقوق وارد شوید)", ""))
        jobs.append({
            "id": job_id,
            "title": clean(link.get_text()),
            "url": href,
            "age": age,
            "days": age_days(age),
            "company": meta[0] if len(meta) > 0 else "",
            "location": meta[1] if len(meta) > 1 else "",
            "contract": contract,
        })
    return jobs


class NotFound(Exception):
    pass


_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    return _local.session


def get(url: str, params=None) -> str:
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = _session().get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as e:      # timeout, connection reset
            last_error = e
        else:
            if r.status_code == 404:
                raise NotFound("404, no such company page")
            if r.status_code == 429 or r.status_code >= 500:
                last_error = RuntimeError(f"HTTP {r.status_code}")
            elif r.status_code >= 400:              # 403 etc: retrying will not help
                raise RuntimeError(f"HTTP {r.status_code}")
            else:
                return r.text
        if attempt < RETRIES:
            time.sleep(3 * attempt)
    raise last_error


def has_next_page(page_html: str, page: int, jobs_on_page: int) -> bool:
    if re.search(rf"[?&;]page={page + 1}\b", page_html):
        return True
    return jobs_on_page >= FULL_PAGE


def fetch_company(url: str) -> list[dict]:
    """Fetch all pages of one company's job list."""
    jobs, ids = [], set()
    for page in range(1, MAX_PAGES + 1):
        page_html = get(url, params={"page": page} if page > 1 else None)
        time.sleep(DELAY_SECONDS)
        found = parse_jobs(page_html)
        fresh = [j for j in found if j["id"] not in ids]
        if not fresh:  # empty page, or the site repeated page 1
            break
        jobs.extend(fresh)
        ids.update(j["id"] for j in fresh)
        if not has_next_page(page_html, page, len(found)):
            break
    return jobs


# ------------------------------------------------------------------- output

def in_notebook() -> bool:
    try:
        from IPython import get_ipython
        shell = get_ipython()
        return shell is not None and shell.__class__.__name__ == "ZMQInteractiveShell"
    except Exception:
        return False


def format_job(job: dict) -> str:
    parts = [("[NEW] " if job.get("flag_new") else "") + job["title"]]
    details = " | ".join(x for x in (job["company"], job["location"], job["contract"], job["age"]) if x)
    if details:
        parts.append(details)
    parts.append(job["url"])
    return "\n".join(parts)


def render_html(summary: str, jobs: list[dict], problems: list[str], show_new_flag: bool,
                empty: list[str] | None = None) -> str:
    e = html.escape
    line = "border-bottom:1px solid rgba(128,128,128,.28);"
    th = f"text-align:left;padding:8px 12px;font-weight:600;white-space:nowrap;{line}background:rgba(128,128,128,.14);"
    td = f"text-align:left;padding:8px 12px;vertical-align:top;{line}"
    badge = ("display:inline-block;margin-left:8px;padding:1px 7px;border-radius:9px;"
             "font-size:11px;font-weight:600;background:#1a7f37;color:#fff;")
    out = [f'<div style="font-family:Segoe UI,Tahoma,Vazirmatn,sans-serif;font-size:13px;line-height:1.5">',
           f'<div style="margin:2px 0 10px;font-weight:600">{e(summary)}</div>']
    if problems:
        out.append(f'<details style="margin:0 0 8px;color:#c9510c"{" open" if len(problems) <= 5 else ""}>'
                   f'<summary style="cursor:pointer">&#9888; {len(problems)} problem(s), click to expand</summary>'
                   + "<br>".join(e(p) for p in problems) + "</details>")
    if empty:
        out.append('<details style="margin:0 0 10px;opacity:.75">'
                   f'<summary style="cursor:pointer">{len(empty)} companies with no open jobs</summary>'
                   + e(", ".join(empty)) + "</details>")
    if jobs:
        heads = ["#", "Position", "Company", "Location", "Contract", "Posted", "Keyword"]
        out.append('<table style="border-collapse:collapse;width:100%">')
        out.append("<thead><tr>" + "".join(f'<th style="{th}">{h}</th>' for h in heads) + "</tr></thead><tbody>")
        for i, j in enumerate(jobs, 1):
            flag = f'<span style="{badge}">NEW</span>' if show_new_flag and j.get("flag_new") else ""
            title = (f'<a href="{e(j["url"], quote=True)}" target="_blank" '
                     f'style="font-weight:600;text-decoration:none">{e(j["title"])}</a>{flag}')
            cells = [str(i), title, e(j["company"]), e(j["location"]), e(j["contract"]),
                     e(j["age"]), e(j.get("keyword", ""))]
            nowrap = {0, 3, 4, 5, 6}
            out.append("<tr>" + "".join(
                f'<td dir="auto" style="{td}{"white-space:nowrap;" if k in nowrap else ""}">{c}</td>'
                for k, c in enumerate(cells)) + "</tr>")
        out.append("</tbody></table>")
    else:
        out.append('<div style="opacity:.7">No new positions.</div>')
    out.append("</div>")
    return "".join(out)


def send_bot(text: str) -> None:
    """Send via Telegram-compatible bot API (Telegram or Bale). Raises on failure."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > 3500 and current:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    if current.strip():
        chunks.append(current)
    for chunk in chunks:
        r = requests.post(
            f"{BOT_API}/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": chunk.strip(), "disable_web_page_preview": "true"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()


# --------------------------------------------------------------------- main

def main(show_all: bool | None = None, chunk: str | None = None) -> list[dict]:
    """Run one check. show_all=True lists every open match, not only new ones.
    chunk="1-100" checks only companies 1 to 100 of urls.txt.

    Returns the list of new jobs.
    """
    if show_all is None:
        show_all = "--all" in sys.argv
    if chunk is None:
        chunk = chunk_from_argv()
    notebook = in_notebook()
    if hasattr(sys.stdout, "reconfigure"):  # Persian output on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    urls, bad_lines, dupes = load_urls(URLS_FILE)
    keywords = [compile_keyword(k) for k in read_lines(KEYWORDS_FILE)]
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    n_all = len(urls)
    first, last = parse_chunk(chunk, n_all)
    urls = urls[first - 1:last]
    scope = f"companies {first}-{last} of {n_all}"

    handle = None
    if notebook:
        from IPython.display import HTML, display
        handle = display(HTML(f"Checking {scope} ..."), display_id=True)

    def live_rows():
        rows = matched_jobs if show_all else [j for j in matched_jobs if j["flag_new"]]
        return sorted(rows, key=lambda j: j["days"])

    matched_jobs, empty = [], []
    problems = [f"UNUSABLE LINE in urls.txt: {b}" for b in bad_lines]
    total = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
        futures = {pool.submit(fetch_company, u): u for u in urls}
        for done, fut in enumerate(as_completed(futures), 1):
            url = futures[fut]
            if handle is None:
                print(f"\rChecking companies: {done}/{len(urls)}", end="", flush=True)
            try:
                jobs = fut.result()
            except NotFound:
                problems.append(f"NOT FOUND  {url}  (wrong company name in the link?)")
                continue
            except Exception as e:
                problems.append(f"FAILED  {url}  ({e})")
                continue
            if not jobs:
                empty.append(slug_of(url))
            total += len(jobs)
            for job in jobs:
                kw = match(job["title"], keywords)
                if kw:
                    job["keyword"] = kw
                    job["flag_new"] = job["id"] not in seen
                    matched_jobs.append(job)
                    if handle is None and (show_all or job["flag_new"]):      # live, terminal
                        print("\r" + " " * 40 + "\r" + format_job(job if show_all else {**job, "flag_new": False}) + "\n")
            if handle is not None:                                             # live, notebook
                handle.update(HTML(render_html(
                    f"Checking {scope}: {done}/{len(urls)} done, {len(matched_jobs)} matched so far ...",
                    live_rows(), problems, show_new_flag=show_all, empty=empty)))
    if handle is None:
        print("\r" + " " * 40 + "\r", end="")
    problems.sort()
    empty.sort()
    took = time.time() - started

    matched_jobs.sort(key=lambda j: j["days"])          # newest first
    new_jobs = [j for j in matched_jobs if j["flag_new"]]
    shown = matched_jobs if show_all else new_jobs

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = (f"{now}  |  {scope}, {total} open jobs, "
               f"{len(matched_jobs)} matched, {len(new_jobs)} new  |  {took / 60:.1f} min")
    if dupes:
        summary += f"  |  {dupes} duplicate links skipped"

    if notebook:
        handle.update(HTML(render_html(summary, shown, problems, show_new_flag=show_all, empty=empty)))
    else:                                   # matches were already printed while running
        print(summary)
        for p in problems:
            print("  ! " + p)
        if empty:
            print(f"  {len(empty)} companies with no open jobs: " + ", ".join(empty))

    if not new_jobs:
        return []

    body = "\n\n".join(format_job({**j, "flag_new": False}) for j in new_jobs)
    delivered = True
    if BOT_TOKEN and CHAT_ID:
        try:
            send_bot(f"{len(new_jobs)} new position(s)\n\n{body}")
        except Exception as e:
            delivered = False
            print(f"\n  ! Bot message failed, will retry next run: {e}")

    # Only mark as seen once delivered, so a failed send is retried tomorrow.
    if delivered:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"===== {now} =====\n{body}\n\n")
        for j in new_jobs:
            seen[j["id"]] = {"title": j["title"], "first_seen": now}
        SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    return new_jobs


if __name__ == "__main__":
    main()