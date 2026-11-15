#!/usr/bin/env python3
"""Read-only, bounded capture of God-here's two already-public sites.
Never publishes, submits forms, changes scores, or retrieves private archives.
"""
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
from urllib.request import Request, urlopen
from html.parser import HTMLParser
from collections import deque
import hashlib, json, re, sys, time

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run as a script, tools/ is the import root
from silent_browser import silent_launch

OUT = Path('audit-output')
MAIN = 'https://god-here-five-realities.ajpadavano.chatgpt.site/'
EVAL = 'https://god-here-evaluation.ajpadavano.chatgpt.site/'
ALLOWED = {urlsplit(MAIN).netloc, urlsplit(EVAL).netloc}
ROUTES = [('', MAIN), ('audio', MAIN+'review.html'), ('fx-watch', MAIN+'fx/watch.html'),
          ('fx-rack', MAIN+'fx/'), ('room-tone', MAIN+'room-tone/'),
          ('color', MAIN+'color-studies/'), ('recovery', MAIN+'recovery/'), ('evaluation', EVAL)]
MAX_BYTES = 24 * 1024 * 1024
MAX_TOTAL = 220 * 1024 * 1024
SKIP = {'.mp4','.mov','.mkv','.wav','.flac','.mp3','.aac','.zip','.tar','.gz','.bin','.ttf','.otf','.woff','.woff2','.onnx','.pt','.safetensors'}

def allowed(url):
    u = urlsplit(url)
    return u.scheme == 'https' and u.netloc in ALLOWED and not u.username and not u.password

def eligible(url):
    return allowed(url) and Path(urlsplit(url).path).suffix.lower() not in SKIP

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.refs = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('src','href','poster') and value:
                self.refs.append(value)

def relative_path(url):
    u = urlsplit(url); p = unquote(u.path).lstrip('/')
    if not p or p.endswith('/'): p += 'index.html'
    if '..' in Path(p).parts: raise ValueError('Unsafe path')
    path = Path(p)
    if u.query:
        digest = hashlib.sha256(u.query.encode('utf-8')).hexdigest()
        path = path.with_name(f'{path.stem}--query-{digest}{path.suffix}')
    return Path('sites') / u.netloc / path

def references(text, content_type):
    if 'html' in content_type:
        parser = Links(); parser.feed(text); return parser.refs
    if any(s in content_type for s in ('javascript','json','css')):
        return re.findall(r'[\"\x27]([^\"\x27\n<>]+\.(?:json|mjs|js|css|html|jpg|jpeg|png|svg|mp4|wav|flac)(?:\?[^\"\x27]*)?)[\"\x27]', text)
    return []

def capture_sources():
    queue = deque((url, 0) for _, url in ROUTES); seen = set(); receipts = []; total = 0
    while queue and len(seen) < 260 and total < MAX_TOTAL:
        url, depth = queue.popleft(); url = url.split('#',1)[0]
        if url in seen or not eligible(url): continue
        seen.add(url); row = {'url':url,'depth':depth,'started_at':time.time()}
        try:
            req = Request(url, headers={'User-Agent':'GodHere-Owner-ReadOnly-Audit/1.0'})
            with urlopen(req, timeout=20) as r:
                if not allowed(r.url): raise ValueError('Redirect left approved sites')
                data = r.read(MAX_BYTES+1)
                if len(data)>MAX_BYTES: raise ValueError('Per-file byte limit exceeded')
                ct = r.headers.get('content-type',''); row.update(status=r.status,content_type=ct,final_url=r.url)
            dest = OUT/relative_path(url); dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(data)
            total += len(data); row.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),local_path=str(dest.relative_to(OUT)))
            if depth < 4:
                refs = references(data.decode('utf-8',errors='replace'),ct)
                row['references'] = [urljoin(url,v) for v in refs if not v.startswith(('data:','javascript:','mailto:'))]
                for ref in row['references']:
                    if eligible(ref): queue.append((ref,depth+1))
        except Exception as exc:
            row['error'] = str(exc)
        receipts.append(row)
        print(json.dumps({k:v for k,v in row.items() if k!='references'}),flush=True)
    (OUT/'source-capture.json').write_text(json.dumps({'scope':'bounded read-only capture; not an exhaustive history audit','max_urls':260,'max_total_bytes':MAX_TOTAL,'captured_bytes':total,'queue_remaining':len(queue),'records':receipts},indent=2))

def capture_browser():
    from playwright.sync_api import sync_playwright
    reports = []
    with sync_playwright() as p:
        browser = silent_launch(p.chromium)
        for name, url in ROUTES:
            name = name or 'main'; page = browser.new_page(viewport={'width':1440,'height':1000})
            errors=[]; bad=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('response',lambda r:bad.append({'url':r.url,'status':r.status}) if r.status>=400 else None)
            page.on('dialog',lambda d:d.dismiss())
            row = {'route':name,'url':url,'browser':'Chromium','errors':errors,'http_failures':bad}
            try:
                response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
                row['status']=response.status if response else None
                page.wait_for_timeout(2500)
                page.screenshot(path=str(OUT/(name+'-desktop-before.png')),full_page=False)
                row['title']=page.title()
                row['controls']=page.locator('button,input,select,a').evaluate_all('(xs)=>xs.map(x=>({tag:x.tagName,id:x.id,label:(x.innerText||x.getAttribute("aria-label")||x.name||"").slice(0,180),disabled:x.disabled||false,href:x.getAttribute("href"),type:x.type}))')
                row['media_before']=page.locator('video,audio').evaluate_all('(xs)=>xs.map(x=>({src:x.currentSrc,readyState:x.readyState,time:x.currentTime,duration:Number.isFinite(x.duration)?x.duration:null,error:x.error&&x.error.message}))')
                selector = '#start' if name=='main' else '#play'
                if page.locator(selector).count() and page.locator(selector).first.is_visible():
                    page.locator(selector).first.click(timeout=5000); page.wait_for_timeout(2500)
                    row['play_probe']={'selector':selector,'media':page.locator('video,audio').evaluate_all('(xs)=>xs.map(x=>({src:x.currentSrc,time:x.currentTime,paused:x.paused,readyState:x.readyState,error:x.error&&x.error.message}))')}
                    page.screenshot(path=str(OUT/(name+'-desktop-after-play.png')),full_page=False)
                    page.locator('video,audio').evaluate_all('(xs)=>xs.forEach(x=>x.pause())')
                page.set_viewport_size({'width':390,'height':844}); page.wait_for_timeout(400)
                row['mobile_overflow']=page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})')
                page.screenshot(path=str(OUT/(name+'-mobile.png')),full_page=False)
                row['rendered_html_path']=name+'-rendered.html'
                (OUT/row['rendered_html_path']).write_text(page.content())
            except Exception as exc:
                row['probe_error']=str(exc)
            reports.append(row);page.close()
        browser.close()
    (OUT/'browser-audit.json').write_text(json.dumps({'scope':'8 current routes; limited playback probes, not exhaustive button acceptance or physical Safari tests','routes':reports},indent=2))

if __name__=='__main__':
    assert allowed(MAIN) and allowed(EVAL)
    assert not allowed('https://example.com/') and not allowed('https://god-here-five-realities.ajpadavano.chatgpt.site.evil.invalid/')
    assert not eligible(MAIN+'private.zip') and not eligible(MAIN+'font.ttf')
    assert str(relative_path(MAIN))=='sites/god-here-five-realities.ajpadavano.chatgpt.site/index.html'
    OUT.mkdir(exist_ok=True)
    capture_sources()
    capture_browser()
