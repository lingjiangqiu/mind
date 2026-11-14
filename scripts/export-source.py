"""Export this project only, never the containing resume workspace.

Uses an allowlist, excludes credentials, databases, weights and generated files.
No Git credentials, repository creation, commit or upload is performed.
"""
from pathlib import Path
import hashlib
import json
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEST = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT.parent/'exports/campus-emotion-agent'
if DEST.exists():
    raise SystemExit('Destination already exists; choose a new empty output directory.')
if DEST == ROOT or ROOT in DEST.parents:
    raise SystemExit('Export destination must be outside project source.')

files = []
for name in ('README.md','SOURCE_PROVENANCE.md','.env.example','.gitignore','.dockerignore',
             'requirements-public.txt','Dockerfile.public','compose.demo.yml'):
    files.append(ROOT/name)
for folder in ('app','skills','tests','docs','.github'):
    for path in (ROOT/folder).rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and '.pytest_cache' not in path.parts and path.suffix != '.pyc':
            if path.as_posix().endswith(('/static/admin.html','/static/admin.js')):
                continue
            files.append(path)
files.append(Path(__file__).resolve())

# Stop on credential-shaped literals without printing matched values.
bad = []
for path in files:
    if path.name == '.env' or path.suffix.lower() in {'.db','.sqlite','.sqlite3','.xlsx','.gguf','.pem','.key'}:
        bad.append(path.relative_to(ROOT).as_posix())
        continue
    if path.suffix.lower() in {'.py','.md','.txt','.js','.json','.html','.yml','.example'}:
        text = path.read_text(encoding='utf-8')
        if re.search(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----', text):
            bad.append(path.relative_to(ROOT).as_posix())
if bad:
    raise SystemExit('Review possible sensitive artifacts in: ' + ', '.join(bad))
DEST.mkdir(parents=True)
manifest = {}
for path in files:
    name = path.relative_to(ROOT)
    output = DEST/name
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, output)
    manifest[name.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
(DEST/'data').mkdir()
(DEST/'data/.gitkeep').touch()
(DEST/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
archive = DEST.parent/(DEST.name+'-source-2026-11-11.zip')
with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
    for path in DEST.rglob('*'):
        if path.is_file():
            z.write(path, arcname=DEST.name+'/'+path.relative_to(DEST).as_posix())
print(json.dumps({'directory': str(DEST), 'archive': str(archive), 'files': len(manifest)+2,
                  'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2))
