import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import io
import types

from core import Session, Transcriber, commit_transcript, paragraph_text


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def session(self, seconds=1):
        s = Session.create(self.root / 'pending', '테스트 강의', 16000, 'tab', 'large-v3')
        try:
            for _ in range(seconds):
                s.append(b'\x01\x00' * 16000)
        finally:
            s.close()
        return s

    def test_success_keeps_only_verified_txt(self):
        s = self.session()
        target, warning = commit_transcript(s, self.root / 'out', '[00:00:00] 실제 강의 내용', 'large-v3')
        self.assertIn('실제 강의 내용', target.read_text('utf-8-sig'))
        self.assertFalse(s.folder.exists())
        self.assertFalse(warning)

    def test_empty_transcript_preserves_recording(self):
        s = self.session()
        with self.assertRaises(ValueError):
            commit_transcript(s, self.root / 'out', '  ', 'large-v3')
        self.assertTrue(s.audio.exists())

    def test_failed_txt_readback_preserves_recording(self):
        s = self.session()
        with patch.object(Path, 'read_bytes', return_value=b'corrupt'):
            with self.assertRaises(IOError):
                commit_transcript(s, self.root / 'out', '받아쓴 내용', 'large-v3')
        self.assertTrue(s.audio.exists())

    def test_recording_error_preserves_original_after_transcription(self):
        s = self.session()
        s.meta['capture_error'] = '연결 끊김'
        target, warning = commit_transcript(s, self.root / 'out', '받아쓴 내용', 'large-v3')
        self.assertTrue(s.audio.exists())
        self.assertIn('연결 끊김', target.read_text('utf-8-sig'))
        self.assertTrue(warning)

    def test_two_hour_audio_is_fully_covered_with_bounded_chunks(self):
        s = Session.create(self.root / 'pending', '2시간', 16000, 'tab', 'large-v3')
        s.close()
        with s.audio.open('wb') as f:
            f.truncate(16000 * 2 * 7200)
        covered, count, end_before = 0, 0, 0
        for start, end, wav, pcm in s.chunks():
            self.assertEqual(start, end_before)
            self.assertLessEqual(end-start, 240)
            with wave.open(io.BytesIO(wav)) as w:
                self.assertEqual(w.getnframes(), len(pcm)//2)
            covered += len(pcm)
            end_before = end
            count += 1
        self.assertEqual(end_before, 7200)
        self.assertEqual(covered, 16000 * 2 * 7200)
        self.assertGreaterEqual(count, 30)

    def test_sequence_gap_never_silently_accepts_missing_audio(self):
        s = Session.create(self.root / 'pending', '순서', 16000, 'tab', 'large-v3')
        try:
            with self.assertRaises(ValueError):
                s.append(b'\0\0', 1)
            self.assertEqual(s.bytes, 0)
            s.append(b'\0\0', 0)
            self.assertEqual(s.bytes, 2)
        finally:
            s.close()

    def test_retry_uses_completed_chunk_and_keeps_source_on_failure(self):
        s = self.session(241)
        calls = []
        def transcribe(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('simulated inference failure')
            return iter([types.SimpleNamespace(start=0,end=1,text='테스트 문장')]), None
        engine = Transcriber(self.root / 'models')
        engine.local_model = lambda *args: types.SimpleNamespace(transcribe=transcribe)
        with self.assertRaises(RuntimeError):
            engine.run(s, self.root / 'out', 'large-v3', '', lambda *args:None)
        self.assertTrue(s.audio.exists())
        checkpoint = json.loads((s.folder / 'progress.json').read_text('utf-8'))
        self.assertEqual(len(checkpoint['chunks']), 1)
        target, _ = engine.run(s, self.root / 'out', 'large-v3', '', lambda *args:None)
        self.assertEqual(len(calls), 3)
        self.assertTrue(target.exists())
        self.assertFalse(s.audio.exists())

    def test_sentence_fragments_are_joined_without_content_changes(self):
        lines = ['[00:00:11] 오늘은 첫 번째 예제에서',
                 '[00:00:16] 변수를 살펴보겠습니다.']
        text = paragraph_text(lines, 60)
        self.assertEqual(text, '[00:00:11] 오늘은 첫 번째 예제에서 변수를 살펴보겠습니다.')

    def test_paragraph_does_not_break_in_middle_of_sentence(self):
        lines = ['[00:00:00] 첫 문장입니다.', '[00:00:58] 그 다음에는',
                 '[00:01:04] 배열을 배웁니다.', '[00:01:10] 다음 문단입니다.']
        self.assertEqual(paragraph_text(lines, 60),
            '[00:00:00] 첫 문장입니다. 그 다음에는 배열을 배웁니다.\n\n[00:01:10] 다음 문단입니다.')

    def test_original_format_and_unrecognized_text_are_preserved(self):
        lines = ['[00:00:00] 내용입니다.', '알 수 없는 형식도 삭제하지 않습니다.']
        self.assertEqual(paragraph_text(lines, 0), '\n\n'.join(lines))
        self.assertIn(lines[1], paragraph_text(lines, 60))

    def test_gpu_failure_retries_entire_chunk_without_duplicate_partial_text(self):
        s = self.session()
        engine = Transcriber(self.root / 'models')
        engine.device_used = 'GPU'
        def gpu_segments():
            yield types.SimpleNamespace(start=0,end=.2,text='불완전한 GPU 결과')
            raise RuntimeError('CUDA out of memory')
        gpu = types.SimpleNamespace(transcribe=lambda *a,**kw:(gpu_segments(),None))
        cpu = types.SimpleNamespace(transcribe=lambda *a,**kw:(iter([types.SimpleNamespace(start=0,end=1,text='완전한 CPU 결과입니다.')]),None))
        def load(*args, **kwargs):
            if kwargs.get('force_cpu'):
                engine.device_used = 'CPU'
                return cpu
            return gpu
        engine.local_model = load
        target,_ = engine.run(s,self.root/'out','large-v3','',lambda *args:None)
        text = target.read_text('utf-8-sig')
        self.assertIn('완전한 CPU 결과입니다.', text)
        self.assertNotIn('불완전한 GPU 결과', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
