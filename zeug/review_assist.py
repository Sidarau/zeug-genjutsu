"""Review assist: every-frame contact sheets and mask statistics, plus optional vision-model notes.

It never approves anything. Approval stays with `workflow.py review --approve`, recorded by a
reviewer who inspected every frame; the sheets make that possible for an agent that reads images
but cannot play video. The report is bound to the current artifact fingerprint.
"""
import base64, datetime, json, os
from pathlib import Path
import av, numpy as np
from PIL import Image, ImageDraw
import mask_review

ROWS = 4
JUMP = 0.35          # relative mask-area change between consecutive frames worth a look
VISION_PROMPT = """You are quality control for a video subject-replacement pipeline.
{clips}
Report, with timestamps, every problem you see:
- parts of the subject left in original colors (hair, hands, feet, clothing edges)
- background painted as subject (chairs, props, other people)
- holes in the subject, flicker between frames, misalignment with the original
- frames where the subject disappears{mesh}
End with exactly one line, "Verdict: looks clean" or "Verdict: needs repair", then list the frames to check first."""


def _frames(path):
    with av.open(str(path)) as c:
        for f in c.decode(video=0):
            yield f.to_ndarray(format='rgb24')


def _mode(folder):
    p = Path(folder) / 'workflow-mode.json'
    return json.loads(p.read_text()).get('mode', 'depth') if p.exists() else 'depth'


def mask_stats(mask_path):
    coverage, edges = [], []
    for a in _frames(mask_path):
        m = a[:, :, 0] > 127
        coverage.append(float(m.mean()))
        edges.append(bool(m[:, 0].any() or m[:, -1].any()))
    if not coverage:
        raise ValueError('mask video has no frames')
    jumps = [i for i in range(1, len(coverage))
             if max(coverage[i], coverage[i - 1]) > 0
             and abs(coverage[i] - coverage[i - 1]) / max(coverage[i], coverage[i - 1]) > JUMP]
    return {'frames': len(coverage), 'coverage_min': round(min(coverage), 4),
            'coverage_mean': round(sum(coverage) / len(coverage), 4), 'coverage_max': round(max(coverage), 4),
            'empty_frames': [i for i, c in enumerate(coverage) if c == 0],
            'coverage_jumps': jumps, 'side_edge_frames': sum(edges)}


def _cell(arrays, width, label):
    parts = []
    for a in arrays:
        im = Image.fromarray(a)
        parts.append(im.resize((width, max(2, round(im.height * width / im.width)))))
    tile = Image.new('RGB', (width * len(parts), max(p.height for p in parts)))
    for i, p in enumerate(parts):
        tile.paste(p, (i * width, 0))
    draw = ImageDraw.Draw(tile)
    draw.rectangle((0, 0, 7 * len(label) + 8, 14), fill=(0, 0, 0))
    draw.text((4, 2), label, fill=(255, 255, 255))
    return tile


def _save_sheet(cells, cols, path):
    w, h = cells[0].size
    rows = -(-len(cells) // cols)
    sheet = Image.new('RGB', (cols * w + (cols - 1) * 4, rows * h + (rows - 1) * 4), (40, 40, 40))
    for i, c in enumerate(cells):
        sheet.paste(c, ((i % cols) * (w + 4), (i // cols) * (h + 4)))
    sheet.save(path, quality=85)
    return path


def sheets(folder, out, every=1, width=200, fps=24):
    """Contact sheets of every `every`-th frame: mask | prepared input (prepared only for face mesh)."""
    folder, out = Path(folder), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob('sheet-*.jpg'):
        old.unlink()
    names = ['composite-silent.mp4'] if _mode(folder) == 'face_mesh' else ['mask.mp4', 'composite-silent.mp4']
    cols = 6 if len(names) == 1 else 4
    cells, paths, count = [], [], 0
    for i, group in enumerate(zip(*(_frames(folder / n) for n in names))):
        count = i + 1
        if i % every:
            continue
        cells.append(_cell(group, width, f'f{i} {i / fps:.2f}s'))
        if len(cells) == cols * ROWS:
            paths.append(_save_sheet(cells, cols, out / f'sheet-{len(paths) + 1:03d}.jpg'))
            cells = []
    if cells:
        paths.append(_save_sheet(cells, cols, out / f'sheet-{len(paths) + 1:03d}.jpg'))
    return paths, count


def _small_clip(src, dst, max_side=480, keep_every=3, fps=8):
    with av.open(str(src)) as inp, av.open(str(dst), 'w') as out:
        stream = None
        n = 0
        for i, frame in enumerate(inp.decode(video=0)):
            if i % keep_every:
                continue
            im = frame.to_image()
            scale = min(1, max_side / max(im.size))
            size = (max(2, int(im.width * scale) // 2 * 2), max(2, int(im.height * scale) // 2 * 2))
            if stream is None:
                stream = out.add_stream('libx264', rate=fps)
                stream.width, stream.height, stream.pix_fmt = size[0], size[1], 'yuv420p'
            vf = av.VideoFrame.from_image(im.resize(size))
            vf.pts = n
            n += 1
            for packet in stream.encode(vf):
                out.mux(packet)
        if stream is not None:
            for packet in stream.encode():
                out.mux(packet)
    return dst


def vision_notes(folder, out):
    """Ask an OpenAI-compatible vision model for QA notes. Returns None when not configured."""
    import requests
    base = os.environ.get('GENJUTSU_VISION_BASE_URL', '').rstrip('/')
    model = os.environ.get('GENJUTSU_VISION_MODEL', '')
    key = os.environ.get('GENJUTSU_VISION_API_KEY', '')
    if not (base and model):
        return None
    folder, mode = Path(folder), _mode(folder)
    clips = [('prepared input: the subject painted as guidance over the original background', 'composite-silent.mp4')]
    if mode != 'face_mesh':
        clips.append(('binary mask: white is the subject', 'mask.mp4'))
    parts = [{'type': 'text', 'text': VISION_PROMPT.format(
        clips='\n'.join(f'Clip {i + 1} is the {d}.' for i, (d, _) in enumerate(clips)),
        mesh='\n- face-mesh tracking gaps or a mesh that drifts off the face' if 'mesh' in mode else '')}]
    for _, name in clips:
        small = _small_clip(folder / name, out / ('vision-' + name))
        parts.append({'type': 'video_url', 'video_url': {
            'url': 'data:video/mp4;base64,' + base64.b64encode(small.read_bytes()).decode()}})
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    r = requests.post(base + '/chat/completions', headers=headers, timeout=600,
                      json={'model': model, 'max_tokens': 16000, 'messages': [{'role': 'user', 'content': parts}]})
    if not r.ok:
        raise RuntimeError(f'vision model HTTP {r.status_code}: {r.text[:300]}')
    data = r.json()
    notes = (data['choices'][0]['message'].get('content') or '').strip()
    # Reasoning models can spend the whole budget thinking and return no text.
    return {'model': model, 'notes': notes or 'model returned no text (output budget spent on reasoning)',
            'usage': data.get('usage') or {}}


def run(folder, every=1, vision=False):
    folder = Path(folder)
    out = folder / 'review-assist'
    fingerprint = mask_review.fingerprint(folder)
    mode = _mode(folder)
    paths, frames = sheets(folder, out, every)
    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'fingerprint': fingerprint,
              'mode': mode, 'frames': frames, 'every': every, 'sheets': [str(p.relative_to(folder)) for p in paths],
              'flags': [], 'note': 'Assist only. Approval still requires workflow.py review --approve by a reviewer '
                                   'who inspected every frame.'}
    if mode != 'face_mesh':
        stats = report['mask'] = mask_stats(folder / 'mask.mp4')
        if stats['empty_frames']:
            report['flags'].append(f'subject missing in frames {stats["empty_frames"][:20]}')
        if stats['coverage_jumps']:
            report['flags'].append(f'mask area jumps >{int(JUMP * 100)}% at frames {stats["coverage_jumps"][:20]} '
                                   '(a cut, or flicker)')
    if 'mesh' in mode and (folder / 'face-tracking.json').exists():
        tracking = report['tracking'] = json.loads((folder / 'face-tracking.json').read_text())
        if tracking.get('missing_frames'):
            report['flags'].append(f'face not tracked in frames {tracking["missing_frames"][:20]}')
    if vision:
        report['vision'] = vision_notes(folder, out) or {'notes': 'not configured: set GENJUTSU_VISION_BASE_URL '
                                                                  'and GENJUTSU_VISION_MODEL'}
    (folder / 'review-assist.json').write_text(json.dumps(report, indent=2))
    lines = [f'# Review assist · {folder.name}', '', f'- Mode: {mode} · {frames} frames · '
             f'{len(paths)} sheet(s), every {"" if every == 1 else f"{every}th "}frame',
             f'- Fingerprint: `{fingerprint}`', '', '## Check first', '']
    lines += [f'- {f}' for f in report['flags']] or ['- nothing flagged by the statistics; still inspect every sheet']
    if report.get('vision'):
        lines += ['', '## Vision model notes (advisory)', '', report['vision']['notes']]
    lines += ['', '## Sheets', ''] + [f'- {s}' for s in report['sheets']] + ['', report['note']]
    (folder / 'review-assist.md').write_text('\n'.join(lines) + '\n')
    return report
