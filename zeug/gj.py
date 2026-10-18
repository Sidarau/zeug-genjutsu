"""Zeug wrapper around workflow.py: spend guard, ledger, review assist and prompt builder.

Run from the project root (`-m` keeps the root importable):
  .venv/bin/python -m zeug.gj doctor
  .venv/bin/python -m zeug.gj prepare clip.mp4 --allow-paid
  .venv/bin/python -m zeug.gj review-assist JOB [--every 2] [--vision]
  .venv/bin/python -m zeug.gj review JOB --approve --fingerprint HASH --note "who, what, repairs"
  .venv/bin/python -m zeug.gj prompt --identity "@Image1=..." --scene "..." --out prompt.txt
  .venv/bin/python -m zeug.gj submit JOB --prompt-file prompt.txt --reference ref.png --allow-paid
  .venv/bin/python -m zeug.gj hd JOB --approved-by NAME --allow-paid
  .venv/bin/python -m zeug.gj ledger [JOB]
status, review and resume pass straight through to workflow.py and never bill.
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import load_env  # noqa: E402
from zeug import ledger  # noqa: E402

RUNS = ROOT / 'runs'
IMAGES, VIDEOS = ('.png', '.jpg', '.jpeg', '.webp'), ('.mp4', '.mov', '.webm')
MAX_SECONDS = 30


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def probe(video):
    """Duration and stream checks that upstream only makes after some calls are billed."""
    import av
    path = Path(video)
    if not path.is_file():
        raise SystemExit(f'no such file: {video}')
    with av.open(str(path)) as c:
        if not c.streams.video:
            raise SystemExit('source has no video stream')
        if not c.streams.audio:
            raise SystemExit('source has no audio track; the workflow restores the original soundtrack and needs one')
        vs = c.streams.video[0]
        seconds = float(vs.duration * vs.time_base) if vs.duration else float(c.duration / av.time_base)
    if seconds > MAX_SECONDS:
        raise SystemExit(f'source is {seconds:.1f} s; the hosted workflow takes {MAX_SECONDS} s or less. Trim it first.')
    return {'seconds': round(seconds, 3), 'width': vs.width, 'height': vs.height}


def check_reference(path, image_only=False):
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f'no such reference: {path}')
    suffix = p.suffix.lower()
    if suffix in IMAGES:
        from PIL import Image
        try:
            with Image.open(p) as im:
                im.verify()
        except Exception:
            raise SystemExit(f'{p.name} is not a decodable image')
        return 'image'
    if suffix in VIDEOS and not image_only:
        import av
        with av.open(str(p)) as c:
            if not c.streams.video:
                raise SystemExit(f'{p.name} has no video stream')
        return 'video'
    raise SystemExit(f'{p.name}: use {", ".join(IMAGES)}' + ('' if image_only else f' or {", ".join(VIDEOS)}'))


def job_folder(job):
    if not re.fullmatch(r'[0-9a-f]{32}', job or ''):
        raise SystemExit('expected a 32-character local job ID')
    folder = RUNS / job
    if not folder.is_dir():
        raise SystemExit(f'job {job} does not exist')
    return folder


def job_meta(folder):
    p = folder / 'zeug-job.json'
    return json.loads(p.read_text()) if p.exists() else {}


def save_meta(folder, meta):
    (folder / 'zeug-job.json').write_text(json.dumps(meta, indent=2))


def job_env(folder=None, pitch=None):
    """Pin the vocal pitch for every stage of a job, so a later stage rebuilds the same input."""
    if pitch is None and folder is not None:
        pitch = job_meta(folder).get('pitch_semitones')
        if pitch is None:
            manifest = folder / 'audio-manifest.json'
            pitch = json.loads(manifest.read_text()).get('pitch_semitones') if manifest.exists() else None
    env = dict(os.environ)
    if pitch is not None:  # otherwise GENJUTSU_PITCH_SEMITONES or the upstream +3 applies
        env['GENJUTSU_PITCH_SEMITONES'] = str(pitch)
    return env


def workflow(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / 'workflow.py'), *map(str, args)], cwd=ROOT, env=env).returncode


def outcome(folder, response, final):
    if (folder / final).exists():
        return 'completed'
    return 'submitted' if (folder / response).exists() else 'not-billed'


# ---------------------------------------------------------------- commands
def cmd_prepare(a):
    a.video = str(Path(a.video).resolve())  # workflow.py runs from the repo root
    info = probe(a.video)
    if a.mode != 'depth' and not (ROOT / '.venv-face/bin/python').exists():
        raise SystemExit('face mesh modes need the worker: bash setup-face-mesh.sh (Python 3.12)')
    if info['seconds'] < 4:
        print('note: a video identity reference (Omni) needs at least 4 s; image references are fine.')
    stage = 'prepare:' + a.mode
    calls, total = ledger.guard(stage, a.allow_paid, a.provider)
    proc = subprocess.Popen([sys.executable, str(ROOT / 'workflow.py'), 'prepare', a.video, '--mode', a.mode,
                             '--subject', a.subject, '--provider', a.provider], cwd=ROOT, env=job_env(pitch=a.pitch),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    job = None
    for line in proc.stdout:
        print(line, end='', flush=True)
        m = re.match(r'JOB: ([0-9a-f]{32})', line)
        job = job or (m and m.group(1))
    code = proc.wait()
    if job:
        folder = RUNS / job
        save_meta(folder, {'job': job, 'created_at': now(), 'source': Path(a.video).name, 'probe': info,
                           'mode': a.mode, 'subject': a.subject, 'provider': a.provider,
                           'pitch_semitones': json.loads((folder / 'audio-manifest.json').read_text()).get('pitch_semitones')
                           if (folder / 'audio-manifest.json').exists() else a.pitch})
        ledger.record(folder, stage, 'ok' if code == 0 else 'failed', calls=calls, estimate_usd=total,
                      provider_ids=ledger.provider_ids(folder))
        if code == 0:
            print(f'\nNext: .venv/bin/python -m zeug.gj review-assist {job}')
    return code


def cmd_review_assist(a):
    from zeug import review_assist
    folder = job_folder(a.job)
    report = review_assist.run(folder, every=a.every, vision=a.vision)
    print(f'{len(report["sheets"])} sheet(s) in {folder / "review-assist"}; report: {folder / "review-assist.md"}')
    for flag in report['flags']:
        print('  check:', flag)
    print(f'fingerprint {report["fingerprint"]}\n{report["note"]}')
    return 0


def cmd_prompt(a):
    from zeug import prompt
    identities = [prompt.parse_identity(s) for s in a.identity]
    dialogue = Path(a.dialogue_file).read_text() if a.dialogue_file else None
    text = prompt.build(identities, a.scene, a.mode, dialogue, a.positions.split(',') if a.positions else None)
    if a.out:
        Path(a.out).write_text(text)
        print(f'wrote {a.out}')
    print(text, end='')
    return 0


def cmd_submit(a):
    folder = job_folder(a.job)
    for name in ('prompt_file', 'reference', 'second_reference'):  # workflow.py runs from the repo root
        if getattr(a, name):
            setattr(a, name, str(Path(getattr(a, name)).resolve()))
    meta = job_meta(folder)
    if (folder / 'seedance-response.json').exists():
        raise SystemExit('this job already has a provider response: use resume, or prepare a new job for a variation')
    kind = check_reference(a.reference)
    if a.second_reference:
        check_reference(a.second_reference, image_only=True)
    prompt_text = Path(a.prompt_file).read_text().strip()
    if not prompt_text:
        raise SystemExit('the prompt file is empty')
    print('Creative prompt:\n' + prompt_text)
    print(f'\nMode: {"multi_reference (Omni), @Video2 is the identity" if kind == "video" else "edit, @Image1 is the identity"}.'
          ' workflow.py appends the audio and reference lines; the full payload lands in seedance-request.json.')
    calls, total = ledger.guard('submit', a.allow_paid)
    meta.update(references=[Path(a.reference).name] +
                ([Path(a.second_reference).name] if a.second_reference else []), submitted_at=now())
    save_meta(folder, meta)
    args = ['submit', a.job, '--prompt-file', a.prompt_file, '--reference', a.reference]
    if a.second_reference:
        args += ['--second-reference', a.second_reference]
    code = workflow(*args, env=job_env(folder))
    ledger.record(folder, 'submit', outcome(folder, 'seedance-response.json', 'final-preview.mp4'), calls=calls,
                  estimate_usd=total, provider_ids=ledger.provider_ids(folder))
    return code


def cmd_hd(a):
    folder = job_folder(a.job)
    if not (folder / 'final-preview.mp4').exists():
        raise SystemExit('no completed draft yet: watch the draft before approving 1080p')
    calls, total = ledger.guard('hd', a.allow_paid)
    meta = job_meta(folder)
    meta.update(hd_approved_by=a.approved_by, hd_approved_at=now())
    save_meta(folder, meta)
    code = workflow('hd', a.job, env=job_env(folder))
    ledger.record(folder, 'hd', outcome(folder, 'hd/response.json', 'hd/final-preview.mp4'), calls=calls,
                  estimate_usd=total, provider_ids=ledger.provider_ids(folder), approved_by=a.approved_by)
    return code


def cmd_repair(a):
    folder = job_folder(a.job)
    calls, total = ledger.guard('repair-rgb', a.allow_paid)
    code = workflow('repair-rgb', a.job, env=job_env(folder))
    ledger.record(folder, 'repair-rgb', 'ok' if code == 0 else 'failed', calls=calls, estimate_usd=total)
    return code


def cmd_ledger(a):
    folders = [job_folder(a.job)] if a.job else sorted(p for p in RUNS.glob('*') if (p / 'zeug-ledger.jsonl').exists())
    counts, unpriced, total = {}, 0, 0.0
    for folder in folders:
        print(folder.name)
        for e in ledger.entries(folder):
            print(f'  {e["at"][:19]}  {e["stage"]:18} {e["outcome"]:10} {ledger.describe(e.get("estimate_usd"))}')
            if e['outcome'] != 'not-billed':
                for c in e.get('calls', []):
                    counts[c] = counts.get(c, 0) + 1
                if e.get('estimate_usd') is None:
                    unpriced += 1
                else:
                    total += e['estimate_usd']
    print('\ncalls: ' + (', '.join(f'{k} x{v}' for k, v in sorted(counts.items())) or 'none'))
    print(f'priced total ${total:.2f}' + (f'; {unpriced} stage run(s) unpriced' if unpriced else ''))
    return 0


def cmd_doctor(a):
    code = subprocess.run([sys.executable, str(ROOT / 'doctor.py'), *(['--require-keys'] if a.require_keys else []),
                           *(['--mesh'] if a.mesh else [])], cwd=ROOT).returncode
    load_env()
    print('\nZeug layer:')
    print('  vocal pitch: +' + os.environ.get('GENJUTSU_PITCH_SEMITONES', '3') + ' semitones (prepare --pitch N overrides)')
    print('  paid stages: ' + ('allowed by GENJUTSU_ALLOW_PAID=1' if ledger.allowed(False) else 'need --allow-paid'))
    unpriced = [k for k, v in ledger.prices().items() if v is None]
    print('  prices: ' + (f'{len(unpriced)} unpriced in zeug/prices.json' if unpriced else 'all priced'))
    vision = os.environ.get('GENJUTSU_VISION_BASE_URL') and os.environ.get('GENJUTSU_VISION_MODEL')
    print('  review-assist --vision: ' + ('configured' if vision else 'not configured (sheets still work)'))
    if os.environ.get('MEDIA_HOST') == 's3':
        bucket = os.environ.get('S3_MEDIA_BUCKET', '')
        ok = bool(bucket and shutil.which('aws')) and subprocess.run(
            ['aws', 's3api', 'head-bucket', '--bucket', bucket], capture_output=True).returncode == 0
        print(f'  s3 bucket {bucket or "(unset)"}: ' + ('reachable' if ok else 'NOT reachable with current AWS credentials'))
        code = code or (0 if ok else 1)
    return code


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog='zeug.gj', description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='command', required=True)
    c = sub.add_parser('prepare')
    c.add_argument('video')
    c.add_argument('--mode', choices=['depth', 'face_mesh', 'depth_mesh'], default='depth')
    c.add_argument('--subject', default='person', help='SAM 3 text prompt, e.g. "person" or "woman"')
    c.add_argument('--provider', choices=['replicate', 'replicate-chenxwh', 'huggingface'], default='replicate')
    c.add_argument('--pitch', type=int, help='semitones to shift the isolated vocals (default +3, upstream)')
    c.add_argument('--allow-paid', action='store_true')
    c = sub.add_parser('review-assist')
    c.add_argument('job')
    c.add_argument('--every', type=int, default=1, help='sheet every Nth frame (default 1 = every frame)')
    c.add_argument('--vision', action='store_true', help='also ask the configured vision model')
    c = sub.add_parser('prompt')
    c.add_argument('--identity', action='append', required=True, help='"@Image1=description", once per subject')
    c.add_argument('--scene', required=True, help='the place, never the people')
    c.add_argument('--mode', choices=['depth', 'face_mesh', 'depth_mesh'], default='depth')
    c.add_argument('--positions', help='comma list matching --identity order, e.g. left,right')
    c.add_argument('--dialogue-file', help='verified transcript of the source dialogue')
    c.add_argument('--out')
    c = sub.add_parser('submit')
    c.add_argument('job')
    c.add_argument('--prompt-file', required=True)
    c.add_argument('--reference', required=True)
    c.add_argument('--second-reference')
    c.add_argument('--allow-paid', action='store_true')
    c = sub.add_parser('hd')
    c.add_argument('job')
    c.add_argument('--approved-by', required=True)
    c.add_argument('--allow-paid', action='store_true')
    c = sub.add_parser('repair-rgb')
    c.add_argument('job')
    c.add_argument('--allow-paid', action='store_true')
    c = sub.add_parser('ledger')
    c.add_argument('job', nargs='?')
    c = sub.add_parser('doctor')
    c.add_argument('--require-keys', action='store_true')
    c.add_argument('--mesh', action='store_true')
    for name in ['status', 'resume', 'review']:
        sub.add_parser(name, add_help=False)
    a, rest = p.parse_known_args(argv)
    if a.command in ('status', 'resume', 'review'):
        folder = job_folder(rest[0]) if rest else None
        return workflow(a.command, *rest, env=job_env(folder))
    if rest:
        p.error('unrecognized arguments: ' + ' '.join(rest))
    return {'prepare': cmd_prepare, 'review-assist': cmd_review_assist, 'prompt': cmd_prompt, 'submit': cmd_submit,
            'hd': cmd_hd, 'repair-rgb': cmd_repair, 'ledger': cmd_ledger, 'doctor': cmd_doctor}[a.command](a)


if __name__ == '__main__':
    sys.exit(main())
