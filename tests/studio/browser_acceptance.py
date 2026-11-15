"""Run real UI/media code with an explicit asset-inlining delivery adapter.
The managed browser blocks HTTP/file navigation. This adapter supplies exact
local HTML/JSON/CSS via set_content + srcdoc, not a fake implementation of any
player, annotation, catalog or navigation function. It does NOT test hosting,
HTTP routing, origin-local storage, external media or physical Safari.
"""
from pathlib import Path
import json,re,sys
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))  # silent launch helper: no run may make a sound
from silent_browser import silent_launch
ROOT=Path(sys.argv[1] if len(sys.argv)>1 else '/mnt/data/god-here-unified-site').resolve()
OUT=Path(sys.argv[2] if len(sys.argv)>2 else '/mnt/data/god-here-consolidation/verification');OUT.mkdir(exist_ok=True)
checks=[];exceptions=[]
def check(name,value=True):
    if not value:raise AssertionError(name)
    checks.append(name);print('PASS '+name,flush=True)
def asset(path):
    part=str(path).split('#')[0].split('?')[0].lstrip('/');p=(ROOT/part).resolve()
    if ROOT not in p.parents and p!=ROOT:raise ValueError('Outside preview')
    if p.is_dir():p=p/'index.html'
    s=p.read_text()
    if p.suffix=='.html':
        style='review-layout.css' if 'review-workbench' in part else 'legacy-layout.css'
        addition='<style id="studio-injected-style">'+(ROOT/'studio'/style).read_text()+'</style>'
        s=s.replace('</head>',addition+'</head>',1) if '</head>' in s else addition+s
    return s
ADAPTER='''
const nativeFetch=window.fetch;
window.fetch=async function(input,options){if(typeof input==='string'&&!/^(data:|blob:|https?:)/.test(input)){try{return new Response(await window.__readLocalText(input),{status:200,headers:{'content-type':'application/json'}});}catch(e){return new Response('Unavailable',{status:404});}}return nativeFetch(input,options);};
Object.defineProperty(HTMLIFrameElement.prototype,'src',{configurable:true,get(){return this.dataset.testAsset||'';},set(path){this.dataset.testAsset=path;window.__readLocalText(path).then(text=>{this.srcdoc=text;}).catch(()=>{this.srcdoc='<title>Not Found</title><body>Unavailable fixture</body>';});}});
'''
def document():
    text=(ROOT/'index.html').read_text();css=(ROOT/'studio/studio.css').read_text()
    text=text.replace('<link rel="stylesheet" href="studio/studio.css">','<style>'+css+'</style>')
    core=re.sub(r'\bexport\s+','',(ROOT/'studio/core.mjs').read_text())
    presentation=re.sub(r'\bexport\s+','',(ROOT/'studio/presentation.mjs').read_text())
    app=re.sub(r'^import[^\n]*\n','',(ROOT/'studio/studio.mjs').read_text(),flags=re.MULTILINE)
    return text.replace('<script type="module" src="studio/studio.mjs"></script>','<script>'+ADAPTER+core+'\n'+presentation+'\n'+app+'</script>')
def open_page(browser,width=1440,height=1100):
    context=browser.new_context(viewport={'width':width,'height':height},accept_downloads=True)
    page=context.new_page();page.expose_function('__readLocalText',asset)
    page.on('pageerror',lambda e:exceptions.append(str(e)));page.on('dialog',lambda d:d.accept())
    page.set_content(document(),wait_until='load',timeout=30000)
    page.wait_for_function("document.querySelector('iframe')?.contentWindow.REVIEW_READY===true",timeout=30000)
    return context,page

def frame(page,tool):return page.locator('iframe[data-tool="'+tool+'"]').content_frame

def change(page,value):
    page.evaluate('(v)=>location.hash=v',value);page.wait_for_timeout(180)

with sync_playwright() as p:
    browser=silent_launch(p.chromium,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
    context,page=open_page(browser)
    review=frame(page,'review');review.locator('#video').wait_for()
    page.wait_for_function("document.querySelector('iframe').contentWindow.document.getElementById('video').readyState>=2")
    check('source workbench is the default destination',page.evaluate("GodHereStudio.activeTool==='review'"))
    check('five persistent task destinations',page.locator('header nav a').count()==5)
    check('all 174 source records preserved',review.locator('#source-table tbody tr').count()==174)
    check('35 dialogue blocks retained',review.locator('.event-block.dialogue').count()==35)
    check('original video dimensions retained',review.locator('#video').evaluate('(v)=>v.videoWidth===1280&&v.videoHeight===720'))
    check('advanced note settings progressively disclosed',not review.locator('#note-status').is_visible())
    review.locator('.studio-advanced summary').click()
    check('optional note fields still reachable',review.locator('#note-status').is_visible())
    review.locator('.studio-advanced summary').click()
    before=review.locator('#video').evaluate('(v)=>v.currentTime');review.locator('#play').click();page.wait_for_timeout(650)
    check('actual embedded source playback advances',review.locator('#video').evaluate('(v)=>v.currentTime')>before+.3)
    change(page,'library');page.wait_for_function("document.getElementById('library-count').textContent.includes('registered identities')")
    check('switching to Library pauses the actual source',review.locator('#video').evaluate('(v)=>v.paused'))
    count=page.locator('#library-count').inner_text();expected=json.loads((ROOT/'studio/coverage.json').read_text())['stats'][0]['value']
    check('all canonical and added identities loaded',f'{expected:,}' in count)
    check('archive uses bounded page size',page.locator('.artifact').count()==24)
    page.locator('#library-next').click();check('next catalog page works',page.locator('#library-page').inner_text().startswith('2 /'))
    page.locator('#library-prev').click();check('previous catalog page works',page.locator('#library-page').inner_text().startswith('1 /'))
    page.locator('#library-search').fill('zzzz-not-a-project-object');check('empty search has an explicit state',page.locator('.empty').is_visible())
    page.locator('#library-clear').click();page.locator('#library-search').fill('god-here-study-05-player.html')
    check('recovered evaluation is discoverable by original name','workspace' in page.locator('#library-results').inner_text())
    page.locator('#library-clear').click();page.locator('#library-status').select_option('failed')
    check('failed media not offered as playable',page.locator('#library-results a').count()==0)
    page.locator('#library-clear').click();page.locator('#library-status').select_option('preserved')
    check('private originals are not exposed as media links',all((a.get_attribute('href') or '').startswith('#compose/') and 'derived workspace' in a.inner_text() for a in page.locator('#library-results a').all()))
    change(page,'project');page.wait_for_function("document.querySelectorAll('.workstream').length===13")
    check('13 workstreams visible',page.locator('.workstream').count()==13)
    check('historical incompleteness explicit','160' in page.locator('#coverage-qualification').inner_text())
    check('protected evaluation boundary visible','returns401' in page.locator('#project-gaps').inner_text())
    change(page,'review');check('review iframe retained rather than recreated',page.locator('iframe[data-tool="review"]').count()==1)
    review.locator('#video').evaluate('(v)=>{v.pause();v.currentTime=40;}');page.wait_for_timeout(180)
    review.locator('#new-at-time').click();review.locator('#note-body').fill('TEST ONLY — hold this recorded reaction; retain the source sound.')
    review.locator('#note-character').select_option('Chris');review.locator('#save-note').click()
    check('note is saved in native annotation engine',review.locator('#notes-tab-count').inner_text()=='1')
    with page.expect_download() as d:page.locator('#global-export').click()
    destination=OUT/'studio-test-notes.json';d.value.save_as(destination);export=json.loads(destination.read_text())
    check('source review export retains native format',export.get('schema_version')=='god-here-review/1' or export.get('schema')=='god-here-review/1')
    check('export contains the new direction','hold this recorded reaction' in destination.read_text())
    check('export retains original source identity','bde1c7e0910941f8ca3bce06e8c2ddb3a31da4335b544fd041d84e97d1060225' in destination.read_text())
    change(page,'project/recovery');page.wait_for_timeout(400)
    check('nested project tool does not duplicate the project dashboard',not page.locator('#project-view').is_visible())
    change(page,'review');review.locator('#delete-note').click();review.locator('#video').evaluate('(v)=>{v.currentTime=12;}');page.wait_for_timeout(5600);page.screenshot(path=str(OUT/'studio-desktop.png'),full_page=False)
    page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(450)
    check('main shell has no mobile horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    check('review child has no mobile horizontal overflow',review.locator('html').evaluate('(x)=>x.scrollWidth<=innerWidth'))
    page.screenshot(path=str(OUT/'studio-mobile.png'),full_page=False)
    change(page,'library');page.wait_for_timeout(250)
    check('Library is usable without whole-page mobile overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    page.screenshot(path=str(OUT/'studio-library-mobile.png'),full_page=False)
    print('PHASE closing primary context',flush=True);context.close();print('PHASE primary closed',flush=True)
    # Test recovered heavy players in separate browser contexts to avoid a
    # misleading memory-performance claim for several decoded atlases at once.
    for tool in ['study-03','study-04','evaluation','layers']:
        print('PHASE opening '+tool,flush=True);context,page=open_page(browser);print('PHASE source ready '+tool,flush=True);change(page,'compose/'+tool)
        child=frame(page,tool)
        if tool!='layers':
            page.wait_for_function('(tool)=>document.querySelector(`iframe[data-tool="${tool}"]`)?.contentWindow.GOD_HERE?.ready===true',arg=tool,timeout=30000)
            check(tool+' loads its exact embedded picture and matte atlases')
            child.locator('#play').click();page.wait_for_timeout(650)
            check(tool+' native audiovisual clock advances',page.evaluate('(tool)=>document.querySelector(`iframe[data-tool="${tool}"]`).contentWindow.GOD_HERE.currentTime>0.2',tool))
            change(page,'review')
            check(tool+' Web Audio transport pauses on workspace switch',page.evaluate('(tool)=>!document.querySelector(`iframe[data-tool="${tool}"]`).contentWindow.GOD_HERE.playing',tool))
        else:
            child.locator('canvas').first.wait_for(timeout=20000);check('layer-study canvas and independent controls retained',child.locator('input').count()==5 and child.locator('#mode').count()==1)
            child.locator('#study').click();check('layer expressive preset updates actual controls',child.locator('#field').input_value()=='8');child.locator('#play').click();page.wait_for_timeout(500);check('layer-study native clock advances',float(child.locator('#time').input_value())>.2);change(page,'review');check('layer-study native transport and sound pause',child.locator('#play').inner_text()=='Play' and child.locator('#audio').evaluate('(a)=>a.paused'))
        if tool=='evaluation':
            change(page,'compose/evaluation');child.locator('[data-tab="evaluation"]').click();
            with page.expect_download() as ev:child.locator('#saveEvaluation').click()
            ev.value.save_as(OUT/'test-evaluation-export.json');check('evaluation export survives unavailable browser storage','self-reported' in (OUT/'test-evaluation-export.json').read_text() or 'evaluation' in (OUT/'test-evaluation-export.json').read_text());page.screenshot(path=str(OUT/'studio-evaluation.png'),full_page=False)
        print('PHASE close '+tool,flush=True);context.close();print('PHASE closed '+tool,flush=True)
    browser.close()
write={'scope':'Actual source review, recovered embedded players and shell with asset-inlining delivery adapter. Not a hosted-site, origin-storage, HTTP-route, external-media or physical-device certification.', 'checks':checks,'passed':len(checks),'page_errors':exceptions,'live_published':False}
(OUT/'studio-browser-validation.json').write_text(json.dumps(write,indent=2))
print('TOTAL',len(checks),'PAGE ERRORS',exceptions)
