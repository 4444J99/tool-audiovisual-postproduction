/** Matched eight-layout comparison using the shared FX engines and source-bound media pack. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {spawn,execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {createSingleModulePatch} from '../web/fx/core.mjs';
import {normalizeWorkbenchPatch,validateDecodedAudio} from '../web/fx/workbench-state.mjs';
import {decodeWav,encodeWav,renderRackAudio,amplitudeEnvelope} from '../web/fx/audio-engine.mjs';
import {VisualEngine} from '../web/fx/visual-engine.mjs';
import {CANVAS,LAYOUT_NAMES,silhouette,anchor as anchorRect,bands as bandRects,textRect} from '../web/fx/layout-geometry.mjs';
const require=createRequire(import.meta.url),{createCanvas,loadImage}=require('@napi-rs/canvas');
const root=path.resolve(process.argv[2]||'web/fx/assets'),out=path.resolve(process.argv[3]||'outputs/full-frame-comparison');await fs.mkdir(out,{recursive:true});
const json=async p=>JSON.parse(await fs.readFile(path.join(root,p),'utf8'));
const media=await json('manifest.json'),regions=await json(media.regions),cues=await json(media.cues),provenance=await json(media.provenance);
const assets={picture:await Promise.all(media.picture.pages.map(p=>loadImage(path.join(root,p)))),matte:await Promise.all(media.matte.pages.map(p=>loadImage(path.join(root,p)))),room:await loadImage(path.join(root,media.room.hybrid)),regions,cues};
const decoded=validateDecodedAudio(decodeWav(await fs.readFile(path.join(root,media.audio.reference))),media),envelope=amplitudeEnvelope(decoded.channels,decoded.sampleRate);
const source={id:'god-here-second-draft',sha256:provenance.source.sha256,offset:media.sourceIn,duration:media.duration};
const names=[...LAYOUT_NAMES];
const types=['displacement','exchange','routing','reverse','granular','stutter','delay','gate'];
const selection=process.argv[4]?Number(process.argv[4])-1:null;
let entries=[];if(selection!==null){const previous=JSON.parse(await fs.readFile(path.join(out,'comparison-manifest.json'),'utf8'));entries=previous.entries.filter(e=>e.layout!==names[selection]);}
function cover(ctx,image,x,y,w,h){const scale=Math.max(w/image.width,h/image.height),sw=w/scale,sh=h/scale;ctx.drawImage(image,(image.width-sw)/2,(image.height-sh)/2,sw,sh,x,y,w,h);}
function figure(ctx,image,region,x,y,w,h){const [rx,ry,rw,rh]=region.bounds;const sw=rw*image.width,sh=rh*image.height,scale=Math.min(w/sw,h/sh),dw=sw*scale,dh=sh*scale;ctx.drawImage(image,rx*image.width,ry*image.height,sw,sh,x+(w-dw)/2,y+(h-dh)/2,dw,dh);}

function done(p){return new Promise((resolve,reject)=>{p.on('error',reject);p.on('close',c=>c===0?resolve():reject(Error('ffmpeg '+c)));});}
for(let layout=0;layout<8;layout++){
 if(selection!==null&&selection!==layout)continue;
 const name=`${String(layout+1).padStart(2,'0')}-${names[layout]}`,type=types[layout];
 let patch=normalizeWorkbenchPatch(createSingleModulePatch(type,{source,demonstration:false,module:{targets:['picture','voice','text'],region:'all'}}),{source,duration:media.duration});
 patch.name=names[layout];patch.metadata={...patch.metadata,wordMode:'authored',words:'OUT HOW WHAT',captionStatus:'Authored expressive words; not verified captions',layout: names[layout],fullFrameRequired:true};
 // All variants retain a readable dry dialogue bus alongside the extreme processor.
 patch.connections.push({from:'source',to:'output',port:'audio',gain:.75,role:'send'});
 const audio=renderRackAudio(patch,decoded.channels,decoded.sampleRate,media.duration,{peakTarget:.92});
 const wav=path.join(out,name+'.wav');await fs.writeFile(wav,new Uint8Array(encodeWav(audio.channels,decoded.sampleRate)));
 await fs.writeFile(path.join(out,name+'.json'),JSON.stringify({patch,audioReport:audio.report},null,2));
 const engine=new VisualEngine(media,assets,{createCanvas,width:384,height:216,cacheFrames:96,control:t=>envelope[Math.min(envelope.length-1,Math.floor(t*decoded.sampleRate))]});
 for(const portrait of [false,true]){
  const {width:W,height:H}=CANVAS[portrait?'portrait':'landscape'],canvas=createCanvas(W,H),ctx=canvas.getContext('2d');
  const filename=name+(portrait?'-vertical':'-horizontal')+'.mp4';
  const encoder=spawn('ffmpeg',['-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s',`${W}x${H}`,'-r','12','-i','pipe:0','-i',wav,'-c:v','libx264','-preset','fast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-t',String(media.duration),'-movflags','+faststart',path.join(out,filename)],{stdio:['pipe','ignore','inherit']});const completion=done(encoder);
  const anchor=anchorRect(portrait);
  for(let i=0;i<media.frameCount;i++){
   const t=i/12,layers=engine.renderLayers(patch,t),full=engine.frame(t),mask=engine.frame(t,true);
   ctx.fillStyle='#101418';ctx.fillRect(0,0,W,H);
   ctx.globalAlpha=.45;cover(ctx,assets.room,0,0,W,H);ctx.globalAlpha=1;
   const bands=bandRects(portrait);
   for(let b=0;b<bands.length;b++){let [x,y,w,h]=bands[b];ctx.save();ctx.beginPath();ctx.rect(x,y,w,h);ctx.clip();ctx.globalAlpha=.85;cover(ctx,layers.picture,x,y,w,h);ctx.restore();}
   // Five independent workprint silhouettes; no rectangular source backgrounds.
   for(let r=0;r<regions.items.length;r++){
    const region=regions.items[r],cut=createCanvas(384,216),cc=cut.getContext('2d');cc.drawImage(layers.picture,0,0);
    const alpha=createCanvas(384,216),ac=alpha.getContext('2d');ac.drawImage(mask,0,0,384,216);const pix=ac.getImageData(0,0,384,216);
    for(let k=0;k<pix.data.length;k+=4)pix.data[k+3]=Math.abs(pix.data[k]-region.labelValue)<15?255:0;
    ac.putImageData(pix,0,0);cc.globalCompositeOperation='destination-in';cc.drawImage(alpha,0,0);
    const p=silhouette(layout,r,portrait,t,media.duration);
    if(p.mirror){ctx.save();ctx.translate(p.x+p.w,p.y);ctx.scale(-1,1);}
    ctx.globalAlpha=p.alpha;
    if(p.ghosts.length){ctx.globalAlpha=p.ghosts[0].alpha;for(const g of p.ghosts)figure(ctx,cut,region,g.x,g.y,g.w,g.h);ctx.globalAlpha=1;}
    if(p.drawn)figure(ctx,cut,region,p.mirror?0:p.x,p.mirror?0:p.y,p.w,p.h);
    if(p.mirror)ctx.restore();ctx.globalAlpha=1;
   }
   ctx.globalAlpha=.85;ctx.drawImage(layers.text,...textRect(portrait));ctx.globalAlpha=1;
   // Draw the complete reference last, so every pixel stays unobstructed.
   ctx.fillStyle='#edf3d2';ctx.fillRect(anchor[0]-2,anchor[1]-2,anchor[2]+4,anchor[3]+4);ctx.drawImage(full,...anchor);
   ctx.fillStyle='#edf3d2';ctx.font='bold 15px sans-serif';ctx.fillText(`${layout+1} / ${names[layout].replaceAll('-',' ').toUpperCase()}`,12,22);
   ctx.font='11px sans-serif';ctx.fillText(`SOURCE ${(95+t).toFixed(2)}s  /  ${type.toUpperCase()} + DRY DIALOGUE`,12,H-10);
   const pix=ctx.getImageData(0,0,W,H).data;if(!encoder.stdin.write(Buffer.from(pix.buffer,pix.byteOffset,pix.byteLength)))await once(encoder.stdin,'drain');
   if(i===72)await fs.writeFile(path.join(out,name+(portrait?'-vertical':'-horizontal')+'.png'),canvas.toBuffer('image/png'));
  }
  encoder.stdin.end();await completion;const probe=JSON.parse(execFileSync('ffprobe',['-v','error','-count_frames','-show_streams','-of','json',path.join(out,filename)],{encoding:'utf8'}));if(Number(probe.streams.find(s=>s.codec_type==='video')?.nb_read_frames)!==media.frameCount)throw Error('Encoded frame count mismatch: '+filename);entries.push({filename,layout:names[layout],orientation:portrait?'vertical':'horizontal',frames:media.frameCount,duration:media.duration,fullFrameRect:anchor,processor:type});console.log(filename);
 }
}
await fs.writeFile(path.join(out,'comparison-manifest.json'),JSON.stringify({source,entries,maskStatus:'Existing hard workprint masks; temporal edges and overlaps remain unaccepted',audioStatus:'Existing DeepFilterNet candidate plus expressive copy; listening acceptance pending'},null,2));
