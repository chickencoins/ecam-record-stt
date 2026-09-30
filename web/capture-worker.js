'use strict';
let reader, canvas, context, timer, running = false, startedAt = 0, interval = 30;
let destination = '', nextDue = 0, lastSecond = -1, uploads = Promise.resolve(), failure = '';
let pending=0;

function elapsed() { return Math.max(0, (performance.timeOrigin + performance.now() - startedAt) / 1000); }
function capture() {
  if (!canvas || failure) return;
  if(pending>=3) {failure='이미지 저장이 지연되어 캡처를 중단했습니다.';postMessage({error:failure});return;}
  const second = Math.floor(elapsed());
  if (second === lastSecond) return;
  lastSecond = second;
  pending++;
  // convertToBlob snapshots the current canvas before the next video frame arrives.
  const blob = canvas.convertToBlob({type:'image/png'});
  uploads = uploads.then(async () => {
    const image = await blob;
    const response = await fetch(destination + second, {method:'POST',body:image,signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error((await response.json()).error || '이미지를 저장하지 못했습니다.');
    postMessage({saved:second});
  }).catch(error => { failure = error.message; postMessage({error:failure}); }).finally(()=>pending--);
}

async function receive(readable) {
  reader = readable.getReader();
  try {
    while (running) {
      const {done,value:frame} = await reader.read();
      if (done) break;
      try {
        if (!canvas || canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas = new OffscreenCanvas(frame.displayWidth, frame.displayHeight);
          context = canvas.getContext('2d', {alpha:false});
        }
        context.drawImage(frame,0,0,canvas.width,canvas.height);
        if (lastSecond < 0) { capture(); nextDue = elapsed() + interval; }
      } finally { frame.close(); }
    }
  } catch(error) {
    if (running) { failure = error.message; postMessage({error:failure}); }
  }
}

onmessage = async event => {
  const data = event.data;
  if (data.type === 'start') {
    startedAt=data.startedAt; interval=data.interval; destination=data.destination; running=true;
    timer=setInterval(()=>{ if(running && canvas && elapsed() >= nextDue) {capture();nextDue=elapsed()+interval;} },250);
    receive(data.readable);
    postMessage({ready:true});
  } else if (data.type === 'stop') {
    running=false;clearInterval(timer);
    if(reader) await reader.cancel().catch(()=>{});
    await uploads;
    postMessage({stopped:true,error:failure || (lastSecond < 0 ? '캡처할 영상 프레임을 받지 못했습니다.' : '')});
  }
};
