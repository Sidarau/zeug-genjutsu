# Zeug Lab fork

Fork of [sirioberati/Genjustsu-Open-Source-Workflow](https://github.com/sirioberati/Genjustsu-Open-Source-Workflow)
(MIT). Upstream files stay close to upstream so updates merge cleanly; Zeug additions live in `zeug/`
and `tests/test_zeug.py`.

## What the fork adds

| Area | Change |
|---|---|
| `zeug/gj.py` | CLI over `workflow.py`. It probes the source (30 s or less, has audio) before anything bills, applies the spend guard, writes a per-job ledger, and pins each job's vocal pitch so later stages rebuild the same input. |
| `zeug/ledger.py`, `zeug/prices.json` | Paid stages refuse to run without `--allow-paid` (or `GENJUTSU_ALLOW_PAID=1`). Every stage appends to `runs/<job>/zeug-ledger.jsonl` with its provider IDs. `gj ledger` totals the runs. |
| `zeug/review_assist.py` | Contact sheets of every frame (mask next to prepared input), mask statistics (missing subject, area jumps, edge fragments, face-tracking gaps) and optional notes from a vision model. It is bound to the artifact fingerprint and never approves. |
| `zeug/prompt.py` | Prompt builder for the identity mapping, a `[Scene]` line (describe the place, never the people, or the draft comes back grey), whole-subject replacement and verbatim dialogue. |
| `zeug/s3_host.py` | `MEDIA_HOST=s3`: a private bucket with presigned URLs instead of public tmpfiles/catbox. A private primary never falls back to a public host unless `MEDIA_HOST_ALLOW_PUBLIC_FALLBACK=1`. |
| Upstream fixes | The 30 s check now runs before Demucs is billed. The depth prediction ID is saved (`depth-prediction.json`), as the docs already claimed. A missing Replicate token gives a clear SAM error. The vocal pitch is configurable (`GENJUTSU_PITCH_SEMITONES`, default +3), and the Seedance prompt now states the pitch actually used. |

## Run

```bash
PYTHON=python3.12 bash setup.sh                 # venv + deps; needs rubberband (brew install rubberband cloudflared)
.venv/bin/python -m zeug.gj doctor
.venv/bin/python -m zeug.gj prepare clip.mp4 --allow-paid            # Demucs + depth + SAM 3 on Replicate
.venv/bin/python -m zeug.gj review-assist JOB                        # then read every sheet
.venv/bin/python -m zeug.gj review JOB --approve --fingerprint HASH --note "who reviewed, findings"
.venv/bin/python -m zeug.gj prompt --identity "@Image1=..." --scene "..." --out prompt.txt
.venv/bin/python -m zeug.gj submit JOB --prompt-file prompt.txt --reference me.png --allow-paid   # 480p draft
.venv/bin/python -m zeug.gj hd JOB --approved-by NAME --allow-paid   # 1080p from the draft_id, after approval
```

The UI (`bash start.command`) still works and uses the same `.env`. Keys come from the environment
first, then `.env`, so a secret manager can inject them into the process instead of a file.

## Private media bucket

```bash
B=your-genjutsu-media-bucket
aws s3api create-bucket --bucket $B --region us-west-2 --create-bucket-configuration LocationConstraint=us-west-2
aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket $B --lifecycle-configuration \
  '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{"Prefix":"genjutsu/"},"Expiration":{"Days":2}}]}'
```

Then set `MEDIA_HOST=s3` and `S3_MEDIA_BUCKET=$B`. Presigned URLs last `S3_MEDIA_EXPIRES` seconds
(default one day, capped at seven), or less if the AWS credentials are temporary session credentials.

## Staying current with upstream

```bash
git fetch upstream && git merge upstream/main
.venv/bin/python -m unittest discover -s tests && .venv/bin/python test_composite.py
```

Conflicts, if any, will be in the five lightly patched upstream files: `pipeline.py`, `media_host.py`,
`doctor.py`, `audio_workflow.py` and `seedance_bridge.py`.
