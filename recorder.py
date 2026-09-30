"""WASAPI output-only fallback; never opens a microphone."""
import threading


class SystemRecorder:
    def __init__(self):
        self.pa = self.stream = None
        self.error = ''

    def prepare(self):
        import pyaudiowpatch as pa
        self.pa = pa.PyAudio()
        try:
            info = self.pa.get_default_wasapi_loopback()
            self.info = info
            self.rate = int(info['defaultSampleRate'])
            self.channels = min(2, int(info['maxInputChannels']))
            return self.rate, info['name']
        except Exception:
            self.pa.terminate()
            self.pa = None
            raise

    def start(self, session):
        import numpy as np
        import pyaudiowpatch as pa

        def callback(data, count, timing, flags):
            try:
                if flags:
                    raise RuntimeError(f'오디오 버퍼 오류 ({flags}). 누락 가능성이 있어 녹음을 보존합니다.')
                samples = np.frombuffer(data, dtype='<i2').reshape(-1, self.channels)
                mono = samples.astype(np.int32).mean(axis=1).astype('<i2').tobytes()
                session.append(mono)
                return (None, pa.paContinue)
            except Exception as e:
                self.error = str(e)
                return (None, pa.paAbort)

        self.stream = self.pa.open(format=pa.paInt16, channels=self.channels, rate=self.rate,
            input=True, input_device_index=self.info['index'], frames_per_buffer=4096,
            stream_callback=callback)

    def stop(self):
        if self.stream:
            try:
                self.stream.stop_stream()
            finally:
                self.stream.close()
                self.stream = None
        if self.pa:
            self.pa.terminate()
            self.pa = None
