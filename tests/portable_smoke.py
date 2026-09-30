"""Silent EXE-only smoke test with a supplied WAV and a predownloaded model cache."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
import wave
from PIL import Image

parser=argparse.ArgumentParser()
parser.add_argument('--exe',required=True,type=Path)
parser.add_argument('--models',required=True,type=Path)
parser.add_argument('--sample',required=True,type=Path)
args=parser.parse_args()

with tempfile.TemporaryDirectory(prefix='ecam-portable-') as temporary:
    root=Path(temporary)
    portable=root/'portable';portable.mkdir()
    exe=portable/'ecam_recordSTT.exe'
    shutil.copyfile(args.exe,exe)
    data=root/'cache';models=data/'models'
    models.mkdir(parents=True)
    for source in args.models.rglob('*'):
        if source.is_file() and '.locks' not in source.parts:
            target=models/source.relative_to(args.models)
            target.parent.mkdir(parents=True,exist_ok=True)
            try:os.link(source,target)
            except OSError:shutil.copyfile(source,target)
    environment=dict(os.environ)
    environment['PATH']=str(Path(os.environ['SYSTEMROOT'])/'System32')
    environment['HF_HUB_OFFLINE']='1'
    for name in list(environment):
        if name.startswith(('CUDA_PATH','PYTHONPATH','PYTHONHOME')):environment.pop(name)
    assert list(portable.iterdir())==[exe]
    process=subprocess.Popen([str(exe),'--no-browser','--data-dir',str(data)],cwd=portable,env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
    url=None
    try:
        deadline=time.monotonic()+180
        print('Starting isolated EXE with system-only PATH...',flush=True)
        while time.monotonic()<deadline:
            if (data/'running.json').exists():
                url=json.loads((data/'running.json').read_text('utf-8'))['url'];break
            if process.poll() is not None:raise RuntimeError('EXE exited during startup')
            time.sleep(.5)
        assert url,'EXE startup timed out'
        def request(path,payload=None,binary=False):
            body=payload if binary else (json.dumps(payload).encode() if payload is not None else None)
            with urllib.request.urlopen(urllib.request.Request(url+path,data=body),timeout=30) as response:return json.load(response)
        status=request('status')
        assert Path(status['output'])==portable/'ecam_recordSTT_output',status
        with wave.open(str(args.sample),'rb') as wav:
            assert wav.getnchannels()==1 and wav.getsampwidth()==2
            rate=wav.getframerate();pcm=wav.readframes(wav.getnframes())
        sid=request('start',{'source':'tab','rate':rate,'title':'Sample','engine':'large-v3','screenshots':True,'capture_interval':5})['id']
        request(f'audio/{sid}/0',pcm,True)
        buffer=io.BytesIO();Image.new('RGB',(320,180),(20,80,140)).save(buffer,'PNG')
        for second in (0,5):request(f'image/{sid}/{second}',buffer.getvalue(),True)
        request('stop',{'seq':1})
        deadline=time.monotonic()+300
        print('Transcribing with bundled runtime...',flush=True)
        while time.monotonic()<deadline:
            status=request('status')
            if status['state'] in ('complete','error'):break
            time.sleep(1)
        assert status['state']=='complete',status
        assert status['device'].startswith('CPU'),status
        target=Path(status['result'])
        assert target.read_text('utf-8-sig').strip()
        assert sorted(p.name for p in target.with_suffix('').iterdir())==['000000s.png','000005s.png']
        assert not (data/'pending'/sid/'audio.pcm').exists()
        print(json.dumps({'passed':True,'device':status['device'],'images':status['image_count'],'output_next_to_exe':True,'exe_only':True}))
        request('exit',{});process.wait(timeout=30)
    finally:
        if process.poll() is None:
            if url:
                try:request('exit',{})
                except Exception:pass
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.terminate()
