import urllib.request, re, json, datetime

base = datetime.date(1899, 12, 30)
d = datetime.date(2024, 1, 9)
st = (d - base).days
url = f'https://economictimes.indiatimes.com/archivelist/year-{d.year},month-{d.month},starttime-{st}.cms'

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}

print(f"Testing URL from GitHub Actions Runner: {url}")
try:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8', errors='ignore')
        links = set(re.findall(r'href=["\x27]([^"\x27]*articleshow[^"\x27]*\.cms)["\x27]', html))
        print(f"HTTP Status: {r.status} | Total links found: {len(links)}")
        
        if links:
            test_art = list(links)[0]
            if test_art.startswith('/'):
                test_art = 'https://economictimes.indiatimes.com' + test_art
            print(f"Testing Article URL: {test_art}")
            req_art = urllib.request.Request(test_art, headers=headers)
            with urllib.request.urlopen(req_art, timeout=15) as r2:
                art_html = r2.read().decode('utf-8', errors='ignore')
                ld = re.findall(r'<script type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>', art_html, re.DOTALL)
                for s in ld:
                    try:
                        item = json.loads(s)
                        if isinstance(item, list): item = item[0]
                        if item.get('@type') == 'NewsArticle':
                            print(f"SUCCESS! Extracted Headline: {item.get('headline')}")
                            print(f"Body length: {len(item.get('articleBody', ''))} characters")
                            break
                    except: pass
            print("CONNECTIVITY TEST: PASSED! GITHUB ACTIONS IS ALLOWED!")
except Exception as e:
    print(f"CONNECTIVITY TEST: FAILED! Error: {e}")
    raise
