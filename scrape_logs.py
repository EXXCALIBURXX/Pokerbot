#!/usr/bin/env python3
"""
scrape_logs.py — IIT Pokerbots Match Log Scraper & Analyzer

Downloads match logs from https://www.iitpokerbots.in/dashboard/matches,
extracts them from .gz, and optionally runs analysis via analyze_logs_v2.py.

Requirements:  pip install selenium requests

Usage:
    python scrape_logs.py                      # Download last 20 matches
    python scrape_logs.py --last 50            # Download last 50 matches
    python scrape_logs.py --all                # Download ALL matches
    python scrape_logs.py --last 10 --analyze  # Download + analyze last 10
    python scrape_logs.py --analyze-only       # Just analyze existing logs
    python scrape_logs.py --list               # List matches without downloading
    python scrape_logs.py --opponent twig      # Only download matches vs twig
    python scrape_logs.py --losses-only        # Only download losses

NOTE: Uses Chrome with a separate profile — your main Brave stays open!
      First run: you'll need to log in once.  Session is remembered after that.
"""

import argparse
import gzip
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR   = Path(__file__).parent.resolve()
LOG_DIR      = SCRIPT_DIR / "real_bot_logs"
DL_TEMP      = LOG_DIR / "_tmp_downloads"
MANIFEST     = LOG_DIR / "_manifest.json"
MATCHES_URL  = "https://www.iitpokerbots.in/dashboard/matches"
PLAYER_NAME  = "6_7"
ANALYZER     = SCRIPT_DIR / "analyze_logs_v2.py"

# Use Chrome for scraping so your main Brave stays open
CHROME_PATH  = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# Dedicated scraper profile (login once, remembered forever)
SCRAPER_PROFILE = SCRIPT_DIR / "._scraper_profile"

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def sanitize(name):
    """Make a string filesystem-safe."""
    return re.sub(r'[<>:"/\\|?*\s]+', '_', name).strip('_')[:60]


def parse_date(date_str):
    """Parse site date → 'YYYYMMDD-HHMM'.  Tries multiple formats."""
    date_str = date_str.strip().rstrip(',')
    for fmt in (
        "%b %d, %Y, %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y, %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y%m%d-%H%M")
        except ValueError:
            continue
    nums = re.findall(r'\d+', date_str)
    return '-'.join(nums[:6]) if nums else 'unknown'


def load_manifest():
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding='utf-8'))
    return {"downloaded": {}}


def save_manifest(m):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding='utf-8')

# ═══════════════════════════════════════════════════════════════════════════════
# BROWSER
# ═══════════════════════════════════════════════════════════════════════════════

def launch_browser():
    """Launch Chrome with a dedicated scraper profile (Brave stays open)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    if not os.path.isfile(CHROME_PATH):
        sys.exit(f"ERROR: Chrome not found at {CHROME_PATH}")

    SCRAPER_PROFILE.mkdir(parents=True, exist_ok=True)

    # Clean stale lock files from scraper profile
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lf = SCRAPER_PROFILE / lock
        if lf.exists():
            try: lf.unlink()
            except OSError: pass

    opts = Options()
    opts.binary_location = CHROME_PATH
    opts.add_argument(f"--user-data-dir={SCRAPER_PROFILE}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-search-engine-choice-screen")

    DL_TEMP.mkdir(parents=True, exist_ok=True)
    opts.add_experimental_option("prefs", {
        "download.default_directory":  str(DL_TEMP),
        "download.prompt_for_download": False,
        "download.directory_upgrade":   True,
    })
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.add_argument("--log-level=3")

    print("Launching Chrome scraper (your main Brave stays open) ...")
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:
        sys.exit(
            f"ERROR: Could not launch Chrome.\n{e}\n\n"
            "→  Run: pip install --upgrade selenium"
        )

    driver.set_page_load_timeout(30)
    return driver

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE SCRAPING
# ═══════════════════════════════════════════════════════════════════════════════

# JavaScript injected into the page to extract structured match data.
# Targets the actual site DOM: <a href="...log.gz" download> inside card divs.
JS_EXTRACT_MATCHES = r"""
var links = document.querySelectorAll('a[href*=".log.gz"]');
var results = [];
for (var i = 0; i < links.length; i++) {
    var a = links[i];
    var card = a.closest('.rounded-xl');
    if (!card) continue;
    var text = card.innerText || '';

    var date = '';
    var dm = text.match(/((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s*\d{4},?\s*\d{1,2}:\d{2}\s*[AP]M)/i);
    if (dm) date = dm[1];

    var result = 'UNKNOWN';
    if (/VICTORY/i.test(text)) result = 'WIN';
    else if (/DEFEAT/i.test(text)) result = 'LOSS';
    else if (/TIE/i.test(text)) result = 'TIE';

    var opp = 'unknown';
    var om = text.match(/vs\.\s+(.+)/m);
    if (om) opp = om[1].trim();

    var chips = 0;
    var cm = text.match(/Net\s*Chips[:\s]*([+-]?\s*[\d,]+)/i);
    if (cm) chips = parseInt(cm[1].replace(/[\s,]/g, ''), 10);

    results.push({
        index:        i,
        opponent:     opp,
        result:       result,
        net_chips:    chips,
        date:         date,
        download_url: a.href
    });
}
return results;
"""


def scroll_to_load(driver, target, max_seconds=180):
    """Scroll the page until enough match entries are loaded."""
    from selenium.webdriver.common.by import By

    deadline = time.time() + max_seconds
    prev = 0
    stalls = 0

    while time.time() < deadline:
        # count by "Download Log" anchors
        n = len(driver.find_elements(By.XPATH,
            "//*[contains(translate(text(),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            "'download log')]"))

        if target and n >= target:
            return n
        if n == prev:
            stalls += 1
            if stalls >= 6:
                # try a "Load More" / "Show More" button
                try:
                    btn = driver.find_element(By.XPATH,
                        "//button[contains(translate(text(),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        "'load') or contains(translate(text(),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        "'more') or contains(translate(text(),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        "'show')]")
                    btn.click()
                    time.sleep(2)
                    stalls = 0
                    continue
                except Exception:
                    return n          # truly no more
        else:
            stalls = 0

        prev = n
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)

    return prev


def scrape_matches(driver, limit):
    """Navigate to the matches page & return list of match dicts."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    print(f"Loading {MATCHES_URL}")
    driver.get(MATCHES_URL)

    # wait for at least one "Download Log" to appear (or login redirect)
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//*[contains(translate(text(),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'download log')]"
            ))
        )
    except Exception:
        # Check if we need to log in (first run with scraper profile)
        page_src = (driver.page_source or '').lower()
        url = driver.current_url.lower()
        needs_login = ('login' in url or 'sign' in url or 'auth' in url
                       or 'download log' not in page_src)
        if needs_login:
            print("\n  ╔══════════════════════════════════════════════════════════╗")
            print("  ║  Not logged in!  Please log in in the browser window.   ║")
            print("  ║  (First time only — session will be remembered.)        ║")
            print("  ╚══════════════════════════════════════════════════════════╝")
            input("\n  Press Enter after logging in ... ")
            driver.get(MATCHES_URL)
            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((
                        By.XPATH,
                        "//*[contains(translate(text(),"
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                        "'download log')]"
                    ))
                )
            except Exception:
                print("  Warning: 'Download Log' still not found. Continuing ...")
        else:
            print("  Warning: 'Download Log' not found within 15 s. Continuing ...")

    # scroll to load enough entries
    target = limit if limit else None
    tag = f">={target}" if target else "all"
    print(f"Scrolling to load {tag} matches ...")
    loaded = scroll_to_load(driver, target)
    print(f"  Loaded {loaded} entries on page")

    # extract structured data
    matches = driver.execute_script(JS_EXTRACT_MATCHES)
    if not matches:
        print("  WARNING: No matches extracted.  Saving debug page ...")
        debug = LOG_DIR / "_debug_page.html"
        debug.parent.mkdir(parents=True, exist_ok=True)
        debug.write_text(driver.page_source, encoding='utf-8')
        print(f"  Saved to {debug}")
        return []

    print(f"  Extracted {len(matches)} match records")
    return matches[:limit] if limit else matches

# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD & EXTRACT
# ═══════════════════════════════════════════════════════════════════════════════

def transfer_cookies(driver):
    """Copy browser cookies into a requests.Session."""
    import requests as req
    s = req.Session()
    ua = driver.execute_script("return navigator.userAgent")
    s.headers.update({"User-Agent": ua})
    for c in driver.get_cookies():
        s.cookies.set(c['name'], c['value'], domain=c.get('domain', ''))
    return s


def download_via_requests(session, url, dest):
    """Stream-download a URL → dest file.  Returns True on success."""
    import requests as req
    try:
        r = session.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"  DL failed ({e})")
        return False


def download_via_click(driver, idx, timeout=30):
    """Click the idx-th download button and wait for a new file."""
    from selenium.webdriver.common.by import By

    existing = set(DL_TEMP.glob("*")) if DL_TEMP.exists() else set()

    btns = driver.find_elements(By.XPATH,
        "//*[contains(translate(text(),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'download log')]")
    if idx >= len(btns):
        return None

    driver.execute_script("arguments[0].scrollIntoView({block:'center'});",
                          btns[idx])
    time.sleep(0.4)
    btns[idx].click()

    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = set(DL_TEMP.glob("*")) if DL_TEMP.exists() else set()
        new = cur - existing
        done = [f for f in new
                if not f.suffix.lower() in ('.crdownload', '.part', '.tmp')]
        if done:
            return done[0]
        time.sleep(0.5)
    return None


def extract_gz(gz_path, out_path):
    """Decompress .gz → out_path.  Handles plain files too."""
    with open(gz_path, 'rb') as f:
        magic = f.read(2)

    if magic == b'\x1f\x8b':      # gzip
        with gzip.open(gz_path, 'rb') as fi, open(out_path, 'wb') as fo:
            shutil.copyfileobj(fi, fo)
    else:
        shutil.copy2(str(gz_path), str(out_path))
    return True


def download_all(driver, matches, force=False):
    """Download & extract logs for every match in the list."""
    import requests as req

    manifest = load_manifest()
    session  = transfer_cookies(driver)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DL_TEMP.mkdir(parents=True, exist_ok=True)

    downloaded, skipped, failed = [], 0, 0

    for i, m in enumerate(matches, 1):
        opp   = m.get('opponent', 'unknown')
        date  = m.get('date', '')
        chips = m.get('net_chips', 0)
        res   = m.get('result', 'UNK')
        mid   = f"{date}|{opp}|{chips}"   # dedup key

        # skip if already downloaded
        if mid in manifest['downloaded'] and not force:
            skipped += 1
            continue

        # build filename  e.g. 20260301-1955_vs_twig_LOSS_-70285.glog
        dstr  = parse_date(date)
        cstr  = f"+{chips}" if chips > 0 else str(chips)
        fname = f"{dstr}_vs_{sanitize(opp)}_{res}_{cstr}.glog"
        out   = LOG_DIR / fname

        tag = f"[{i}/{len(matches)}]"
        print(f"  {tag} {date:>25}  vs {opp:<18} {res:<5} {cstr:>8} ", end="", flush=True)

        ok = False
        url = m.get('download_url')

        # ── try requests (fast) ──────────────────────────────────────────
        if url and url.startswith('http'):
            tmp = DL_TEMP / f"match_{i}.gz"
            if download_via_requests(session, url, tmp):
                try:
                    extract_gz(tmp, out)
                    ok = True
                except Exception as e:
                    print(f"[extract err: {e}] ", end="")
                finally:
                    tmp.unlink(missing_ok=True)

        # ── fallback: click ──────────────────────────────────────────────
        if not ok:
            dl = download_via_click(driver, m.get('index', i-1))
            if dl:
                try:
                    extract_gz(dl, out)
                    ok = True
                except Exception as e:
                    print(f"[extract err: {e}] ", end="")
                finally:
                    if dl and dl.exists():
                        dl.unlink(missing_ok=True)

        if ok:
            print("OK")
            downloaded.append(str(out))
            manifest['downloaded'][mid] = {
                'file':      fname,
                'date':      date,
                'opponent':  opp,
                'result':    res,
                'net_chips': chips,
                'ts':        datetime.now().isoformat(),
            }
            save_manifest(manifest)
        else:
            print("FAIL")
            failed += 1

    # clean up temp dir
    if DL_TEMP.exists():
        shutil.rmtree(DL_TEMP, ignore_errors=True)

    print(f"\n  Done — {len(downloaded)} downloaded,  {skipped} skipped,  {failed} failed")
    return downloaded

# ═══════════════════════════════════════════════════════════════════════════════
# ANALYSIS INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════

def run_analysis(files=None, last_n=None, player=PLAYER_NAME):
    """Run analyze_logs_v2.py on the given (or discovered) log files."""
    if not ANALYZER.exists():
        print(f"  Analyzer not found: {ANALYZER}")
        return

    if files is None:
        files = sorted(glob.glob(str(LOG_DIR / "*.glog")))
        if last_n:
            files = files[-last_n:]

    if not files:
        print("  No log files to analyze.")
        return

    print(f"\n  Analyzing {len(files)} log files with analyze_logs_v2 ...")
    cmd = [sys.executable, str(ANALYZER)] + [str(f) for f in files] + \
          ["--player", player, "--no-plot"]
    subprocess.run(cmd)

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="IIT Pokerbots — match log scraper & analyzer")
    ap.add_argument("--last",     type=int, default=20,
                    help="Download last N matches (default 20)")
    ap.add_argument("--all",      action="store_true",
                    help="Download ALL matches")
    ap.add_argument("--analyze",  action="store_true",
                    help="Run analyzer after downloading")
    ap.add_argument("--analyze-only", action="store_true",
                    help="Skip download; analyze existing logs")
    ap.add_argument("--analyze-last", type=int, metavar="N",
                    help="When --analyze-only, analyze last N logs")
    ap.add_argument("--list",     action="store_true",
                    help="List matches on site without downloading")
    ap.add_argument("--opponent", type=str, default=None,
                    help="Only download matches vs this opponent")
    ap.add_argument("--losses-only", action="store_true",
                    help="Only download losses")
    ap.add_argument("--wins-only",   action="store_true",
                    help="Only download wins")
    ap.add_argument("--force",    action="store_true",
                    help="Re-download even if already in manifest")
    ap.add_argument("--player",   default=PLAYER_NAME,
                    help=f"Player name for analysis (default {PLAYER_NAME})")
    args = ap.parse_args()

    # ── analyze-only shortcut ────────────────────────────────────────────
    if args.analyze_only:
        run_analysis(last_n=args.analyze_last, player=args.player)
        return

    limit = None if args.all else args.last

    # ── launch browser ───────────────────────────────────────────────────
    driver = launch_browser()
    try:
        matches = scrape_matches(driver, limit)
        if not matches:
            print("No matches found.")
            return

        # ── optional filters ─────────────────────────────────────────────
        if args.opponent:
            pat = args.opponent.lower()
            matches = [m for m in matches
                       if pat in m.get('opponent', '').lower()]
            print(f"  Filtered to {len(matches)} matches vs '{args.opponent}'")

        if args.losses_only:
            matches = [m for m in matches if m.get('result') == 'LOSS']
            print(f"  Filtered to {len(matches)} losses")
        elif args.wins_only:
            matches = [m for m in matches if m.get('result') == 'WIN']
            print(f"  Filtered to {len(matches)} wins")

        if not matches:
            print("  No matches after filtering.")
            return

        # ── list mode ────────────────────────────────────────────────────
        if args.list:
            w, l, t = 0, 0, 0
            print(f"\n  {'#':>4}  {'Date':<27}  {'Opponent':<20}  "
                  f"{'Result':<7}  {'Net Chips':>10}")
            print("  " + "-" * 80)
            for i, m in enumerate(matches, 1):
                r = m.get('result', '?')
                c = m.get('net_chips', 0)
                cstr = f"+{c}" if c > 0 else str(c)
                print(f"  {i:>4}  {m.get('date','?'):<27}  "
                      f"{m.get('opponent','?'):<20}  {r:<7}  {cstr:>10}")
                if r == 'WIN':  w += 1
                elif r == 'LOSS': l += 1
                else: t += 1
            print(f"\n  Total: {len(matches)} matches — "
                  f"{w}W / {l}L / {t}T")
            return

        # ── download ─────────────────────────────────────────────────────
        downloaded = download_all(driver, matches, force=args.force)

    finally:
        driver.quit()
        print("  Browser closed.")

    # ── optional analysis ────────────────────────────────────────────────
    if args.analyze and downloaded:
        run_analysis(files=downloaded, player=args.player)


if __name__ == "__main__":
    main()
