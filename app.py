from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from core import Session, Transcriber, atomic_write
from recorder import SystemRecorder

ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'web'
HOME = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent


class App:
    def __init__(self, data, output):
        self.data, self.output = data, output
        self.sessions = data / 'pending'
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        self.transcriber = Transcriber(data / 'models')
        self.lock = threading.RLock()
        self.session = None
        self.recorder = None
        self.state = 'idle'
        self.message = '준비되었습니다. Start를 누른 뒤 강의를 재생하세요.'
        self.progress = 0
        self.result = ''
        self.last_seen = time.monotonic()
        self.terms = ''
        self.engine = 'large-v3'
        self.device = 'cpu'
        self.paragraph_seconds = 60
        self.shutdown_event = threading.Event()

    def update(self, message, progress=0):
        with self.lock:
            self.message, self.progress = message, progress

    def pending(self):
        result = []
        for file in sorted(self.sessions.glob('*/session.json'), reverse=True):
            try:
                meta = json.loads(file.read_text('utf-8'))
                if (file.parent / 'audio.pcm').exists():
                    result.append({'id': file.parent.name, 'title': meta['title'], 'created': meta['created']})
            except (OSError, ValueError, KeyError):
                pass
        return result

    def status(self):
        with self.lock:
            self.last_seen = time.monotonic()
            return {'state': self.state, 'message': self.message, 'progress': self.progress,
                    'seconds': self.session.seconds if self.session else 0,
                    'level': self.session.level if self.session else 0,
                    'output': str(self.output), 'result': self.result,
                    'device': self.transcriber.device_used,
                    'device_note': self.transcriber.device_note,
                    'image_count': self.session.meta.get('image_count', 0) if self.session else 0,
                    'image_error': self.session.meta.get('image_error', '') if self.session else '',
                    'image_dir': self.session.meta.get('image_dir', '') if self.session else '',
                    'pending': self.pending() if self.state not in ('recording', 'transcribing') else []}

    def options(self, data):
        engine = data.get('engine', 'large-v3')
        if engine not in ('large-v3', 'turbo'):
            raise ValueError('알 수 없는 STT 모델입니다.')
        device = data.get('device', 'cpu')
        paragraph = int(data.get('paragraph_seconds', 60))
        if device not in ('auto', 'cpu') or paragraph not in (0, 30, 60, 120, 300):
            raise ValueError('처리 장치와 문단 길이를 확인하세요.')
        self.device, self.paragraph_seconds = device, paragraph
        self.engine, self.terms = engine, str(data.get('terms', ''))[:2000]

    def start(self, data):
        with self.lock:
            if self.state in ('recording', 'transcribing'):
                raise ValueError('이미 작업 중입니다.')
            self.options(data)
            source = data.get('source', 'tab')
            if source not in ('tab', 'system'):
                raise ValueError('녹음 범위를 확인하세요.')
            screenshots = data.get('screenshots', False) is True
            capture_interval = int(data.get('capture_interval', 30))
            if not 1 <= capture_interval <= 3600:
                raise ValueError('캡처 간격은 1~3600초여야 합니다.')
            if screenshots and source != 'tab':
                raise ValueError('이미지 캡처는 Chrome 탭 모드에서 사용할 수 있습니다.')
            rate = int(data.get('rate', 16000))
            recorder = None
            if source == 'system':
                recorder = SystemRecorder()
                rate, _ = recorder.prepare()
            try:
                self.session = Session.create(self.sessions, data.get('title', '강의'), rate, source, self.engine)
                self.session.meta['paragraph_seconds'] = self.paragraph_seconds
                self.session.meta.update(screenshots=screenshots, capture_interval=capture_interval,
                                         image_count=0, image_error='')
                self.session.save()
                self.recorder = recorder
                if recorder:
                    recorder.start(self.session)
                self.state, self.result, self.progress = 'recording', '', 0
                self.message = '녹음 중 · 강의가 끝나면 Stop을 누르세요.'
                return {'id': self.session.meta['id']}
            except Exception:
                if recorder:
                    recorder.stop()
                if self.session and self.session.file:
                    self.session.close('녹음 시작 실패')
                raise

    def append(self, sid, seq, data):
        with self.lock:
            if self.state != 'recording' or not self.session or sid != self.session.meta['id']:
                raise ValueError('녹음 세션이 일치하지 않습니다.')
            self.session.append(data, seq)

    def screenshot(self, sid, seconds, data):
        with self.lock:
            if self.state != 'recording' or not self.session or sid != self.session.meta['id']:
                raise ValueError('녹음 세션이 일치하지 않습니다.')
            self.session.save_screenshot(self.output, seconds, data)

    def stop(self, error='', expected_seq=None, image_error=''):
        with self.lock:
            if self.state != 'recording':
                raise ValueError('녹음 중이지 않습니다.')
            if self.recorder:
                self.recorder.stop()
                error = error or self.recorder.error
                self.recorder = None
            if expected_seq is not None and self.session.seq != expected_seq:
                error = error or '일부 오디오를 수신하지 못했습니다.'
            self.session.close(error)
            if image_error:
                self.session.meta['image_error'] = image_error[:1000]
                self.session.save()
            if error:
                self.state = 'error'
                self.message = error + ' 수신된 녹음은 보존했습니다. 아래에서 재시도할 수 있습니다.'
                return
            self.begin_transcription()

    def begin_transcription(self):
        self.state, self.progress = 'transcribing', 0
        self.result = ''
        self.transcriber.device_preference = self.device
        self.message = '전체 전사 준비 중…'
        args = (self.session, self.engine, self.terms)
        threading.Thread(target=self.transcribe, args=args, daemon=True).start()

    def transcribe(self, session, engine, terms):
        try:
            target, warning = self.transcriber.run(session, self.output, engine, terms, self.update)
            with self.lock:
                self.result = str(target)
                self.state, self.progress = 'complete', 100
                self.message = warning or '완료 · TXT 저장을 검증하고 임시 녹음을 삭제했습니다.'
                if session.meta.get('image_error'):
                    self.message += '\n이미지 캡처 중 일부 오류가 있었습니다. TXT의 알림을 확인하세요.'
        except Exception as e:
            with self.lock:
                self.state = 'error'
                self.message = f'변환 실패: {e}\n녹음은 보존되어 있습니다. 아래에서 재시도하세요.'
        finally:
            # Return GPU memory to other applications after every completed/failed job.
            self.transcriber.release()

    def retry(self, data):
        with self.lock:
            if self.state in ('recording', 'transcribing'):
                raise ValueError('현재 작업이 끝난 뒤 재시도하세요.')
            sid = data.get('id', '')
            known = {p['id'] for p in self.pending()}
            if sid not in known:
                raise ValueError('복구할 녹음이 없습니다.')
            self.options(data)
            folder = self.sessions / sid
            meta = json.loads((folder / 'session.json').read_text('utf-8'))
            if meta['state'] == 'recording':
                meta['capture_error'] = '이전 실행에서 녹음이 비정상 종료되었습니다. 녹음된 구간만 전사합니다.'
            self.session = Session(folder, meta)
            self.session.meta['paragraph_seconds'] = self.paragraph_seconds
            self.session.save()
            self.begin_transcription()

    def prepare_model(self, data):
        with self.lock:
            if self.state in ('recording', 'transcribing'):
                raise ValueError('작업이 끝난 뒤 모델을 준비하세요.')
            engine = data.get('engine', 'large-v3')
            if engine not in ('large-v3', 'turbo'):
                raise ValueError('지원하지 않는 모델입니다.')
            self.options(data)
            self.transcriber.device_preference = self.device
            self.state = 'transcribing'
            def work():
                try:
                    self.transcriber.local_model(engine, self.update)
                    self.update('모델 준비 완료. 이제 Start를 누르세요.', 100)
                    self.state = 'idle'
                except Exception as e:
                    self.update(f'모델 준비 실패: {e}')
                    self.state = 'error'
                finally:
                    self.transcriber.release()
            threading.Thread(target=work, daemon=True).start()

    def guard(self, server):
        active = False
        while not self.shutdown_event.wait(2):
            busy = self.state in ('recording', 'transcribing')
            if os.name == 'nt' and busy != active:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if busy else 0x80000000)
                active = busy
            if self.state == 'recording' and self.recorder and self.recorder.error:
                self.stop(self.recorder.error)
            if self.state == 'recording' and self.session.meta['source'] == 'tab' and time.monotonic() - self.session.last_audio > 25:
                self.stop('탭 오디오 수신이 25초 이상 중단되었습니다.')
            if time.monotonic() - self.last_seen > 100:
                if self.state == 'recording':
                    self.stop('녹음 창과 연결이 끊겼습니다.')
                if self.state != 'transcribing':
                    self.shutdown_event.set()
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        server.shutdown()


def handler_for(app, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, data, code=200, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def route(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                raise ValueError('잘못된 호스트입니다.')
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + host:
                raise ValueError('다른 웹사이트의 요청은 허용되지 않습니다.')
            prefix = '/' + token + '/'
            path = urlsplit(self.path).path
            if not path.startswith(prefix):
                raise ValueError('잘못된 접근입니다.')
            return path[len(prefix):]

        def do_GET(self):
            try:
                route = self.route()
                if route == 'status':
                    self.send(app.status())
                elif route in ('', 'index.html', 'app.js', 'style.css', 'worklet.js', 'capture-worker.js'):
                    name = route or 'index.html'
                    kind = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css'}[name.split('.')[-1]]
                    self.send((ASSETS / name).read_bytes(), content_type=kind + '; charset=utf-8')
                elif route == 'result' and app.result:
                    self.send(Path(app.result).read_bytes(), content_type='text/plain; charset=utf-8')
                else:
                    self.send({'error': '찾을 수 없습니다.'}, 404)
            except Exception as e:
                self.send({'error': str(e)}, 400)

        def do_POST(self):
            try:
                route = self.route()
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > (32_000_000 if route.startswith('image/') else 4_000_000):
                    raise ValueError('요청 크기가 너무 큽니다.')
                payload = self.rfile.read(size)
                if route.startswith('image/'):
                    _, sid, seconds = route.split('/')
                    app.screenshot(sid, int(seconds), payload)
                    self.send({'ok': True})
                    return
                if route.startswith('audio/'):
                    _, sid, seq = route.split('/')
                    app.append(sid, int(seq), payload)
                    self.send({'ok': True})
                    return
                data = json.loads(payload or b'{}')
                if route == 'start':
                    self.send(app.start(data))
                    return
                if route == 'stop':
                    app.stop(str(data.get('error', ''))[:1000], data.get('seq'), str(data.get('image_error', ''))[:1000])
                elif route == 'retry':
                    app.retry(data)
                elif route == 'model':
                    app.prepare_model(data)
                elif route == 'folder':
                    os.startfile(app.output)
                elif route == 'open':
                    if app.result:
                        os.startfile(app.result)
                elif route == 'exit':
                    if app.state in ('recording', 'transcribing'):
                        raise ValueError('Stop 후 변환이 끝나면 종료해 주세요.')
                    app.shutdown_event.set()
                else:
                    self.send({'error': '찾을 수 없습니다.'}, 404)
                    return
                self.send({'ok': True})
            except Exception as e:
                self.send({'error': str(e)}, 400)
    return Handler


def launch(url):
    candidates = [Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
                  Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
                  Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe']
    for executable in candidates:
        if executable.exists():
            subprocess.Popen([str(executable), '--app=' + url, '--window-size=1120,950'])
            return
    import webbrowser
    webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    data = args.data_dir or Path(os.environ.get('LOCALAPPDATA', str(HOME))) / 'ecam_recordSTT'
    output = args.output_dir or HOME / 'ecam_recordSTT_output'
    data.mkdir(parents=True, exist_ok=True)
    # Keep one app instance per data directory. The lock is released by Windows after a crash.
    import msvcrt
    lock_path = data / 'instance.lock'
    if not lock_path.exists():
        lock_path.write_bytes(b'0')
    lock = lock_path.open('r+b')
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        info = json.loads((data / 'running.json').read_text('utf-8'))
        launch(info['url'])
        return
    app = App(data, output)
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app, token))
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}/{token}/'
    atomic_write(data / 'running.json', json.dumps({'url': url, 'pid': os.getpid()}).encode())
    threading.Thread(target=app.guard, args=(server,), daemon=True).start()
    if not args.no_browser:
        launch(url)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        (data / 'running.json').unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    # Libraries expect writable stdout/stderr even in a windowed PyInstaller executable.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')
    try:
        main()
    except Exception as exc:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'ecam recordSTT 실행 오류', 0x10)
        else:
            raise
