"""Private media hosting: upload to your own S3 bucket and hand Enhancor a presigned GET URL.

Uses the AWS CLI rather than boto3, so credentials come from the normal AWS chain
(environment, profile or instance role). Expire objects with a bucket lifecycle rule;
ZEUG.md has the setup commands.
"""
import mimetypes, shutil, subprocess, uuid
from pathlib import Path
from config import setting

MAX_EXPIRY = 7 * 24 * 3600  # SigV4 presigned URLs cannot outlive seven days
TYPES = {'.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
         '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}


def _aws(*args):
    binary = shutil.which('aws')
    if not binary:
        raise ValueError('MEDIA_HOST=s3 needs the AWS CLI on PATH.')
    try:
        r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ValueError(f'aws {args[0]} {args[1]} timed out')
    if r.returncode != 0:
        raise ValueError(f'aws {args[0]} {args[1]} failed: ' + (r.stderr or r.stdout).strip()[-300:])
    return r.stdout.strip()


def object_key(path, prefix):
    prefix = prefix.strip('/')
    name = uuid.uuid4().hex + '/' + Path(path).name
    return prefix + '/' + name if prefix else name


def upload(path):
    path = Path(path)
    bucket = setting('S3_MEDIA_BUCKET')
    if not bucket:
        raise ValueError('Set S3_MEDIA_BUCKET to use MEDIA_HOST=s3.')
    expires = min(int(setting('S3_MEDIA_EXPIRES', '86400') or 86400), MAX_EXPIRY)
    region = setting('S3_MEDIA_REGION') or setting('AWS_REGION') or 'us-west-2'
    uri = f's3://{bucket}/' + object_key(path, setting('S3_MEDIA_PREFIX', 'genjutsu/'))
    ctype = TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    _aws('s3', 'cp', str(path), uri, '--only-show-errors', '--content-type', ctype, '--region', region)
    url = _aws('s3', 'presign', uri, '--expires-in', str(expires), '--region', region)
    if not url.startswith('https://'):
        raise ValueError('S3 presign did not return an HTTPS URL')
    return url
