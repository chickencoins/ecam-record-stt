import io
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from core import Session, commit_transcript


def png():
    stream=io.BytesIO()
    Image.new('RGB',(320,180),(20,80,140)).save(stream,format='PNG')
    return stream.getvalue()


class CaptureTests(unittest.TestCase):
    def test_images_survive_transcript_cleanup_and_share_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            session=Session.create(root/'pending','Sample',16000,'tab','large-v3')
            session.meta['screenshots']=True
            session.save_screenshot(root/'out',0,png())
            session.save_screenshot(root/'out',30,png())
            session.append(b'\0\0'*16000)
            session.close()
            target,_=commit_transcript(session,root/'out','A complete sentence.','large-v3')
            self.assertEqual(sorted(p.name for p in target.with_suffix('').iterdir()),['000000s.png','000030s.png'])
            self.assertFalse(session.audio.exists())

    def test_disabled_capture_and_bad_images_do_not_write_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            session=Session.create(root/'pending','Sample',16000,'tab','large-v3')
            try:
                with self.assertRaises(ValueError):session.save_screenshot(root/'out',0,png())
                session.meta['screenshots']=True
                with self.assertRaises(ValueError):session.save_screenshot(root/'out',-1,png())
                with self.assertRaises(ValueError):session.save_screenshot(root/'out',0,b'not an image')
                self.assertFalse((root/'out').exists())
            finally:session.close()


if __name__=='__main__':unittest.main()
