import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createCanvas,loadImage} from '@napi-rs/canvas';
import {readFile} from 'node:fs/promises';
import {PerformancePlayer,digest,canonicalJSON} from '../../web/studies/performance-adapter.mjs';
import {loadPreparedFiles,verifyRetainedEngine,verifyWavHeader} from '../../web/studies/performance-assets.mjs';
import {encodeWav} from '../../web/fx/audio-engine.mjs';
import {VisualEngine} from '../../web/fx/visual-engine.mjs';

const base=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c',"import json;from tests.composition.fixtures import record;print(json.dumps(record().to_dict()))"],{cwd:new URL('../..',import.meta.url),encoding:'utf8'}));
function context(){return {currentTime:0,destination:{},resume:async()=>{},createBuffer(channels,length,sampleRate){const data=Array.from({length:channels},()=>new Float32Array(length));return {numberOfChannels:channels,length,sampleRate,copyToChannel:(a,c)=>data[c].set(a),getChannelData:c=>data[c]};},createBufferSource(){return {playbackRate:{value:1},connect(){},disconnect(){},start(){},stop(){}};},createGain(){return {gain:{setValueAtTime(){}},connect(){},disconnect(){}};}};}
async function fixture(){const record=structuredClone(base);Object.assign(record.runtime.profile,{admitted:true,byte_caps:{decoded_audio_bytes:100000000,texture_bytes:100000000,event_history_bytes:1000000}});
  const canvas=createCanvas(120,1),ctx=canvas.getContext('2d');ctx.fillStyle='#ff0000';ctx.fillRect(0,0,120,1);ctx.fillStyle='#0000ff';ctx.fillRect(10,0,1,1);
  const png=canvas.toBuffer('image/png'),wav=encodeWav([new Float32Array(176400).fill(.1),new Float32Array(176400).fill(.1)],44100);
  const row={file:'atlas.png',sha256:await digest(png),bytes:png.length,width:120,height:1};
  const manifest={schema:'amps.prepared-material.v1',media:{fps:30,duration:4,columns:120,framesPerPage:120,picture:{width:1,height:1},matte:{width:1,height:1}},assets:{picture:[row],matte:[row]},audio:{file:'source.wav',sha256:await digest(wav),bytes:wav.byteLength,channels:2,samples:176400,sample_rate:44100}};
  const bytes=new TextEncoder().encode(JSON.stringify(manifest));record.runtime.materials[0].sha256=await digest(bytes);record.runtime.materials[0].bytes=bytes.length;
  return {record,files:[new File([bytes],'clip.material.json'),new File([png],'atlas.png'),new File([wav],'source.wav')],manifest};
}
const decodeImage=async blob=>loadImage(Buffer.from(await blob.arrayBuffer()));
test('untrusted WAV headers cannot allocate channels or frames beyond admitted measurements',()=>{
  const wav=encodeWav([new Float32Array(30),new Float32Array(30)],44100),row={channels:2,samples:30,sample_rate:44100};
  verifyWavHeader(wav,row);const changed=wav.slice(0);new DataView(changed).setUint16(22,65535,true);
  assert.throws(()=>verifyWavHeader(changed,row),/measurements/);
  const corrupt=wav.slice(0);new DataView(corrupt).setUint16(32,1,true);assert.throws(()=>verifyWavHeader(corrupt,row),/measurements/);
});
test('local package authenticates every byte and keeps exact protected pixels in H/V',async()=>{
  const {record,files}=await fixture(),audioContext=context();const prepared=await loadPreparedFiles(record,files,audioContext,{decodeImage});
  const canvas=createCanvas(96,54),player=new PerformancePlayer({record,prepared,canvas,audioContext,createCanvas,autoTick:false});
  await player.play();for(const mode of ['horizontal','vertical']){player.rotate(mode);const w=canvas.width,h=canvas.height,ctx=canvas.getContext('2d');
    assert.deepEqual([...ctx.getImageData(Math.floor(w/2),Math.floor(h*5/6),1,1).data],[255,0,0,255]);
    assert.deepEqual([...ctx.getImageData(0,Math.floor(h*5/6),1,1).data],[0,0,0,255]);
  }
  assert.equal(player.engine('clip').frame,VisualEngine.prototype.frame,'render must restore the original frame method without accumulating bound wrappers');
  assert.ok(player.supervisor.controls.length);assert.equal(player.supervisor.snapshot().active[0].picture_clock_position,10);
  player.cancel();
});
test('changed bytes, absent files, duplicate basenames and cancellation fail closed',async()=>{
  const {record,files}=await fixture();const changed=files.map(f=>f.name==='atlas.png'?new File([new Uint8Array(f.size)],f.name):f);
  await assert.rejects(()=>loadPreparedFiles(record,changed,context(),{decodeImage}),/hash/);
  await assert.rejects(()=>loadPreparedFiles(record,files.slice(0,2),context(),{decodeImage}),/missing/);
  await assert.rejects(()=>loadPreparedFiles(record,[...files,files[1]],context(),{decodeImage}),/Duplicate/);
  const abort=new AbortController();abort.abort();await assert.rejects(()=>loadPreparedFiles(record,files,context(),{signal:abort.signal,decodeImage}),/abort/i);
  const delayed=new AbortController();let closed=0;await assert.rejects(()=>loadPreparedFiles(record,files,context(),{signal:delayed.signal,decodeImage:async()=>{delayed.abort();return {width:120,height:1,close(){closed++;}};}}),/abort/i);assert.ok(closed>0,'late decoded image is released');
});
test('retained engine readback agrees with the actual installed CLI identity',async()=>{
  const record=structuredClone(base);record.runtime.engine_sha256=execFileSync(process.env.PYTHON||'python3',['-c','from amp.composition.performance_cli import retained_engine_identity;print(retained_engine_identity())'],{cwd:new URL('../..',import.meta.url),encoding:'utf8'}).trim();
  const fetcher=async url=>({ok:true,arrayBuffer:async()=>{const data=await readFile(url);return data.buffer.slice(data.byteOffset,data.byteOffset+data.byteLength);}});
  assert.equal(await verifyRetainedEngine(record,fetcher),record.runtime.engine_sha256);record.runtime.engine_sha256='0'.repeat(64);await assert.rejects(()=>verifyRetainedEngine(record,fetcher),/different/);
});
