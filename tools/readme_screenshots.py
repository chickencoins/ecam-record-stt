"""Render documentation screenshots with transparent red DOM outlines."""
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from app import App,handler_for

ROOT=Path(__file__).resolve().parents[1]


def outline(page, selectors):
    page.evaluate('''selectors => {
      document.querySelectorAll('.doc-outline').forEach(e=>e.remove());
      for(const selector of selectors) {
        const box=document.querySelector(selector).getBoundingClientRect();
        const node=document.createElement('div');node.className='doc-outline';
        Object.assign(node.style,{position:'absolute',left:(box.left+scrollX-5)+'px',top:(box.top+scrollY-5)+'px',width:(box.width+10)+'px',height:(box.height+10)+'px',border:'3px solid #ed2929',borderRadius:'5px',background:'transparent',pointerEvents:'none',zIndex:'9999',boxSizing:'border-box'});
        document.body.appendChild(node);
      }
    }''',selectors)


with tempfile.TemporaryDirectory() as tmp:
    app=App(Path(tmp)/'data',Path(tmp)/'output')
    original_status=app.status
    def status():
        result=original_status()
        result['output']='.\\ecam_recordSTT_output'
        if result['result']:
            result['result']='.\\ecam_recordSTT_output\\sample_lecture.txt'
        return result
    app.status=status
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(app,'docs'))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page(viewport={'width':1120,'height':1150},device_scale_factor=1)
            page.goto(f'http://127.0.0.1:{server.server_port}/docs/')
            page.locator('#title').fill('샘플 강의')
            page.wait_for_timeout(1100)
            outline(page,['#title','#source','#start'])
            page.screenshot(path=str(ROOT/'docs/images/01-start.png'),full_page=True)
            page.locator('#screenshots').check();page.locator('#captureInterval').fill('30')
            outline(page,['#captureSettings','#paragraph','#device'])
            page.screenshot(path=str(ROOT/'docs/images/02-capture.png'),full_page=True)
            app.state='complete';app.progress=100;app.result='sample_lecture.txt'
            app.message='완료 · TXT 저장을 검증하고 임시 녹음을 삭제했습니다.'
            app.transcriber.device_used='CPU · INT8'
            page.wait_for_timeout(1300)
            outline(page,['#done','#result','#folder'])
            page.screenshot(path=str(ROOT/'docs/images/03-result.png'),full_page=True)
            browser.close()
    finally:
        server.shutdown();server.server_close()
