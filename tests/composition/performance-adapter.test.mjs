import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {PerformancePlayer, RuntimeSupervisor, referenceGeometry, preparePerformanceAudio,audioVariantKey} from '../../web/studies/performance-adapter.mjs';

const fixture = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json,sys
sys.path.insert(0,'tests')
from composition.fixtures import record
from amp.composition.supervisor import PerformanceSupervisor
r=record().to_dict();r['runtime']['profile'].update(admitted=True,byte_caps=dict(decoded_audio_bytes=1000000,texture_bytes=1000000,event_history_bytes=1000000))
s=PerformanceSupervisor(r);s.admit(dict(decoded_audio_bytes=100,texture_bytes=100,event_history_bytes=100));s.resume()
states=[s.advance(f) for f in (0,1,30,89,90,179,180)]
print(json.dumps(dict(record=r,states=states)))
`], {cwd:new URL('../..',import.meta.url), encoding:'utf8'}));
const fresh=()=>structuredClone(fixture.record);
const measurements={decoded_audio_bytes:100,texture_bytes:100,event_history_bytes:100};

test('browser scheduler follows Python source clocks and event bounds',()=>{
  const s=new RuntimeSupervisor(fresh());s.admit(measurements);s.resume();
  for(const expected of fixture.states){const actual=s.advance(expected.frame);
    for(const key of ['frame','status','playing','reference','active','pending','audio_voices','visual_instances'])
      assert.deepEqual(actual[key],expected[key],key+' at '+expected.frame);
  }
});
test('count and byte exhaustion preserve reference and suppress old voices after recovery',()=>{
  const r=fresh();r.runtime.profile.audio_voices=1;
  const other=structuredClone(r.runtime.events[1]);other.id='other';r.runtime.events.push(other);
  const s=new RuntimeSupervisor(r);s.admit(measurements);let state=s.resume();
  assert.equal(state.active.length,0);assert.equal(state.fallback_material_id,'clip');assert.ok(state.reference);
  s.advance(20);s.recover(measurements);state=s.advance(21);assert.equal(state.active.length,0);
  s.observeResources({...measurements,texture_bytes:2000000});assert.ok(s.snapshot().degradation_reasons.includes('texture_bytes'));
});
test('pause, cancel and checkpoint reload preserve intent without autoplay',async()=>{
  const s=new RuntimeSupervisor(fresh());s.admit(measurements);s.resume();s.advance(30);
  const checkpoint=await s.checkpoint();const restored=await RuntimeSupervisor.restore(JSON.parse(JSON.stringify(checkpoint)));
  assert.equal(restored.frame,30);assert.equal(restored.playing,false);assert.equal(restored.resumeIntent,true);
  restored.admit(measurements);restored.resume();assert.equal(restored.snapshot().frame,30);
  restored.cancel();assert.throws(()=>restored.resume());assert.equal(restored.advance(40).status,'cancelled');
  checkpoint.state.frame++;await assert.rejects(()=>RuntimeSupervisor.restore(checkpoint),/digest/);
});
test('malformed records and unadmitted profiles fail before playback',()=>{
  for(const mutate of [r=>r.runtime.events[1].picture_clock.rate_den=0,r=>r.runtime.events[1].parent_id='missing',r=>r.runtime.events[1].gain=NaN,r=>r.runtime.materials[0].metadata.qualification.technical='pending']){
    const r=fresh();mutate(r);assert.throws(()=>new RuntimeSupervisor(r));
  }
  const r=fresh();r.runtime.profile.admitted=false;const s=new RuntimeSupervisor(r);assert.throws(()=>s.admit(measurements));assert.throws(()=>s.resume());
  for(const [key,value]of Object.entries({max_depth:5,audio_voices:25,visual_instances:13,pending_events:257,lookahead_frames:301})){
    const changed=fresh();changed.runtime.profile[key]=value;assert.throws(()=>new RuntimeSupervisor(changed),key);
  }
});
test('reference geometry is letterboxed in a reserved third in either orientation',()=>{
  for(const [w,h] of [[960,540],[540,960]]){
    const box=referenceGeometry(w,h,16/9);assert.equal(box.reserved.height,h/3);
    assert.equal(box.image.width/box.image.height,16/9);
    assert.ok(box.image.y>=box.reserved.y);assert.ok(box.image.y+box.image.height<=h);
    assert.equal(box.expressive.height,2*h/3);
  }
});

function audioContext(){
  const started=[],stopped=[],gains=[];
  return {currentTime:0,state:'suspended',started,stopped,gains,
    async resume(){this.state='running';},destination:{},
    createBufferSource(){return {playbackRate:{value:1},connect(){},disconnect(){},start(...a){started.push({source:this,args:a});},stop(){stopped.push(this);}};},
    createGain(){const node={gain:{value:0,setValueAtTime(v){this.value=v;}},connect(){},disconnect(){}};gains.push(node);return node;},
  };
}
function canvas(){const draws=[];return {width:96,height:54,draws,getContext(){return {save(){},restore(){},beginPath(){},rect(){},clip(){},clearRect(){},fillRect(){},setTransform(){},drawImage(...args){draws.push(args);},globalAlpha:1};}};}
function prepared(){const p={materials:new Map([['clip',{sha256:fixture.record.runtime.materials[0].sha256,
  audioBuffer:{numberOfChannels:1,length:176400,sampleRate:44100,getChannelData:()=>new Float32Array(176400)},
  media:{fps:30,duration:4,columns:120,framesPerPage:120,picture:{width:1,height:1},matte:{width:1,height:1}},
  assets:{picture:[{width:120,height:1}],matte:[{width:120,height:1}]}}]])};
  // Scheduling fake; the separate retained-DSP test exercises real processing.
  p.audioVariants=new Map([['shot',{key:audioVariantKey(fresh(),fixture.record.runtime.events[1]),buffer:p.materials.get('clip').audioBuffer}]]);return p;}
function playbackRecord(){const r=fresh();r.runtime.profile.byte_caps.decoded_audio_bytes=100000000;r.runtime.profile.byte_caps.texture_bytes=100000000;return r;}
test('one persistent decoded buffer drives native sample offsets; pause and seek stop stale sources',async()=>{
  const ctx=audioContext(),out=canvas();const player=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:out,audioContext:ctx,createCanvas:canvas,autoTick:false});
  await player.play();assert.equal(ctx.started.length,1);assert.equal(ctx.started[0].args[1],14700/44100);
  ctx.currentTime=1;player.tick();assert.equal(player.supervisor.frame,30);
  player.pause();assert.ok(ctx.stopped.length);player.seek(60);await player.play();
  assert.equal(ctx.started.at(-1).args[1],(14700+88200)/44100);
  const events=JSON.stringify(player.supervisor.runtime.events);player.rotate('vertical');assert.equal(JSON.stringify(player.supervisor.runtime.events),events);
  player.cancel();const n=ctx.started.length;ctx.currentTime=2;player.tick();assert.equal(ctx.started.length,n);
});
test('late audio-context readiness cannot undo cancel or pause',async()=>{
  for(const action of ['cancel','pause']){
    const ctx=audioContext();let release;ctx.resume=()=>new Promise(resolve=>{release=resolve;});
    const p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:ctx,createCanvas:canvas,autoTick:false});
    const pending=p.play();p[action]();release();await pending;assert.equal(ctx.started.length,0);assert.equal(p.supervisor.playing,false);
  }
});
test('background suspension retains play intent while requiring a fresh gesture',async()=>{
  const ctx=audioContext();let release;ctx.resume=()=>new Promise(resolve=>{release=resolve;});
  const p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:ctx,createCanvas:canvas,autoTick:false});
  const pending=p.play();p.suspend();release();await pending;
  assert.equal(p.supervisor.playing,false);assert.equal(p.supervisor.resumeIntent,true);assert.equal(ctx.started.length,0);
  p.pause();assert.equal(p.supervisor.resumeIntent,false);
});
test('audio FX are prepared through the retained DSP and counted before allocation',async()=>{
  const r=playbackRecord(),p=prepared(),ctx=audioContext();
  ctx.createBuffer=(channels,length,sampleRate)=>{const data=Array.from({length:channels},()=>new Float32Array(length));return {numberOfChannels:channels,length,sampleRate,getChannelData:c=>data[c],copyToChannel:(a,c)=>data[c].set(a)};};
  const input=new Float32Array(176400).fill(.1);p.materials.get('clip').audioBuffer.getChannelData=()=>input;
  await preparePerformanceAudio(r,p,ctx);const variant=p.audioVariants.get('shot');
  assert.ok(variant);assert.notEqual(variant.buffer,p.materials.get('clip').audioBuffer);
  assert.ok(variant.report.modules.some(m=>m.type==='routing'));
  assert.ok(variant.buffer.getChannelData(0).some(v=>v!==input[0]));
  const tooSmall=fresh();tooSmall.runtime.profile.byte_caps.decoded_audio_bytes=1;
  let allocations=0;ctx.createBuffer=()=>{allocations++;throw Error('must not allocate');};
  await assert.rejects(()=>preparePerformanceAudio(tooSmall,prepared(),ctx),/byte/);assert.equal(allocations,0);
});
test('played control observations are measured once, preserved and used by delayed coupling',async()=>{
  const r=playbackRecord();r.runtime.events[1].couplings=[{source_event_id:'shot',lag_frames:1,amount:.5}];
  const ctx=audioContext(),p=prepared();p.audioVariants.get('shot').buffer.getChannelData=()=>new Float32Array(176400).fill(.5);
  const player=new PerformancePlayer({record:r,prepared:p,canvas:canvas(),audioContext:ctx,createCanvas:canvas,autoTick:false});
  await player.play();assert.equal(player.supervisor.controls.length,1);assert.equal(player.supervisor.controls[0].frame,0);
  ctx.currentTime=1/30;player.tick();assert.ok(ctx.gains[0].gain.value>.5);
  player.tick();assert.equal(player.supervisor.controls.length,2,'same frame has one immutable observation');
});
test('texture pressure never allocates beyond its admitted cap and still paints the reference',async()=>{
  const r=playbackRecord();r.runtime.profile.byte_caps.texture_bytes=30000;
  let allocated=0;const allocate=(w,h)=>{allocated+=w*h*4;return canvas();};const ctx=audioContext(),out=canvas();
  const p=new PerformancePlayer({record:r,prepared:prepared(),canvas:out,audioContext:ctx,createCanvas:allocate,autoTick:false});
  await p.play();assert.ok(p.supervisor.reasons.includes('texture_bytes'));assert.equal(allocated,4,'only the pre-reserved isolated reference cell is allocated');assert.ok(out.draws.length>0);assert.equal(p.supervisor.snapshot().active.length,0);
});
test('later cancel, pause, seek and restore supersede an in-flight restore',async()=>{
  for(const action of ['cancel','pause','seek']){
    const p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:audioContext(),createCanvas:canvas,autoTick:false});
    p.seek(30);const cp=await p.checkpoint();p.seek(0);const pending=p.restore(cp);
    if(action==='seek')p.seek(10);else p[action]();await pending;
    assert.equal(p.supervisor.status,action==='cancel'?'cancelled':'paused');assert.equal(p.supervisor.frame,action==='seek'?10:0);
  }
  const p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:audioContext(),createCanvas:canvas,autoTick:false});
  p.seek(10);const a=await p.checkpoint();p.seek(20);const b=await p.checkpoint();p.seek(0);
  await Promise.all([p.restore(a),p.restore(b)]);assert.equal(p.supervisor.frame,20);
});
test('repeated Play synchronizes the existing output clock without rescheduling its anchor',async()=>{
  const ctx=audioContext(),p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:ctx,createCanvas:canvas,autoTick:false});
  await p.play();ctx.currentTime=1;await p.play();assert.equal(p.supervisor.frame,30);assert.equal(p.anchorTime,0);assert.equal(ctx.started.length,1);
});
test('finished and cancelled checkpoints reload for inspection without playback admission',async()=>{
  for(const status of ['finished','cancelled']){
    const p=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:audioContext(),createCanvas:canvas,autoTick:false});
    if(status==='finished')p.seek(180);else p.cancel();const cp=await p.checkpoint();
    const next=new PerformancePlayer({record:playbackRecord(),prepared:prepared(),canvas:canvas(),audioContext:audioContext(),createCanvas:canvas,autoTick:false});
    await next.restore(cp);assert.equal(next.supervisor.status,status);assert.equal(next.supervisor.playing,false);await assert.rejects(()=>next.play());
    if(status==='finished'){next.seek(0);await next.play();assert.equal(next.supervisor.playing,true);}else assert.throws(()=>next.seek(0));
  }
});
test('atlas cell geometry cannot put any required source rectangle outside its page',()=>{
  for(const mutate of [p=>p.materials.get('clip').media.picture.width=2,p=>p.materials.get('clip').media.matte.height=2,p=>p.materials.get('clip').media.picture.width=0]){
    const p=prepared();mutate(p);assert.throws(()=>new PerformancePlayer({record:playbackRecord(),prepared:p,canvas:canvas(),audioContext:audioContext(),createCanvas:canvas,autoTick:false}));
  }
});
