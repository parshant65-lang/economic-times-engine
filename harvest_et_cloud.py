import os
import sys
import re
import time
import json
import shutil
import datetime
import subprocess
import html as html_lib
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

VAULT_LOCAL_DIR = "/tmp/et_cloud_vault"
BASE_DATE = datetime.date(1899, 12, 30)
MAX_WORKERS = 14
BUFFER_LIMIT = 1000
MAX_SESSION_SECONDS = 4 * 3600  # 4 hours per run

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def clean_html(raw_html):
    if not raw_html:
        return ""
    clean = re.sub(r"<[^>]+>", "", raw_html)
    clean = html_lib.unescape(clean)
    return " ".join(clean.split())

def get_token():
    return os.environ.get("VAULT_PAT", "").strip()

def get_vault_repo_for_date(target_date):
    """
    Volume 1: 2001 to 2012
    Volume 2: 2013 to 2019
    Volume 3: 2020 to August 23, 2026 (~500 MB)
    Volume 4: August 24, 2026 onwards
    """
    if target_date.year <= 2012:
        return "economic-times-vault-1"
    elif target_date.year <= 2019:
        return "economic-times-vault-2"
    elif target_date < datetime.date(2026, 8, 24):
        return "economic-times-vault-3"
    else:
        return "economic-times-vault-4"

def get_vault_repo_for_year(year):
    if year <= 2012:
        return "economic-times-vault-1"
    elif year <= 2019:
        return "economic-times-vault-2"
    elif year <= 2025:
        return "economic-times-vault-3"
    else:
        return "economic-times-vault-4"

def fetch_day_archive_links(target_date):
    st = (target_date - BASE_DATE).days
    url = f"https://economictimes.indiatimes.com/archivelist/year-{target_date.year},month-{target_date.month},starttime-{st}.cms"
    
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode('utf-8', errors='ignore')
                raw_links = set(re.findall(r'href=["\x27]([^"\x27]*articleshow[^"\x27]*\.cms)["\x27]', html))
                
                clean_links = []
                for l in raw_links:
                    if l.startswith("/"):
                        clean_links.append("https://economictimes.indiatimes.com" + l)
                    elif l.startswith("http"):
                        clean_links.append(l)
                return clean_links
        except Exception as e:
            time.sleep(1 + attempt)
            if attempt == 3:
                log(f"[WARN] Failed fetching archive for {target_date} (st={st}): {e}")
                return []
    return []

def fetch_single_article(art_url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(art_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode('utf-8', errors='ignore')
                
                # 1. Search JSON-LD NewsArticle
                ld_blocks = re.findall(r'<script type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>', html, re.DOTALL)
                for s in ld_blocks:
                    try:
                        item = json.loads(s)
                        if isinstance(item, list):
                            item = item[0]
                        if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                            body = item.get("articleBody", "")
                            if body:
                                paras = [p.strip() for p in body.split("\n") if len(p.strip()) > 20]
                                if not paras:
                                    paras = [body]
                                
                                m_id = re.search(r'articleshow/(\d+)\.cms', art_url)
                                art_id = m_id.group(1) if m_id else str(hash(art_url))
                                
                                author_obj = item.get("author", {})
                                author_name = author_obj.get("name", "") if isinstance(author_obj, dict) else str(author_obj)
                                
                                return {
                                    "id": art_id,
                                    "date": item.get("datePublished", ""),
                                    "title": clean_html(item.get("headline", "")),
                                    "link": art_url,
                                    "author": author_name,
                                    "section": item.get("articleSection", "News"),
                                    "word_count": len(body.split()),
                                    "paragraphs": paras
                                }
                    except:
                        pass
                
                # 2. Fallback
                m_title = re.search(r'<title>(.*?)</title>', html)
                title = clean_html(m_title.group(1).split(" - ")[0]) if m_title else ""
                
                p_matches = re.findall(r'<p[^>]*>(.*?)</p>', html, re.DOTALL)
                clean_p = [clean_html(p) for p in p_matches if len(clean_html(p)) > 30 and not any(k in clean_html(p).lower() for k in ["subscribe", "advertisement", "sign in"])]
                
                if clean_p:
                    m_id = re.search(r'articleshow/(\d+)\.cms', art_url)
                    art_id = m_id.group(1) if m_id else str(hash(art_url))
                    return {
                        "id": art_id,
                        "date": "",
                        "title": title,
                        "link": art_url,
                        "author": "ET Online",
                        "section": "News",
                        "word_count": sum(len(x.split()) for x in clean_p),
                        "paragraphs": clean_p
                    }
                return None
        except Exception:
            time.sleep(0.5)
    return None

def push_batch_to_vault(repo_name, articles_batch, checkpoint_updates):
    if not articles_batch and not checkpoint_updates:
        return

    token = get_token()
    repo_url = f"https://x-access-token:{token}@github.com/parshant65-lang/{repo_name}.git"

    current_remote = None
    if os.path.exists(VAULT_LOCAL_DIR) and os.path.exists(os.path.join(VAULT_LOCAL_DIR, ".git")):
        try:
            res = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=VAULT_LOCAL_DIR, capture_output=True, text=True)
            if repo_name in res.stdout:
                current_remote = repo_name
        except:
            pass

    try:
        if current_remote == repo_name:
            log(f"[SYNC] Pulling latest {repo_name}...")
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=VAULT_LOCAL_DIR, check=True, capture_output=True)
        else:
            if os.path.exists(VAULT_LOCAL_DIR):
                shutil.rmtree(VAULT_LOCAL_DIR, ignore_errors=True)
            log(f"[SYNC] Cloning {repo_name} for batch push...")
            subprocess.run(["git", "clone", "--depth", "1", repo_url, VAULT_LOCAL_DIR], check=True, capture_output=True)

        by_year_month = {}
        for a in articles_batch:
            dt = a.get("date", "")
            try:
                y, m = dt.split("-")[0], dt.split("-")[1]
            except:
                cur_d = checkpoint_updates.get("current_date", "2001-01-01")
                y, m = cur_d.split("-")[0], cur_d.split("-")[1]
            by_year_month.setdefault((y, m), []).append(a)

        for (y, m), m_arts in sorted(by_year_month.items()):
            m_dir = os.path.join(VAULT_LOCAL_DIR, "data", "articles", y, m)
            os.makedirs(m_dir, exist_ok=True)
            m_file = os.path.join(m_dir, f"articles_{y}_{m}.json")

            existing = []
            if os.path.exists(m_file):
                try:
                    with open(m_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except:
                    existing = []

            seen_ids = {x.get("id") for x in existing if "id" in x}
            for a in m_arts:
                if a.get("id") not in seen_ids:
                    existing.append(a)
                    seen_ids.add(a.get("id"))

            existing.sort(key=lambda x: x.get("date", ""))
            with open(m_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=1)

        cp_file = os.path.join(VAULT_LOCAL_DIR, "checkpoint.json")
        cp = {}
        if os.path.exists(cp_file):
            try:
                with open(cp_file, "r", encoding="utf-8") as f:
                    cp = json.load(f)
            except:
                cp = {}

        cp.update(checkpoint_updates)
        cp["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(cp, f, indent=2)

        subprocess.run(["git", "config", "user.name", "GitHub Action Runner"], cwd=VAULT_LOCAL_DIR, check=True)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=VAULT_LOCAL_DIR, check=True)
        subprocess.run(["git", "add", "."], cwd=VAULT_LOCAL_DIR, check=True)

        new_count = len(articles_batch)
        d_str = checkpoint_updates.get("current_date", "")
        msg = f"[ET-CLOUD] Ingest {d_str} (+{new_count} articles, Total Vault: {cp.get('total_articles', 0)}) [skip ci]"
        subprocess.run(["git", "commit", "-m", msg], cwd=VAULT_LOCAL_DIR, check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=VAULT_LOCAL_DIR, capture_output=True)
        res_push = subprocess.run(["git", "push", "origin", "main"], cwd=VAULT_LOCAL_DIR, capture_output=True, text=True)

        if res_push.returncode == 0:
            log(f"[SYNC SUCCESS] ✅ Pushed {new_count} articles to {repo_name}!")
        else:
            log(f"[SYNC ERROR] {res_push.stderr.strip()}")

    except Exception as e:
        log(f"[SYNC EXCEPTION] {e}")

def main():
    log("=" * 65)
    log("🚀 ECONOMIC TIMES HIGH-SPEED CLOUD HARVESTER (GITHUB ACTIONS)")
    log("=" * 65)

    token = get_token()
    if not token:
        log("ERROR: VAULT_PAT token missing!")
        sys.exit(1)

    cp = {}
    active_vault = "economic-times-vault-1"

    import base64
    for v_name in ["economic-times-vault-4", "economic-times-vault-3", "economic-times-vault-2", "economic-times-vault-1"]:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/parshant65-lang/{v_name}/contents/checkpoint.json",
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                c_temp = json.loads(base64.b64decode(data["content"]))
                if c_temp.get("rollover_status") == "ARCHIVED_FULL":
                    log(f"[ARCHIVED] Vault {v_name} is marked ARCHIVED_FULL. Skipping...")
                    continue
                if c_temp.get("current_date"):
                    cp = c_temp
                    active_vault = v_name
                    log(f"Found active checkpoint in {v_name}: Date {cp.get('current_date')}")
                    break
        except Exception:
            pass

    start_date_str = cp.get("current_date", "2001-01-01")
    cur_y, cur_m, cur_d = [int(x) for x in start_date_str.split("-")]
    current_date = datetime.date(cur_y, cur_m, cur_d)
    end_date = datetime.date(2026, 11, 12)

    total_articles = cp.get("total_articles", 0)
    buffered = []

    log(f"Resuming Cloud Harvester at Date: {current_date} | Total Vault Articles: {total_articles}")
    start_time = time.time()

    while current_date <= end_date:
        if time.time() - start_time > MAX_SESSION_SECONDS:
            log("Session time limit reached (~4h). Flushing and preparing next run...")
            break

        target_vault = get_vault_repo_for_date(current_date)
        
        day_links = fetch_day_archive_links(current_date)
        log(f"[{current_date}] Found {len(day_links)} articles in daily archive")

        day_articles = []
        if day_links:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(fetch_single_article, url): url for url in day_links}
                for f in as_completed(futures):
                    res = f.result()
                    if res:
                        day_articles.append(res)

        buffered.extend(day_articles)
        total_articles += len(day_articles)
        log(f"  ✓ Processed {len(day_articles)}/{len(day_links)} articles for {current_date} (Buffered: {len(buffered)})")

        is_month_end = (current_date + datetime.timedelta(days=1)).month != current_date.month
        is_year_end = (current_date + datetime.timedelta(days=1)).year != current_date.year

        if len(buffered) >= BUFFER_LIMIT or is_month_end or is_year_end:
            cp_updates = {
                "current_date": current_date.strftime("%Y-%m-%d"),
                "current_year": current_date.year,
                "current_month": current_date.month,
                "current_day": current_date.day,
                "total_articles": total_articles
            }
            if is_year_end:
                comp = cp.get("completed_years", [])
                if current_date.year not in comp:
                    comp.append(current_date.year)
                cp_updates["completed_years"] = comp

            push_batch_to_vault(target_vault, buffered, cp_updates)
            cp.update(cp_updates)
            buffered.clear()

        current_date += datetime.timedelta(days=1)

    if buffered:
        cp_updates = {
            "current_date": current_date.strftime("%Y-%m-%d"),
            "total_articles": total_articles
        }
        push_batch_to_vault(target_vault, buffered, cp_updates)

    log(f"Session complete! Latest Date: {current_date} | Total Articles: {total_articles}")

if __name__ == "__main__":
    main()
