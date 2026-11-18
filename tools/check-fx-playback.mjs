/**
 * Browser-level recovery regressions for the god-here FX workbench.
 *
 * Serves a local, read-only snapshot of .mjs files with the real media assets.
 * No production browser, Site API, or source files are modified. Worker faults
 * and metadata-only video readiness are injected at public browser boundaries.
 *
 * Usage: node tools/check-fx-playback.mjs [dist-or-web-root] [report-label]
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {silentLaunch, assertNoClosedShadowRoots} from './silent-browser.mjs';

const require=createRequire(import.meta.url);
const {webkit}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
const root=path.resolve(process.argv[2]||fileURLToPath(new URL('../web/',import.meta.url)));
const label=(process.argv[3]||'latest').replace(/[^a-zA-Z0-9_-]/g,'-');
const out=path.resolve(process.env.FX_QA_OUTPUT_DIR||fileURLToPath(new URL('../projects/god-here/fx/evidence/',import.meta.url)));
fs.mkdirSync(out,{recursive:true});
const report={started:new Date().toISOString(),root,label,browser:'Headless Playwright WebKit; public browser API fault injection; no physical iPhone claim',checks:[]};
const sourceSnapshot=new Map();
function snapshotModules(dir){for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,entry.name);if(entry.isDirectory())snapshotModules(file);else if(entry.name.endsWith('.mjs'))sourceSnapshot.set(file,fs.readFileSync(file));}}
snapshotModules(path.join(root,'fx'));
report.appSha256=createHash('sha256').update(sourceSnapshot.get(path.join(root,'fx/app.mjs'))).digest('hex');
const mime={'.mjs':'text/javascript','.js':'text/javascript','.css':'text/css','.html':'text/html','.json':'application/json','.wav':'audio/wav','.png':'image/png','.jpg':'image/jpeg','.mp4':'video/mp4'};
const server=http.createServer(async(req,res)=>{
  try{
    let file=path.resolve(root,'.'+decodeURIComponent(new URL(req.url,'http://localhost').pathname));
    if(file!==root&&!file.startsWith(root+path.sep)){res.writeHead(403).end();return;}
    let stat=await fs.promises.stat(file);if(stat.isDirectory()){file=path.join(file,'index.html');stat=await fs.promises.stat(file);}
    const cached=sourceSnapshot.get(file),size=cached?.byteLength??stat.size;
    const headers={'Content-Type':mime[path.extname(file)]||'application/octet-stream','Accept-Ranges':'bytes','Cache-Control':'no-cache'};
    let start=0,end=size-1,status=200;
    if(req.headers.range){const match=/^bytes=(\d*)-(\d*)$/.exec(req.headers.range);if(!match||!match[1]&&!match[2]){res.writeHead(416,{'Content-Range':`bytes */${size}`}).end();return;}if(!match[1])start=Math.max(0,size-Number(match[2]));else{start=Number(match[1]);if(match[2])end=Math.min(end,Number(match[2]));}if(start>=size||end<start){res.writeHead(416,{'Content-Range':`bytes */${size}`}).end();return;}status=206;headers['Content-Range']=`bytes ${start}-${end}/${size}`;}
    headers['Content-Length']=end-start+1;res.writeHead(status,headers);
    if(req.method==='HEAD'){res.end();return;}
    if(cached){res.end(cached.subarray(start,end+1));return;}
    fs.createReadStream(file,{start,end}).on('error',()=>res.destroy()).pipe(res);
  }catch{res.writeHead(404).end('Not found');}
});
await new Promise((resolve,reject)=>{server.on('error',reject);server.listen(0,'127.0.0.1',resolve);});
const origin=`http://127.0.0.1:${server.address().port}`;
let browser;
const reportPath=path.join(out,`playback-recovery-${label}.json`);
const save=()=>fs.writeFileSync(reportPath,JSON.stringify(report,null,2)+'\n');

/** All mutable harness controls are in the test page, separate from app state. */
function injectBoundaries(options){
  const NativeWorker=window.Worker;
  const nativePost=NativeWorker.prototype.postMessage;
  const nativeTerminate=NativeWorker.prototype.terminate;
  const qa=window.__qa={hold:false,held:[],workers:[],audioStarts:[],previewPlays:0,videoGate:!!options.videoGate,videoUnlocked:false};
  window.Worker=class extends NativeWorker{
    constructor(...args){super(...args);qa.workers.push(this);}
    postMessage(data,transfer){if(data?.type==='render'&&qa.hold){qa.held.push({worker:this,data:structuredClone(data),transfer});return;}return nativePost.call(this,data,transfer);}
  };
  qa.release=()=>{qa.hold=false;const held=qa.held.splice(0);for(const item of held)nativePost.call(item.worker,item.data,item.transfer);return held.length;};
  qa.crash=()=>{const item=qa.held.shift();if(!item)throw Error('The test must hold an actual render before crashing its worker.');qa.hold=false;qa.held=qa.held.filter(x=>x.worker!==item.worker);item.worker.dispatchEvent(new ErrorEvent('error',{message:'QA injected audio worker failure',cancelable:true}));nativeTerminate.call(item.worker);};
  const nativeStart=AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start=function(...args){qa.audioStarts.push({at:performance.now(),offset:args[1]||0});return nativeStart.apply(this,args);};
  const ready=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'readyState');
  Object.defineProperty(HTMLMediaElement.prototype,'readyState',{...ready,get(){if(this.id==='rendered-picture'&&qa.videoGate&&!qa.videoUnlocked)return 1;return ready.get.call(this);}});
  const nativePlay=HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play=function(...args){if(this.id==='rendered-picture'){qa.previewPlays++;qa.videoUnlocked=true;}return nativePlay.apply(this,args);};
  if(options.saved!==undefined)localStorage.setItem('god-here-fx-patch-v1',JSON.stringify(options.saved));
}
async function get(page){return page.evaluate(()=>({status:window.fxWorkbench?.getStatus(),message:document.querySelector('#status')?.textContent,playText:document.querySelector('#play')?.textContent,retry:document.querySelector('#audio-retry')?{hidden:document.querySelector('#audio-retry').hidden,disabled:document.querySelector('#audio-retry').disabled}:null,workerCount:window.__qa.workers.length,held:window.__qa.held.length,audioStarts:window.__qa.audioStarts.length,previewPlays:window.__qa.previewPlays}));}
async function settled(page){await page.waitForFunction(()=>{const s=window.fxWorkbench?.getStatus();return s?.ready&&!s.audioBusy&&s.audioRevision>=0&&s.audioRevision===s.audioDesired;},null,{timeout:60000});}
async function fresh(options={}){
  const context=await browser.newContext({viewport:{width:390,height:844}});
  await context.addInitScript(injectBoundaries,options);
  const page=await context.newPage();page.setDefaultTimeout(10000);
  const pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
  await page.goto(origin+'/fx/',{waitUntil:'load'});
  await settled(page);
  return {page,context,pageErrors};
}
async function heldEdit(page){
  await page.locator('[data-module="gate"]').click();await settled(page);
  await page.evaluate(()=>window.__qa.hold=true);
  await page.locator('.module-card').first().locator('[data-parameter="intensity"]').fill('0.43');
  await page.waitForFunction(()=>window.__qa.held.length===1&&window.fxWorkbench.getStatus().audioBusy);
}
async function check(name,fn){
  const result={name,started:new Date().toISOString()};report.checks.push(result);
  console.log('CHECK',name);
  try{await fn(result);result.pass=true;console.log('PASS',name);}catch(error){result.pass=false;result.error=error.stack;console.log('FAIL',name,error.message);}
  result.finished=new Date().toISOString();save();
}

try{
  // AV #186 pre-flight: a closed declarative shadow root would hide media no page script can reach, so the
  // served FX page is refused here, before any browser launches, instead of going audible.
  const fxPage=path.join(root,'fx/index.html');
  if(fs.existsSync(fxPage))assertNoClosedShadowRoots(fs.readFileSync(fxPage,'utf8'),fxPage);
  browser=await silentLaunch(webkit,{headless:true,...(process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE}:{})});
  await check('Cancel a pending Play; later worker completion never starts audio',async result=>{
    const {page,context,pageErrors}=await fresh();
    try{
      await heldEdit(page);await page.locator('#play').click();
      await page.waitForFunction(()=>/Preparing|render|sound/i.test(document.querySelector('#status').textContent)||window.fxWorkbench.getStatus().playPending===true);
      result.pending=await get(page);
      await page.locator('#play').click();
      result.canceled=await get(page);
      result.released=await page.evaluate(()=>window.__qa.release());await settled(page);
      // A DOM-to-animation round trip observes the awaited Play continuations.
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      result.afterCompletion=await get(page);result.pageErrors=pageErrors;
      assert.equal(result.pending.status.playPending,true,'Play must expose its pending state');
      assert.match(result.pending.playText,/Cancel|Pause/,'The transport must offer cancellation while sound is preparing');
      assert.equal(result.canceled.status.playPending,false,'The second tap must cancel pending playback');
      assert.equal(result.afterCompletion.status.playing,false,'A canceled playback request must stay stopped');
      assert.equal(result.afterCompletion.audioStarts,0,'No stale async Play may start a buffer after cancellation');
      assert.deepEqual(pageErrors,[]);
    }finally{await context.close();}
  });
  await check('Worker failure clears waiters; reference works; Retry restarts expressive rendering',async result=>{
    const {page,context,pageErrors}=await fresh();
    try{
      await heldEdit(page);await page.locator('#play').click();
      await page.waitForFunction(()=>/Preparing|render|sound/i.test(document.querySelector('#status').textContent)||window.fxWorkbench.getStatus().playPending===true);
      result.beforeFailure=await get(page);
      await page.evaluate(()=>window.__qa.crash());
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      result.failed=await get(page);
      assert.equal(result.failed.status.audioBusy,false,'A crashed worker must release its busy latch');
      assert.equal(result.failed.status.playPending,false,'Worker failure must settle pending transport requests');
      assert.match(result.failed.status.audioError,/QA injected/);
      assert.equal(await page.locator('#audio-retry').isVisible(),true,'Retry must be offered after worker failure');
      await page.locator('#monitor').selectOption('reference');await page.locator('#play').click();
      await page.waitForFunction(()=>window.fxWorkbench.getStatus().playing);
      result.reference=await get(page);
      assert.equal(result.reference.status.playing,true,'Reference audition remains available when FX rendering fails');
      await page.locator('#play').click();
      await page.locator('#audio-retry').click();await settled(page);
      result.retried=await get(page);
      assert.ok(result.retried.workerCount>result.failed.workerCount,'Retry must create a fresh worker');
      assert.equal(result.retried.status.audioError,null);
      await page.locator('#monitor').selectOption('expressive');await page.locator('#play').click();
      await page.waitForFunction(()=>window.fxWorkbench.getStatus().playing);
      result.expressive=await get(page);result.pageErrors=pageErrors;
      assert.equal(result.expressive.status.playing,true);
      assert.deepEqual(pageErrors,[]);
    }finally{await context.close();}
  });
  await check('Metadata-only preset video receives a Play attempt before readyState reaches 2',async result=>{
    const {page,context,pageErrors}=await fresh({videoGate:true});
    try{
      result.before=await get(page);
      assert.equal(result.before.previewPlays,0);
      await page.locator('#play').click();
      await page.waitForFunction(()=>window.fxWorkbench.getStatus().playing);
      await page.waitForFunction(()=>window.__qa.previewPlays>0,null,{timeout:5000});
      await page.waitForFunction(()=>window.fxWorkbench.getStatus().preview==='rendered',null,{timeout:10000});
      result.after=await get(page);result.pageErrors=pageErrors;
      assert.ok(result.after.previewPlays>0,'A metadata-only video needs play() to trigger deferred loading');
      assert.equal(result.after.status.preview,'rendered');
      assert.deepEqual(pageErrors,[]);
    }finally{await context.close();}
  });
  await check('Valid JSON with malformed persisted patch falls back to a playable factory rack',async result=>{
    const {page,context,pageErrors}=await fresh({saved:{schemaVersion:'1',id:'corrupt',name:'Corrupt stored patch',nodes:[null],connections:[],events:[],metadata:{demoPhases:false}}});
    try{
      const restored=await page.evaluate(()=>window.fxWorkbench.getPatch());
      assert.equal(restored.id,'message-impact');assert.equal(await page.locator('[data-module]').count(),13);
      await page.locator('#play').click();await page.waitForFunction(()=>window.fxWorkbench.getStatus().playing);
      result.recovered=await get(page);result.patchId=restored.id;result.pageErrors=pageErrors;
      assert.equal(result.recovered.status.playing,true);assert.deepEqual(pageErrors,[]);
    }finally{await context.close();}
  });
}catch(error){report.fatal=error.stack;console.log('FATAL',error.stack);}finally{
  report.finished=new Date().toISOString();report.passed=report.checks.filter(x=>x.pass).length;report.failed=report.checks.filter(x=>!x.pass).length;save();
  await browser?.close();await new Promise(resolve=>server.close(resolve));
  console.log(JSON.stringify({passed:report.passed,failed:report.failed,fatal:report.fatal,report:reportPath},null,2));
  process.exitCode=report.failed||report.fatal?1:0;
}
