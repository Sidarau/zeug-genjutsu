"""Spend guard and per-job ledger for every billed provider call.

Billed stages refuse to run without --allow-paid (or GENJUTSU_ALLOW_PAID=1). Each run appends
to runs/<job>/zeug-ledger.jsonl with the calls made and the provider IDs they returned, so a
cost can be reconciled against the provider dashboards later.
"""
import datetime, json, os
from pathlib import Path

# Billed calls per stage. Prices live in prices.json (USD per call, null = not priced yet).
STAGES = {
    'prepare:depth': ['replicate:demucs', 'replicate:depth', 'replicate:sam3'],
    'prepare:depth_mesh': ['replicate:demucs', 'replicate:depth', 'replicate:sam3'],
    'prepare:face_mesh': ['replicate:demucs'],
    'repair-rgb': ['replicate:sam3'],
    'submit': ['enhancor:seedance-draft'],
    'hd': ['enhancor:seedance-1080p'],
}
PRICES_FILE = Path(__file__).with_name('prices.json')
SAVED = {  # where workflow.py persists each billed call's provider response
    'replicate:demucs': ('audio-prediction.json', 'id'),
    'replicate:depth': ('depth-prediction.json', 'id'),
    'replicate:sam3': ('sam-raw-request.json', 'id'),
    'enhancor:seedance-draft': ('seedance-response.json', 'requestId'),
    'enhancor:seedance-1080p': ('hd/response.json', 'requestId'),
}


def calls_for(stage, provider='replicate'):
    calls = list(STAGES[stage])
    if provider == 'huggingface':
        calls = ['huggingface:depth' if c == 'replicate:depth' else c for c in calls]
    return calls


def prices():
    try:
        data = json.loads(PRICES_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith('_')}


def estimate(calls):
    table = prices()
    known = [table.get(c) for c in calls]
    return None if any(p is None for p in known) else round(sum(known), 2)


def describe(total):
    return 'unpriced, fill zeug/prices.json' if total is None else f'about ${total:.2f}'


def allowed(flag):
    return bool(flag) or os.environ.get('GENJUTSU_ALLOW_PAID') == '1'


def guard(stage, flag, provider='replicate'):
    calls = calls_for(stage, provider)
    total = estimate(calls)
    if not allowed(flag):
        raise SystemExit(f'{stage} bills {", ".join(calls)} ({describe(total)}). '
                         'Re-run with --allow-paid once this spend is approved.')
    return calls, total


def provider_ids(folder):
    folder = Path(folder)
    found = {}
    for call, (name, field) in SAVED.items():
        path = folder / name
        try:
            ident = json.loads(path.read_text()).get(field)
        except (OSError, ValueError, AttributeError):
            continue
        if ident:
            found[call] = ident
    return found


def record(folder, stage, outcome, **extra):
    entry = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'stage': stage,
             'outcome': outcome, **extra}
    with (Path(folder) / 'zeug-ledger.jsonl').open('a') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def entries(folder):
    path = Path(folder) / 'zeug-ledger.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
