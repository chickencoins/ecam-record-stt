"""Durable audio sessions and full-transcript generation. No microphone input."""
from __future__ import annotations

import array
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
import wave
import sys

_DLL_HANDLES = []
_GPU_PATHS_READY = False


def enable_gpu_libraries():
    """Resolve optional externally installed CUDA libraries for this process."""
    global _GPU_PATHS_READY
    if _GPU_PATHS_READY or os.name != 'nt':
        return
    roots = []
    if os.environ.get('ECAM_GPU_DIR'):
        roots.append(Path(os.environ['ECAM_GPU_DIR']))
    if os.environ.get('CUDA_PATH'):
        roots.append(Path(os.environ['CUDA_PATH']) / 'bin')
    if not getattr(sys, 'frozen', False):
        vendor = Path(sys.prefix) / 'Lib' / 'site-packages' / 'nvidia'
        roots.extend(p for p in vendor.glob('*/bin'))
        roots.extend(p for p in vendor.glob('*/lib'))
    valid = list(dict.fromkeys(str(p.resolve()) for p in roots if p.is_dir()))
    for path in valid:
        _DLL_HANDLES.append(os.add_dll_directory(path))
    if valid:
        os.environ['PATH'] = os.pathsep.join(valid) + os.pathsep + os.environ.get('PATH', '')
    _GPU_PATHS_READY = True


def atomic_write(path: Path, data: bytes):
    part = path.with_name(path.name + '.writing')
    with part.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def stamp(seconds):
    n = max(0, int(seconds))
    return f'{n // 3600:02}:{n // 60 % 60:02}:{n % 60:02}'


def paragraph_text(lines, seconds=60):
    """Merge short recognition fragments; only break after sentence punctuation."""
    if seconds == 0:
        return '\n\n'.join(lines)
    paragraphs, words = [], []
    first_time = None
    pattern = re.compile(r'^\[(\d+):(\d{2}):(\d{2})\]\s*(.*)$', re.S)
    sentence_end = re.compile(r'(?:[.!?。！？][\"\u201d\u2019\u0027\u0029\u005d]*|(?:습니다|입니다|합니다|됩니다|있어요|없어요|거예요|이에요|예요|해요|돼요|죠|네요|고요|구요))$')

    def flush():
        if words:
            paragraphs.append(f'[{stamp(first_time)}] ' + ' '.join(words))
            words.clear()

    for line in lines:
        match = pattern.match(line)
        if not match:
            # Preserve unexpected/legacy content verbatim rather than dropping it.
            flush()
            first_time = None
            paragraphs.append(line)
            continue
        h, m, s, text = match.groups()
        current = int(h) * 3600 + int(m) * 60 + int(s)
        if words and current - first_time >= seconds and sentence_end.search(words[-1]):
            flush()
            first_time = None
        if not words:
            first_time = current
        words.append(text.strip())
    flush()
    return '\n\n'.join(paragraphs)


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip(' .')[:90]
    if not name or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        name = '강의'
    return name


class Session:
    def __init__(self, folder: Path, meta: dict, create=False):
        self.folder, self.meta = folder, meta
        self.lock = threading.RLock()
        self.audio = folder / 'audio.pcm'
        self.rate = int(meta['rate'])
        self.bytes = self.audio.stat().st_size if self.audio.exists() else 0
        self.seq = int(meta.get('seq', 0))
        self.level = 0.0
        self.last_audio = time.monotonic()
        self.file = None
        if create:
            folder.mkdir(parents=True)
            self.file = self.audio.open('ab')
            self.save()

    @classmethod
    def create(cls, root, title, rate, source, engine):
        if rate not in range(8000, 192001):
            raise ValueError('지원하지 않는 샘플링 주파수입니다.')
        sid = dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        return cls(root / sid, {'id': sid, 'title': safe_name(title), 'rate': rate,
                               'source': source, 'engine': engine, 'state': 'recording',
                               'created': dt.datetime.now().isoformat(timespec='seconds'),
                               'capture_error': ''}, True)

    def save(self):
        self.meta['seq'] = self.seq
        atomic_write(self.folder / 'session.json', json.dumps(self.meta, ensure_ascii=False).encode('utf-8'))

    @property
    def output_stem(self):
        return f"{self.meta['title']}_{self.meta['id']}"

    def save_screenshot(self, output, seconds, data):
        from PIL import Image
        with self.lock:
            if self.meta['state'] != 'recording' or not self.meta.get('screenshots'):
                raise ValueError('이미지 캡처가 활성화된 녹음이 아닙니다.')
            if not isinstance(seconds, int) or not 0 <= seconds <= 604800:
                raise ValueError('잘못된 캡처 시간입니다.')
            if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 32_000_000:
                raise ValueError('유효한 PNG 이미지가 아닙니다.')
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width < 1 or picture.height < 1 or picture.width * picture.height > 40_000_000:
                    raise ValueError('지원하지 않는 이미지 크기입니다.')
                picture.verify()
            image_dir = output / self.output_stem
            image_dir.mkdir(parents=True, exist_ok=True)
            target = image_dir / f'{seconds:06d}s.png'
            if target.exists():
                if target.read_bytes() == data:
                    return
                raise ValueError('같은 시간의 이미지가 이미 저장되어 있습니다.')
            atomic_write(target, data)
            if target.read_bytes() != data:
                raise IOError('이미지 저장을 검증하지 못했습니다.')
            self.meta['image_count'] = self.meta.get('image_count', 0) + 1
            self.meta['image_dir'] = str(image_dir)
            self.save()

    def append(self, data: bytes, seq=None):
        with self.lock:
            if self.file is None or self.meta['state'] != 'recording':
                raise ValueError('녹음이 진행 중이지 않습니다.')
            if len(data) % 2 or len(data) > 4_000_000:
                raise ValueError('잘못된 오디오 데이터입니다.')
            if seq is not None and seq != self.seq:
                raise ValueError(f'오디오 순서 오류: {self.seq}번째 데이터가 필요합니다.')
            self.file.write(data)
            self.file.flush()
            os.fsync(self.file.fileno())
            self.bytes += len(data)
            self.seq += 1
            self.last_audio = time.monotonic()
            samples = array.array('h', data)
            self.level = max((abs(v) for v in samples), default=0) / 32768

    def close(self, error=''):
        with self.lock:
            if self.file:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.close()
                self.file = None
            if error:
                self.meta['capture_error'] = error
            self.meta['state'] = 'ready'
            self.save()

    @property
    def seconds(self):
        return self.bytes / (self.rate * 2)

    def chunks(self, seconds=240):
        """Bound memory; prefer a low-energy boundary in the final 12 seconds."""
        import numpy as np
        total = self.audio.stat().st_size // 2
        start = 0
        with self.audio.open('rb') as f:
            while start < total:
                end = min(total, start + self.rate * seconds)
                if end < total:
                    scan_start = end - self.rate * 12
                    f.seek(scan_start * 2)
                    samples = np.frombuffer(f.read((end - scan_start) * 2), dtype='<i2').astype(np.float32)
                    width = self.rate // 5
                    blocks = samples[:len(samples) // width * width].reshape(-1, width)
                    energies = np.mean(blocks * blocks, axis=1)
                    end = scan_start + int(np.argmin(energies)) * width + width // 2
                f.seek(start * 2)
                pcm = f.read((end - start) * 2)
                if len(pcm) != (end - start) * 2:
                    raise IOError('녹음 데이터가 예상보다 짧습니다. 원본을 보존합니다.')
                buf = io.BytesIO()
                with wave.open(buf, 'wb') as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self.rate)
                    w.writeframes(pcm)
                yield start / self.rate, end / self.rate, buf.getvalue(), pcm
                start = end


def commit_transcript(session: Session, output: Path, body: str, engine: str):
    """Only remove owned recording files after an exact durable TXT readback."""
    if not body.strip():
        raise ValueError('인식된 말이 없습니다. 빈 TXT를 성공으로 처리하지 않고 녹음을 보존합니다.')
    output.mkdir(parents=True, exist_ok=True)
    warning = session.meta.get('capture_error', '')
    header = (f"{session.meta['title']}\n녹음 시작: {session.meta['created']}\n"
              f"음성 길이: {stamp(session.seconds)} | 모델: {engine}\n"
              '시간은 영상 시간이 아닌 녹음 시작 기준입니다. 자동 전사에는 오인식이 있을 수 있습니다.\n')
    if warning:
        header += f'주의: 녹음이 정상 종료되지 않았습니다. 원본 보존. {warning}\n'
    if session.meta.get('screenshots'):
        header += f"이미지: {session.meta.get('image_count', 0)}장 | 폴더: {session.output_stem}\n"
        if session.meta.get('image_error'):
            header += '이미지 캡처 알림: ' + session.meta['image_error'] + '\n'
    data = (header + '\n' + body.strip() + '\n').encode('utf-8-sig')
    target = output / f'{session.output_stem}.txt'
    if target.exists():
        raise FileExistsError('같은 이름의 TXT가 이미 있습니다. 기존 파일과 녹음을 보존합니다.')
    atomic_write(target, data)
    actual = target.read_bytes()
    if actual != data or not actual.decode('utf-8-sig').strip():
        raise IOError('TXT 저장 검증 실패. 녹음 파일은 삭제하지 않았습니다.')
    session.meta.update(state='complete', output=str(target), txt_sha256=hashlib.sha256(data).hexdigest())
    session.save()
    cleanup_error = ''
    if not warning:
        try:
            # Exact, application-owned files only. No recursive deletion.
            for name in ('audio.pcm', 'progress.json', 'session.json'):
                (session.folder / name).unlink(missing_ok=True)
            session.folder.rmdir()
        except OSError as e:
            cleanup_error = f'TXT 저장 완료. 임시 파일 일부를 지우지 못했습니다: {e}'
    else:
        cleanup_error = '녹음 중 오류가 있어 확인용 원본을 보존했습니다.'
    return target, cleanup_error


class Transcriber:
    def __init__(self, models: Path):
        self.models = models
        self.loaded = None
        self.loaded_id = None
        self.device_preference = 'cpu'
        self.device_used = ''
        self.device_note = ''

    def release(self):
        import gc
        self.loaded = None
        self.loaded_id = None
        gc.collect()

    def local_model(self, model_id, status, force_cpu=False):
        use_gpu = self.device_preference != 'cpu' and not force_cpu
        desired = (model_id, use_gpu)
        if self.loaded_id != desired:
            status('모델 준비 중 · 처음에는 다운로드가 필요합니다. 창을 열어 두세요.', 0)
            os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
            os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
            self.release()
            try:
                model_path = download_model(model_id, cache_dir=str(self.models), local_files_only=True)
            except Exception:
                model_path = download_model(model_id, cache_dir=str(self.models))
            self.device_note = ''
            if use_gpu:
                try:
                    enable_gpu_libraries()
                    import ctypes
                    import ctranslate2
                    if ctranslate2.get_cuda_device_count() < 1:
                        raise RuntimeError('사용 가능한 NVIDIA GPU가 없습니다.')
                    if os.name == 'nt':
                        for library in ('cublas64_12.dll', 'cudnn64_9.dll'):
                            ctypes.WinDLL(library)
                    status('NVIDIA GPU에 모델 준비 중…', 0)
                    self.loaded = WhisperModel(model_path, device='cuda', compute_type='int8_float16', num_workers=1)
                    self.device_used = 'GPU · CUDA INT8/FP16'
                except Exception as exc:
                    self.release()
                    self.device_note = 'GPU를 준비하지 못해 CPU로 전환했습니다: ' + str(exc)[:200]
            if self.loaded is None:
                self.loaded = WhisperModel(model_path, device='cpu', compute_type='int8',
                                           cpu_threads=max(1, min(8, (os.cpu_count() or 4) - 2)),
                                           download_root=str(self.models))
                self.device_used = 'CPU · INT8'
            self.loaded_id = desired
        return self.loaded

    def run(self, session, output, engine, terms, status):
        import numpy as np
        if session.seconds < .5:
            raise ValueError('녹음이 너무 짧습니다. 녹음을 보존했습니다.')
        if engine not in ('large-v3', 'turbo'):
            raise ValueError('지원하지 않는 모델입니다.')
        model = self.local_model(engine, status)
        progress_path = session.folder / 'progress.json'
        fingerprint = hashlib.sha256((engine + '\n' + terms).encode()).hexdigest()
        checkpoint = {'fingerprint': fingerprint, 'chunks': []}
        if progress_path.exists():
            old = json.loads(progress_path.read_text('utf-8'))
            if old.get('fingerprint') == fingerprint:
                checkpoint = old
        lines = []
        for index, (begin, end, wav_bytes, pcm) in enumerate(session.chunks()):
            status(f'전사 중 · {stamp(begin)} / {stamp(session.seconds)} · {self.device_used}', begin / session.seconds * 100)
            if index < len(checkpoint['chunks']):
                lines.extend(checkpoint['chunks'][index])
                continue
            chunk_lines = []
            peak = np.max(np.abs(np.frombuffer(pcm, dtype='<i2').astype(np.int32))) if pcm else 0
            if peak > 0:
                def recognize(active_model):
                    result = []
                    segments, _ = active_model.transcribe(io.BytesIO(wav_bytes), language='ko', task='transcribe',
                        beam_size=5, vad_filter=True, vad_parameters={'threshold': .35, 'min_silence_duration_ms': 700, 'speech_pad_ms': 400},
                        initial_prompt=terms.strip() or None, condition_on_previous_text=False)
                    for seg in segments:
                        if seg.text.strip():
                            result.append(f'[{stamp(begin + seg.start)}] {seg.text.strip()}')
                        status(f'전사 중 · {stamp(begin + seg.end)} / {stamp(session.seconds)} · {self.device_used}', min(99, (begin + seg.end) / session.seconds * 100))
                    return result
                try:
                    chunk_lines = recognize(model)
                except Exception:
                    if not self.device_used.startswith('GPU'):
                        raise
                    # Discard this chunk's partial text and retry the complete chunk once on CPU.
                    model = None
                    self.release()
                    status('GPU 처리 오류 · 현재 구간을 CPU로 다시 처리합니다.', begin / session.seconds * 100)
                    model = self.local_model(engine, status, force_cpu=True)
                    self.device_note = 'GPU 처리 오류로 CPU에서 이어서 전사했습니다.'
                    chunk_lines = recognize(model)
            checkpoint['chunks'].append(chunk_lines)
            atomic_write(progress_path, json.dumps(checkpoint, ensure_ascii=False).encode('utf-8'))
            lines.extend(chunk_lines)
        status('TXT 저장 및 내용 검증 중…', 99)
        body = paragraph_text(lines, int(session.meta.get('paragraph_seconds', 60)))
        return commit_transcript(session, output, body, engine)
