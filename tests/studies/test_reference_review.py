"""Every-frame source/reference integrity over actual retained-Canvas captures."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from amp.composition.performance_cli import retained_engine_identity

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests'))
from composition.fixtures import record
from studies import test_review_evidence as audio_tests


def run(*command):
    """Run a fixture command; a failure carries the command's own stderr instead of a bare exit status."""
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=20)
    if result.returncode:
        raise AssertionError(f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()[-800:]}")
    return result


class ReferenceReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # @napi-rs/canvas is a declared devDependency that CI installs with `npm ci`; a fresh checkout lacks it.
        # Fail once with the remedy rather than as four opaque subprocess errors. This is a failure, not a skip.
        if not (ROOT/'node_modules'/'@napi-rs'/'canvas').is_dir():
            raise AssertionError("@napi-rs/canvas is not installed; run `npm ci --ignore-scripts --no-audit --no-fund` in the repository root")

    def fixture(self,directory):
        helper=audio_tests.ReviewEvidenceTests();root,binding,locations,original=helper.fixture(directory)
        def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
        run('node','tests/composition/render_reference_fixture.mjs','source',str(root))
        source=root/'source.mkv';run('ffmpeg','-nostdin','-v','error','-framerate','2','-i',str(root/'source-%03d.png'),'-c:v','ffv1','-pix_fmt','bgr0',str(source))
        r=record().to_dict();r['record_id']='reference-fixture';r['span']['end']=8;r['timebase']={'num':2,'den':1}
        runtime=r['runtime'];runtime['duration_frames']=8;runtime['engine_sha256']=retained_engine_identity()
        runtime['profile'].update(admitted=True,lookahead_frames=20,byte_caps=dict(decoded_audio_bytes=2_000_000,texture_bytes=8_000_000,event_history_bytes=1_000_000))
        material=runtime['materials'][0];material.update(sha256=sha(source),bytes=source.stat().st_size)
        material['span']={'start':0,'end':4,'timebase':{'num':2,'den':1}}
        material['metadata'].update(audio_sample_rate=48000,audio_origin_sample=0,audio_sample_count=96000)
        runtime['reference']['clock'].update(start_frame=0,end_frame=4)
        runtime['events'][0]['end_frame']=8;event=runtime['events'][1];event.update(end_frame=8,audio=False)
        event['picture_clock']['start_frame']=0;event['matte_clock']['start_frame']=0
        event['audio_clock'].update(start_sample=0,sample_rate=48000)
        record_file=root/'performance.json';record_file.write_text(json.dumps(r))
        run('node','tests/composition/render_reference_fixture.mjs','views',str(root),str(record_file))
        loc=json.loads(locations.read_text())
        def add(name,p,kind):
            ref=dict(id=name,sha256=sha(p),bytes=p.stat().st_size,kind=kind);binding['inputs'].append(ref);loc['objects'][ref['sha256']]=str(p);return ref
        add('reference-source',source,'video');rr=add('performance',record_file,'metadata');views=[]
        adapter=sha(ROOT/'web/studies/performance-adapter.mjs')
        for orientation,w,h in (('horizontal',97,55),('vertical',49,83)):
            movie=root/(orientation+'.mkv');run('ffmpeg','-nostdin','-v','error','-framerate','2','-i',str(root/(orientation+'-%03d.png')),'-c:v','ffv1','-pix_fmt','bgr0',str(movie))
            output=add(orientation,movie,'video');sidecar=root/(orientation+'.json')
            sidecar.write_text(json.dumps(dict(schema='amps.performance-capture.v1',performance_sha256=rr['sha256'],output_sha256=output['sha256'],source_sha256=sha(source),orientation=orientation,renderer_sha256=adapter,frames=8,frame_rate={'num':2,'den':1},dimensions=[w,h])))
            add(orientation+'-capture',sidecar,'metadata');views.append(dict(material_id=orientation,capture_record_id=orientation+'-capture',orientation=orientation,width=w,height=h,frames=8,frame_rate={'num':2,'den':1}))
        locations.write_text(json.dumps(loc))
        spec=binding['parameters']['review_spec'];spec['visual_reference']=dict(source_id='reference-source',performance_id='performance',source=dict(width=32,height=18,frames=4,frame_rate={'num':2,'den':1}),views=views,max_channel_error=0,mean_channel_error=0)
        spec['resource_profile'].update(max_scratch_bytes=8*1024*1024,deadline_seconds=60)
        return helper,root,binding,locations

    def test_every_frame_and_both_orientations_match_whole_source_at_record_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            helper,root,binding,locations=self.fixture(directory)
            process,receipt=helper.execute(root,binding,locations)
            self.assertEqual(process.returncode,0,receipt)
            self.assertEqual(receipt['reference_frame']['status'],'passed')
            self.assertEqual(receipt['reference_frame']['checked_frames'],16)
            self.assertEqual(receipt['reference_frame']['max_channel_error'],0)
            self.assertEqual(receipt['reference_frame']['source_frames'],[0,1,2,3,0,1,2,3])
            self.assertEqual(receipt['qualification']['perceptual'],'pending')
            self.assertFalse(receipt['comparison_complete']) # Actual human/model gates remain independent.
            self.assertFalse((root/'out/.reference-cache').exists())
            for name,row in receipt['outputs'].items():self.assertEqual(hashlib.sha256((root/'out'/name).read_bytes()).hexdigest(),row['sha256'])

    def test_missing_visual_caps_changed_clock_or_other_decision_record_block(self):
        with tempfile.TemporaryDirectory() as directory:
            helper,root,binding,locations=self.fixture(directory)
            changes=[lambda s:s['resource_profile'].pop('max_scratch_bytes'),
                lambda s:s['visual_reference']['views'][0].update(frame_rate={'num':3,'den':1}),
                lambda s:s['visual_reference'].update(performance_id='native-record')]
            for index,change in enumerate(changes):
                with self.subTest(change=index):
                    changed=copy.deepcopy(binding);change(changed['parameters']['review_spec'])
                    process,receipt=helper.execute(root,changed,locations,name=f'bad{index}',dry=True)
                    self.assertEqual(process.returncode,2,receipt)
                    self.assertFalse((root/f'bad{index}').exists())

    def test_occlusion_cannot_be_hidden_by_hash_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            helper,root,binding,locations=self.fixture(directory)
            movie=root/'vertical.mkv';bad=root/'occluded.mkv'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(movie),'-vf','drawbox=x=15:y=62:w=10:h=12:color=black:t=fill','-c:v','ffv1','-pix_fmt','bgr0',str(bad)],check=True,timeout=20)
            ref=next(x for x in binding['inputs'] if x['id']=='vertical');ref.update(sha256=hashlib.sha256(bad.read_bytes()).hexdigest(),bytes=bad.stat().st_size)
            loc=json.loads(locations.read_text());loc['objects'][ref['sha256']]=str(bad)
            p=root/'vertical.json';d=json.loads(p.read_text());d['output_sha256']=ref['sha256'];p.write_text(json.dumps(d))
            sidecar=next(x for x in binding['inputs'] if x['id']=='vertical-capture');sidecar.update(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size);loc['objects'][sidecar['sha256']]=str(p);locations.write_text(json.dumps(loc))
            process,receipt=helper.execute(root,binding,locations,name='occluded')
            self.assertEqual(process.returncode,2,receipt)
            self.assertEqual(receipt['error']['code'],'review_reference_pixels_mismatch')

    def test_wrong_phase_cropped_stretched_and_missing_frames_fail_actual_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            helper,root,binding,locations=self.fixture(directory)
            for name,filters,code in [('phase','reverse','review_reference_pixels_mismatch'),
                    ('crop','crop=45:83:2:0,scale=49:83','review_reference_pixels_mismatch'),
                    ('stretch','scale=49:63,pad=49:83:0:20:black','review_reference_pixels_mismatch'),
                    ('count','trim=end_frame=7','review_reference_presentation_clock_mismatch')]:
                with self.subTest(name=name):
                    changed=copy.deepcopy(binding);bad=root/(name+'.mkv')
                    subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(root/'vertical.mkv'),'-vf',filters,'-c:v','ffv1','-pix_fmt','bgr0',str(bad)],check=True,timeout=20)
                    ref=next(x for x in changed['inputs'] if x['id']=='vertical');ref.update(sha256=hashlib.sha256(bad.read_bytes()).hexdigest(),bytes=bad.stat().st_size)
                    loc=json.loads(locations.read_text());loc['objects'][ref['sha256']]=str(bad)
                    sidecar=root/(name+'.json');d=json.loads((root/'vertical.json').read_text());d['output_sha256']=ref['sha256'];sidecar.write_text(json.dumps(d))
                    row=next(x for x in changed['inputs'] if x['id']=='vertical-capture');row.update(sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),bytes=sidecar.stat().st_size);loc['objects'][row['sha256']]=str(sidecar)
                    separate=root/(name+'-locations.json');separate.write_text(json.dumps(loc))
                    process,receipt=helper.execute(root,changed,separate,name=name)
                    self.assertEqual(process.returncode,2,receipt);self.assertEqual(receipt['error']['code'],code)

    def test_protected_reference_region_starts_on_an_integer_pixel(self):
        result=subprocess.check_output(['node','--input-type=module','-e',"import {referenceGeometry} from './web/studies/performance-adapter.mjs';console.log(JSON.stringify(referenceGeometry(390,844,0.5)))"],cwd=ROOT,text=True)
        geometry=json.loads(result)
        self.assertEqual(geometry['reserved']['y'],int(geometry['reserved']['y']))

    def test_coarse_container_ticks_cannot_hide_one_frame_presentation_shift(self):
        import time
        from amp.media_resolver import ContractError
        from amp.studies.review_reference import video_clock
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'shifted.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=red:s=8x8:r=2:d=1',
                '-vf','setpts=PTS+1','-fps_mode','passthrough','-c:v','libx264rgb','-crf','0','-video_track_timescale','2',str(target)],check=True,timeout=20)
            with self.assertRaises(ContractError) as raised:
                video_clock(target,dict(width=8,height=8,frames=2,frame_rate={'num':2,'den':1}),time.monotonic()+10)
            self.assertEqual(raised.exception.code,'review_reference_presentation_clock_mismatch')
