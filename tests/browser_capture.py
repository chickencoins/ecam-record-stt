"""Silent real-tab capture integration, including an off-screen/minimized window."""
import argparse
import hashlib
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from playwright.sync_api import sync_playwright, expect
from app import App, handler_for
from core import commit_transcript

parser=argparse.ArgumentParser()
parser.add_argument('--minimized',action='store_true')
args=parser.parse_args()

with tempfile.TemporaryDirectory() as tmp:
    app=App(Path(tmp)/'data',Path(tmp)/'ecam_recordSTT_output')
    app.transcriber.run=lambda s,o,e,t,status:commit_transcript(s,o,'[00:00:00] Sample transcript.',e)
    base=handler_for(app,'browser-test')
    class Handler(base):
        def do_GET(self):
            if self.path=='/fixture':
                body=b'''<!doctype html><title>Capture fixture</title><style>body{margin:0;background:#14508c;color:white;font:32px sans-serif}h1{padding:40px}</style><h1>Sample slide</h1><script>let n=0;setInterval(()=>{document.body.style.backgroundColor='rgb('+((++n*17)%180+20)+',80,140)';document.querySelector('h1').textContent='Sample slide '+n;},500);</script>'''
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
            else:super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    origin=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=not args.minimized,args=[
                '--enable-usermedia-screen-capturing','--auto-select-tab-capture-source-by-title=Capture fixture',
                '--window-position=-16000,-16000'])
            context=browser.new_context(no_viewport=True) if args.minimized else browser.new_context(viewport={'width':1280,'height':720})
            source=context.new_page();source.goto(origin+'/fixture')
            page=context.new_page();page.goto(origin+'/browser-test/')
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            assert not page.locator('#screenshots').is_checked()
            page.locator('#screenshots').check();page.locator('#captureInterval').fill('1')
            page.locator('#start').click()
            expect(page.locator('#state')).to_have_text('● 녹음 중',timeout=15000)
            page.wait_for_timeout(4000)
            initial=app.session.meta.get('image_count',0)
            assert initial>=2, (initial,page.locator('#message').inner_text(),errors)
            if args.minimized:
                cdp=context.new_cdp_session(source)
                window=cdp.send('Browser.getWindowForTarget')
                cdp.send('Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'windowState':'minimized'}})
            page.wait_for_timeout(6500)
            assert app.session.meta['image_count']>=initial+4,app.session.meta
            image_dir=Path(app.session.meta['image_dir'])
            pictures=sorted(image_dir.glob('*.png'))
            expected_size=page.evaluate('() => {const s=stream.getVideoTracks()[0].getSettings();return [s.width,s.height];}')
            with Image.open(pictures[-1]) as im:
                assert im.size==tuple(expected_size),(im.size,expected_size)
                assert im.width>=1000 and im.height>=600
                pixel=im.convert('RGB').getpixel((im.width-40,im.height-40))
                assert abs(pixel[1]-80)<=5 and abs(pixel[2]-140)<=5,pixel
            assert hashlib.sha256(pictures[-1].read_bytes()).digest()!=hashlib.sha256(pictures[-3].read_bytes()).digest()
            # Calling Stop through the page does not restore the minimized window.
            page.evaluate('() => stop()')
            expect(page.locator('#state')).to_have_text('저장 완료',timeout=30000)
            assert Path(app.result).with_suffix('')==image_dir
            assert not app.session.audio.exists()
            assert not errors,errors
            print(f'PASS: {len(pictures)} PNGs; background/minimized={args.minimized}; TXT stem matches; audio removed.')
            browser.close()
    finally:
        if app.session and app.session.file:app.session.close('test shutdown')
        server.shutdown();server.server_close()
