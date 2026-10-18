"""Zeug layer: private hosting, spend guard, review assist, prompt builder, upstream fixes."""
import io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest.mock import Mock, patch
import av, numpy as np
from pipeline import writer, put, finish
import media_host, mask_review, pipeline
from zeug import gj, ledger, prompt, review_assist, s3_host


def make_clip(path, frames=24, audio=True, size=64):
    with av.open(str(path), 'w') as out:
        v = out.add_stream('libx264', rate=24); v.width = size; v.height = size; v.pix_fmt = 'yuv420p'
        a = out.add_stream('aac', rate=44100) if audio else None
        if a: a.layout = 'stereo'
        for i in range(frames):
            f = av.VideoFrame.from_ndarray(np.full((size, size, 3), 40, dtype=np.uint8), format='rgb24')
            f.pts = i; f.time_base = Fraction(1, 24)
            for packet in v.encode(f): out.mux(packet)
        for packet in v.encode(): out.mux(packet)
        if a:
            n = int(44100 * frames / 24)
            samples = np.zeros((2, n), dtype=np.float32)
            for i in range(0, n, 4096):
                f = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[:, i:i + 4096]), format='fltp', layout='stereo')
                f.sample_rate = 44100; f.pts = i
                for packet in a.encode(f): out.mux(packet)
            for packet in a.encode(): out.mux(packet)


def make_job(folder, empty_frame=None, frames=8):
    """Depth-mode job artifacts: a centred square subject, optionally missing in one frame."""
    for name in ['original', 'depth', 'mask', 'composite-silent']:
        c, s = writer(folder / (name + '.mp4'), 64, 64)
        for i in range(frames):
            a = np.zeros((64, 64, 3), dtype=np.uint8)
            if name == 'mask' and i != empty_frame:
                a[16:48, 16:48] = 255
            elif name != 'mask':
                a[:] = 90
            put(c, s, a, i)
        finish(c, s)


class HostingTests(unittest.TestCase):
    def test_private_primary_never_falls_back_to_public_host(self):
        env = {'MEDIA_HOST': 's3', 'MEDIA_HOST_FALLBACKS': 'catbox'}
        with patch.dict(os.environ, env), patch('media_host._upload_once', side_effect=ValueError('down')) as once, \
                patch('media_host.time.sleep'):
            with self.assertRaises(RuntimeError):
                media_host.upload('x.mp4')
        self.assertEqual({call.args[1] for call in once.call_args_list}, {'s3'})

    def test_public_fallback_only_when_explicitly_allowed(self):
        env = {'MEDIA_HOST': 's3', 'MEDIA_HOST_FALLBACKS': 'catbox', 'MEDIA_HOST_ALLOW_PUBLIC_FALLBACK': '1'}
        with patch.dict(os.environ, env), patch('media_host._upload_once', side_effect=ValueError('down')) as once, \
                patch('media_host.time.sleep'):
            with self.assertRaises(RuntimeError):
                media_host.upload('x.mp4')
        self.assertEqual({call.args[1] for call in once.call_args_list}, {'s3', 'catbox'})

    def test_s3_upload_presigns_with_capped_expiry_and_content_type(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return Mock(returncode=0, stdout='https://bucket.s3.amazonaws.com/k?X-Amz-Signature=x' if cmd[2] == 'presign' else '', stderr='')
        env = {'S3_MEDIA_BUCKET': 'b', 'S3_MEDIA_PREFIX': 'gj/', 'S3_MEDIA_EXPIRES': '999999999'}
        with patch.dict(os.environ, env), patch('zeug.s3_host.shutil.which', return_value='/usr/bin/aws'), \
                patch('zeug.s3_host.subprocess.run', side_effect=fake_run):
            url = s3_host.upload('/tmp/prepared.mp4')
        self.assertTrue(url.startswith('https://'))
        cp, presign = calls
        self.assertTrue(cp[4].startswith('s3://b/gj/') and cp[4].endswith('/prepared.mp4'))
        self.assertIn('video/mp4', cp)
        self.assertEqual(presign[presign.index('--expires-in') + 1], str(s3_host.MAX_EXPIRY))

    def test_s3_requires_bucket(self):
        with patch.dict(os.environ, {'S3_MEDIA_BUCKET': ''}):
            with self.assertRaisesRegex(ValueError, 'S3_MEDIA_BUCKET'):
                s3_host.upload('x.mp4')


class GuardTests(unittest.TestCase):
    def test_paid_stage_refused_without_allow_paid(self):
        with patch.dict(os.environ, {'GENJUTSU_ALLOW_PAID': ''}):
            with self.assertRaisesRegex(SystemExit, 'allow-paid'):
                ledger.guard('submit', False)
            calls, _ = ledger.guard('submit', True)
        self.assertEqual(calls, ['enhancor:seedance-draft'])

    def test_huggingface_depth_is_not_a_replicate_call(self):
        self.assertIn('huggingface:depth', ledger.calls_for('prepare:depth', 'huggingface'))

    def test_ledger_collects_saved_provider_ids(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'audio-prediction.json').write_text(json.dumps({'id': 'demucs-1'}))
            (p / 'seedance-response.json').write_text(json.dumps({'requestId': 'req-1'}))
            ledger.record(p, 'submit', 'submitted', provider_ids=ledger.provider_ids(p))
            entry = ledger.entries(p)[0]
        self.assertEqual(entry['provider_ids'], {'replicate:demucs': 'demucs-1', 'enhancor:seedance-draft': 'req-1'})

    def test_probe_rejects_long_or_silent_sources_before_spend(self):
        with tempfile.TemporaryDirectory() as d:
            make_clip(Path(d) / 'ok.mp4', frames=24)
            self.assertAlmostEqual(gj.probe(Path(d) / 'ok.mp4')['seconds'], 1.0, delta=0.1)
            make_clip(Path(d) / 'silent.mp4', audio=False)
            with self.assertRaisesRegex(SystemExit, 'audio'):
                gj.probe(Path(d) / 'silent.mp4')
            make_clip(Path(d) / 'long.mp4', frames=24 * 31, size=16)
            with self.assertRaisesRegex(SystemExit, '30 s'):
                gj.probe(Path(d) / 'long.mp4')

    def test_job_env_pins_recorded_pitch(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with patch.dict(os.environ, {}, clear=True):
                self.assertNotIn('GENJUTSU_PITCH_SEMITONES', gj.job_env(p))   # upstream +3 applies
            (p / 'audio-manifest.json').write_text(json.dumps({'pitch_semitones': 3}))
            self.assertEqual(gj.job_env(p)['GENJUTSU_PITCH_SEMITONES'], '3')
            gj.save_meta(p, {'pitch_semitones': 2})
            self.assertEqual(gj.job_env(p)['GENJUTSU_PITCH_SEMITONES'], '2')


class ReviewAssistTests(unittest.TestCase):
    def test_sheets_cover_every_frame_and_flag_missing_subject(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            make_job(p, empty_frame=5, frames=20)
            report = review_assist.run(p)
            self.assertEqual(report['frames'], 20)
            self.assertEqual(len(report['sheets']), 2)          # 16 cells per sheet in depth mode
            self.assertEqual(report['mask']['empty_frames'], [5])
            self.assertTrue(any('missing' in f for f in report['flags']))
            self.assertEqual(report['fingerprint'], mask_review.fingerprint(p))
            self.assertFalse((p / 'mask-review.json').exists())  # assist never approves

    def test_vision_is_skipped_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'GENJUTSU_VISION_BASE_URL': ''}):
            p = Path(d)
            make_job(p)
            report = review_assist.run(p, every=4, vision=True)
        self.assertIn('not configured', report['vision']['notes'])


class PromptTests(unittest.TestCase):
    def test_prompt_maps_identities_and_describes_the_scene(self):
        text = prompt.build([prompt.parse_identity('@Image1=man, short dark hair'),
                             prompt.parse_identity('@Image2=round blue plush creature')],
                            'Seamless orange studio backdrop. One hanging microphone',
                            dialogue='Hello   there', positions=['left', 'right'])
        self.assertIn('The person on the left becomes @Image1: man, short dark hair.', text)
        self.assertIn('[Scene] Seamless orange studio backdrop. One hanging microphone.', text)
        self.assertIn('"Hello there"', text)

    def test_prompt_requires_scene_and_identity_syntax(self):
        with self.assertRaises(ValueError):
            prompt.build([('@Image1', 'x')], '  ')
        with self.assertRaises(ValueError):
            prompt.parse_identity('Image1 without a token')


class UpstreamFixTests(unittest.TestCase):
    def test_overlong_clip_is_rejected_before_demucs_bills(self):
        with tempfile.TemporaryDirectory() as d, patch('pipeline.normalize', side_effect=ValueError('too long')), \
                patch('audio_workflow.separate') as separate:
            with self.assertRaises(ValueError):
                pipeline.run('clip.mp4', d)
        separate.assert_not_called()

    def test_depth_prediction_id_is_saved(self):
        model = {'latest_version': {'id': 'v1', 'openapi_schema': {'components': {'schemas': {'Input': {
            'properties': {'video': {'format': 'uri'}}}}}}}}
        responses = [Mock(json=Mock(return_value=model)), Mock(json=Mock(return_value={'id': 'depth-9', 'status': 'failed', 'error': 'x', 'urls': {}}))]
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'original.mp4'; src.write_bytes(b'v')
            with patch.dict(os.environ, {'REPLICATE_API_TOKEN': 't'}), \
                    patch('pipeline.requests.get', return_value=responses[0]), \
                    patch('pipeline.requests.post', side_effect=[Mock(json=Mock(return_value={'urls': {'get': 'u'}})), responses[1]]):
                with self.assertRaises(RuntimeError):
                    pipeline.depth_replicate(src, Path(d) / 'depth-raw.mp4')
            self.assertEqual(json.loads((Path(d) / 'depth-prediction.json').read_text())['id'], 'depth-9')


if __name__ == '__main__':
    unittest.main()
