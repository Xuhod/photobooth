"""Block accidental publication of booth data before committing or pushing."""

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


# The already-reviewed Canva assets are allowed only with their original Git blob IDs.
APPROVED_ASSETS = '50b432c'
WEB_TEMPLATE = ('template/Template_photobooth_transparan.png', '100644',
                '84d87cfbd96e527d1b1f44aae0ef4fd55827bae3')
BLOCKED_PATH = re.compile(
    r'(?:^|/)(?:jobs|sessions|\.venv|venv|__pycache__|credentials\.json|client_secret_.*\.json|'
    r'token\.json[^/]*|\.env(?:\.[^/]*)?|[^/]+\.(?:pem|pfx|p12|key))(?=/|$)', re.I
)
SECRET = re.compile(
    rb'AIza[0-9A-Za-z_-]{35}|GOCSPX-[A-Za-z0-9_-]{12,}|ya29\.[A-Za-z0-9_-]{12,}|'
    rb'1//[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
    rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|'
    rb'"(?:client_secret|refresh_token|access_token|private_key)"\s*:\s*"[^"\r\n]{8,}"', re.I
)
DRIVE_LINK = re.compile(
    rb'https?://drive\.google\.com/(?:file/d/|drive/(?:u/\d+/)?folders/|(?:open|uc)\?id=)'
    rb'([A-Za-z0-9_-]+)', re.I
)


def git(*args):
    return subprocess.check_output(['git', *args])


def tree(ref, *paths):
    for row in git('ls-tree', '-rz', ref, '--', *paths).split(b'\0'):
        if row:
            meta, path = row.split(b'\t', 1)
            mode, _, oid = meta.decode().split()
            yield path.decode('utf-8', errors='backslashreplace'), mode, oid


def staged():
    for row in git('ls-files', '--stage', '-z').split(b'\0'):
        if row:
            meta, path = row.split(b'\t', 1)
            mode, oid, _ = meta.decode().split()
            yield path.decode('utf-8', errors='backslashreplace'), mode, oid


def outgoing(lines):
    commits = set()
    for line in lines:
        _, local, _, remote = line.split()
        if not local.strip('0'):
            continue  # Ref deletion publishes no objects.
        tip = git('rev-parse', '--verify', f'{local}^{{commit}}').decode().strip()
        if local != tip:
            raise ValueError('Annotated tags are not scanned; this repo only permits commit refs.')
        remotes = []
        if remote.strip('0') and subprocess.run(
            ['git', 'cat-file', '-e', f'{remote}^{{commit}}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            remotes.append(f'^{remote}')
        commits.update(git('rev-list', tip, *remotes).decode().splitlines())
    for commit in commits:
        if content_issue(git('show', '-s', '--format=%B', commit), ''):
            raise ValueError(f'Sensitive content in commit message {commit[:12]}.')
        yield from tree(commit)


def content_issue(data, path):
    if SECRET.search(data):
        return 'looks like credentials'
    for match in DRIVE_LINK.finditer(data):
        if path != 'test_camera_app.py' or match[1] not in (b'test', b'strip'):
            return 'looks like a real Drive link'
    return None


def local_oauth_values():
    values = set()
    for path in (Path('credentials.json'), Path('token.json')):
        if path.is_file():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            for fields in (data, data.get('installed', {})):
                for key in ('client_id', 'client_secret', 'token', 'refresh_token'):
                    value = fields.get(key)
                    if isinstance(value, str) and len(value) >= 12:
                        values.add(value.encode())
    return values


def check(entries):
    approved = set(tree(APPROVED_ASSETS, 'Template'))
    approved.add(WEB_TEMPLATE)
    approved.add(('web-booth/' + WEB_TEMPLATE[0], *WEB_TEMPLATE[1:]))
    oauth_values = local_oauth_values()
    seen = set()
    blocked = False
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        path, mode, oid = entry
        if BLOCKED_PATH.search(path):
            reason = 'private/local file'
        elif entry in approved:
            continue
        elif mode not in ('100644', '100755'):
            reason = 'not a regular file'
        elif PurePosixPath(path).suffix.lower() in {'.svg', '.jsonl', '.csv', '.log'}:
            reason = 'unreviewed image, export or log'
        elif int(git('cat-file', '-s', oid)) > 2_000_000:
            reason = 'unreviewed large file'
        else:
            data = git('cat-file', 'blob', oid)
            try:
                data.decode('utf-8-sig')
                if b'\0' in data:
                    reason = 'unreviewed binary data'
                elif any(value in data for value in oauth_values):
                    reason = 'contains a local OAuth value'
                else:
                    reason = content_issue(data, path)
            except UnicodeDecodeError:
                reason = 'unreviewed binary data'
        if reason:
            print(f'Privacy guard: {path!r}: {reason}', file=sys.stderr)
            blocked = True
    return int(blocked)


if __name__ == '__main__':
    try:
        mode = sys.argv[1]
        sys.exit(check(staged() if mode == 'staged' else outgoing(sys.stdin)))
    except (ValueError, OSError, AttributeError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        sys.exit(f'Privacy guard could not complete: {message}')
