"""Build and transfer only owned package and checkpoint artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from typing import Any



def build_wheel(destination: Path, project: Path | None = None) -> Path:
    """Build from a clean temporary copy so packaging cannot modify the checkout."""
    project = project or Path(__file__).resolve().parents[3]
    if not (project / "pyproject.toml").is_file():
        raise ValueError("Bootstrap requires a source checkout; configure --project to its root")
    source = destination / "source"
    source.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(project / name, source / name)
    for name in ("src", "licenses"):
        shutil.copytree(project / name, source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    output = destination / "dist"
    output.mkdir()
    options: dict[str, Any] = {"cwd": source, "capture_output": True, "timeout": 180,
                               "env": {**os.environ, "SOURCE_DATE_EPOCH": "315532800"}}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    uv = shutil.which("uv")
    if uv:
        command = [uv, "build", "--wheel", "--out-dir", str(output), str(source)]
    else:
        command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                   "--wheel-dir", str(output), str(source)]
    result = subprocess.run(command, **options)
    if result.returncode:
        raise RuntimeError("Package wheel build failed; install uv or pip/setuptools in the manager environment")
    wheels = list(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Package build did not produce exactly one wheel")
    return wheels[0]


def checkpoint_archive(source: Path, destination: Path) -> dict[str, dict[str, Any]]:
    """Reject links and record bytes actually archived, including file hashes."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Checkpoint must be a regular directory, not a symlink")
    source = source.resolve()
    if not (source / "config.json").is_file():
        raise ValueError("Checkpoint must contain config.json")
    manifest: dict[str, dict[str, Any]] = {}
    with tarfile.open(destination, "w") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Checkpoint links are not supported")
            if path.is_dir():
                continue
            if not path.is_file() or not path.resolve().is_relative_to(source):
                raise ValueError("Checkpoint contains a special or out-of-root file")
            relative = path.relative_to(source).as_posix()
            if relative == ".cloud-upload-manifest.json":
                raise ValueError("Checkpoint uses a reserved manifest filename")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                info = archive.gettarinfo(str(path), arcname=relative)
                archive.addfile(info, stream)
            manifest[relative] = {"size": info.size, "sha256": digest}
        if not manifest:
            raise ValueError("Checkpoint is empty")
    return manifest


STORE_WHEEL = """
import hashlib, os, pathlib, sys
target = pathlib.Path(sys.argv[1]); target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_suffix('.part')
h = hashlib.sha256()
with temporary.open('wb') as f:
    while block := sys.stdin.buffer.read(1024 * 1024):
        h.update(block); f.write(block)
if h.hexdigest() != sys.argv[2]:
    temporary.unlink(missing_ok=True); raise RuntimeError('wheel digest mismatch')
os.replace(temporary, target)
"""

BOOTSTRAP = """
import fcntl, json, os, pathlib, shutil, subprocess, sys
config = json.load(sys.stdin); root = pathlib.Path(config['root'])
root.mkdir(parents=True, exist_ok=True); os.chmod(root, 0o700)
lock = (root/'bootstrap.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
token = root / 'token'
if not token.exists():
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(config['token'])
uv = shutil.which('uv') or str(pathlib.Path.home() / '.local/bin/uv')
if not pathlib.Path(uv).is_file(): raise RuntimeError('uv is required on server')
release = root / 'releases' / config['digest']; venv = release / 'venv'
python = venv / 'bin/python'; marker = release / 'ready'
release.mkdir(parents=True,exist_ok=True)
def run(arguments: list[str]) -> None:
    with (release/'bootstrap.log').open('ab') as log:
        os.chmod(release/'bootstrap.log',0o600)
        result = subprocess.run(arguments,stdout=log,stderr=log)
    if result.returncode:
        print(json.dumps({'error':'Installation command failed; inspect '+str(release/'bootstrap.log')}))
        sys.exit(0)
if not marker.exists():
    run([uv, 'venv', '--python', sys.executable, str(venv)])
    run([uv, 'pip', 'install', '--python', str(python),config['wheel'] + '[cloud]'])
    marker.write_text(config['digest'])
env = os.environ.copy()
if config.get('runtime_python'):
    candidate = pathlib.Path(config['runtime_python'])
    if not candidate.is_file(): raise RuntimeError('runtime interpreter does not exist')
    env['LEROBOT_CLOUD_RUNTIME_PYTHON'] = str(candidate)
previous_file = root/'installation.json'
previous = json.loads(previous_file.read_text()) if previous_file.exists() else None
switched = False
if previous and previous['digest'] != config['digest']:
    status_result = subprocess.run([previous['python'],'-m','lerobot_monitor.cloud','daemon','status',
                                   '--root',str(root),'--port',str(config['port'])],capture_output=True,text=True)
    try: running = json.loads(status_result.stdout).get('running',False)
    except (ValueError,TypeError):
        print(json.dumps({'error':'Cannot verify existing daemon status; check it before upgrading'}));sys.exit(0)
    if running:
        if not config.get('upgrade'):
            print(json.dumps({'error':'A different build is running; use Upgrade to switch after sessions end'}));sys.exit(0)
        stopped = subprocess.run([previous['python'],'-m','lerobot_monitor.cloud','daemon','stop',
                                 '--root',str(root),'--port',str(config['port'])],capture_output=True,text=True)
        if stopped.returncode:
            print(json.dumps({'error':'Existing service refused to stop; finish active sessions before upgrading'}));sys.exit(0)
        switched = True
result = subprocess.run([str(python), '-m', 'lerobot_monitor.cloud', 'daemon', 'start',
                         '--root', str(root), '--port', str(config['port'])],
                        env=env, capture_output=True, text=True, timeout=45)
if result.returncode:
    if switched:
        restored = subprocess.run([previous['python'],'-m','lerobot_monitor.cloud','daemon','start',
                                  '--root',str(root),'--port',str(config['port'])],capture_output=True,text=True)
        reason = 'Previous service restored' if restored.returncode == 0 else 'Rollback failed; inspect service logs'
    else: reason = 'Inspect service logs'
    print(json.dumps({'error':'Cloud daemon start failed. '+reason}));sys.exit(0)
temporary = root/'installation.json.tmp'
temporary.write_text(json.dumps({'digest':config['digest'],'wheel':config['wheel'],
    'python':str(python), 'runtime_python':config.get('runtime_python')}))
os.replace(temporary,root/'installation.json')
print(json.dumps({'status':'started','digest':config['digest']}))
"""

EXTRACT_UPLOAD = """
import hashlib, json, os, pathlib, sys, tarfile
target = pathlib.Path(sys.argv[1]); target.mkdir(parents=True, exist_ok=False)
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    for member in archive:
        relative = pathlib.PurePosixPath(member.name)
        if relative.is_absolute() or '..' in relative.parts or not member.isfile():
            raise RuntimeError('unsafe archive member')
        destination = target.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(member) as source, destination.open('xb') as output:
            while block := source.read(1024 * 1024): output.write(block)
print(json.dumps({'staged':str(target)}))
"""

CLEANUP_UPLOAD = """
import json, pathlib, re, shutil, sys
root = pathlib.Path(sys.argv[1]).resolve(); target = pathlib.Path(sys.argv[2])
uploads = root/'uploads'
if (not re.fullmatch('[0-9a-f]{32}',target.name) or target.is_symlink()
        or target.parent.resolve() != uploads.resolve()
        or uploads.resolve() != uploads or target.resolve().parent != uploads):
    raise RuntimeError('refusing cleanup outside the exact owned upload directory')
if target.exists(): shutil.rmtree(target)
print(json.dumps({'removed':str(target)}))
"""

VERIFY_UPLOAD = """
import hashlib, json, pathlib, sys
manifest = json.load(sys.stdin); target = pathlib.Path(sys.argv[1])
files = {p.relative_to(target).as_posix():p for p in target.rglob('*') if p.is_file()}
if set(files) != set(manifest): raise RuntimeError('upload manifest does not match files')
for name, item in manifest.items():
    path = files[name]
    with path.open('rb') as f: digest = hashlib.file_digest(f,'sha256').hexdigest()
    if path.stat().st_size != item['size'] or digest != item['sha256']:
        raise RuntimeError('uploaded checkpoint checksum mismatch')
(target / '.cloud-upload-manifest.json').write_text(json.dumps(manifest))
print(json.dumps({'verified':True}))
"""

WRITE_RUNTIME_MANIFEST = """
profile = {'python':str(python),'profile':config['profile'],'lerobot_wheel_sha256':config['digest'],
           'huggingface_home':config.get('huggingface_home'),'lock':str(runtime/'requirements.lock')}
manifest_path = root/'runtime.json'
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
profiles = dict(manifest.get('profiles', {}))
if manifest.get('profile') and manifest.get('python'):
    profiles.setdefault(manifest['profile'],{key:manifest[key] for key in
        ('python','profile','lerobot_wheel_sha256','huggingface_home','lock') if key in manifest})
profiles[config['profile']] = profile
manifest = {**profile,'version':1,'profiles':profiles,'default_profile':config['profile']}
temporary = root/'runtime.json.tmp'; temporary.write_text(json.dumps(manifest)); os.replace(temporary,manifest_path)
print(json.dumps(profile))
"""

RUNTIME_REQUIREMENTS = """
extra = '[dataset]' if config['profile']=='act' else '[dataset,'+config['profile']+']'
requirements = runtime/'requirements.in'
requirements.write_text(config['wheel']+extra+'\\n'+installation['wheel']+'[cloud]\\n'
    +'torch==2.11.0+cu128\\ntorchvision==0.26.0+cu128\\n'
    +('transformers==5.5.4\\n' if config['profile']!='act' else ''))
"""

PREPARE_RUNTIME = """
import fcntl, json, os, pathlib, shutil, subprocess, sys
config = json.load(sys.stdin); root = pathlib.Path(config['root'])
lock = (root/'bootstrap.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
installation = json.loads((root/'installation.json').read_text())
runtime = root/'runtimes'/(config['digest']+'-'+config['profile']+'-'+installation['digest'][:12])
runtime.mkdir(parents=True,exist_ok=True)
uv = shutil.which('uv') or str(pathlib.Path.home()/'.local/bin/uv')
python = runtime/'venv/bin/python'
def run(arguments: list[str]) -> None:
    with (runtime/'install.log').open('ab') as log:
        os.chmod(runtime/'install.log',0o600)
        result = subprocess.run(arguments,stdout=log,stderr=log)
    if result.returncode:
        print(json.dumps({'error':'Runtime installation failed; inspect '+str(runtime/'install.log')}))
        sys.exit(0)
if not (runtime/'ready').exists():
    run([uv,'venv','--python',sys.executable,str(runtime/'venv')])
""" + textwrap.indent(RUNTIME_REQUIREMENTS.strip() + "\n", "    ") + """
    locked = runtime/'requirements.lock'
    if not locked.exists():
        run([uv,'pip','compile','--python',str(python),'--generate-hashes',
             '--extra-index-url','https://download.pytorch.org/whl/cu128',
             '--index-strategy','unsafe-best-match','--emit-index-url',
             str(requirements),'--output-file',str(locked)])
    run([uv,'pip','sync','--python',str(python),'--require-hashes',
         '--extra-index-url','https://download.pytorch.org/whl/cu128',
         '--index-strategy','unsafe-best-match',str(locked)])
    result = subprocess.run([uv,'pip','freeze','--python',str(python)],check=True,capture_output=True,text=True)
    (runtime/'installed.txt').write_text(result.stdout)
    check = ('import torch,datasets; from lerobot_monitor.policy import load_policy; '
             'from lerobot.policies.factory import get_policy_class,make_pre_post_processors; '
             'from lerobot.rollout.inference import RTCInferenceConfig,SyncInferenceConfig; '
             'from lerobot.rollout.inference.rtc import supports_rtc_inference; '
             'from lerobot.processor import RelativeActionsProcessorStep; '
             'from lerobot.policies.act.modeling_act import ACTPolicy; '
             'assert torch.cuda.is_available()')
    if config['profile']=='smolvla': check += '; from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy'
    if config['profile']=='pi': check += '; from lerobot.policies.pi0.modeling_pi0 import PI0Policy'
    run([str(python),'-c',check])
    (runtime/'ready').write_text(config['digest'])
""" + WRITE_RUNTIME_MANIFEST
