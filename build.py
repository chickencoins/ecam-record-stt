"""Build one portable executable, excluding GPU runtime DLLs."""
from pathlib import Path
import importlib.metadata as metadata
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    notices = ROOT / 'build' / 'cpu-notices'
    notices.mkdir(parents=True, exist_ok=True)
    packages = []
    excluded = {'pip', 'setuptools', 'playwright', 'pyee', 'greenlet', 'pyinstaller',
                'pyinstaller-hooks-contrib', 'altgraph', 'pefile', 'pywin32-ctypes'}
    for distribution in metadata.distributions():
        name = distribution.metadata.get('Name', 'package')
        if name.lower() in excluded or name.lower().startswith('nvidia-'):
            continue
        packages.append({'name': name, 'version': distribution.version})
        for item in distribution.files or []:
            if any(part.lower().startswith(('license', 'copying', 'notice')) for part in item.parts):
                source = Path(distribution.locate_file(item))
                if source.is_file() and source.stat().st_size < 2_000_000:
                    target = notices / name / Path(*item.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
    (notices / 'packages.json').write_text(json.dumps(packages, indent=2), 'utf-8')
    spec = ROOT / 'build' / 'portable.spec'
    spec.write_text(f'''
from pathlib import PurePath
from PyInstaller.utils.hooks import collect_all, copy_metadata
datas = [({str(ROOT / 'web')!r}, 'web'), ({str(notices)!r}, 'third-party')]
datas += copy_metadata('huggingface-hub')
binaries = []
hiddenimports = []
for package in ('faster_whisper', 'ctranslate2', 'tokenizers', 'onnxruntime', 'av', 'pyaudiowpatch', 'PIL'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
a = Analysis([{str(ROOT / 'app.py')!r}], pathex=[{str(ROOT)!r}],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=['nvidia'], noarchive=False)
def keep(entry):
    name = PurePath(entry[0]).name.lower()
    return not (name.endswith('.dll') and name.startswith(
        ('cudnn', 'cublas', 'cudart', 'nvrtc', 'nvjitlink', 'nvblas', 'cufft', 'curand', 'cusolver', 'cusparse')))
a.binaries = [entry for entry in a.binaries if keep(entry)]
a.datas = [entry for entry in a.datas if keep(entry)]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='ecam_recordSTT',
    debug=False, strip=False, upx=False, console=False)
''', 'utf-8')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', str(spec)],
                   cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
