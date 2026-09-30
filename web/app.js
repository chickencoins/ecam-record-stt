'use strict';
const $ = id => document.getElementById(id);
let stream = null, context = null, node = null, sourceNode = null, sid = '', seq = 0;
let uploads = Promise.resolve(), uploadError = '', buffered = 0, stopping = false, starting = false;
let state = 'idle', recording = false, stoppedResolve = null, lastPending = '', localError = '';
let captureWorker=null, captureTrack=null, captureStopped=null, imageError='';
const base = new URL('.', location.href);
async function api(path, data) {
  const options = data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)};
  options.signal = AbortSignal.timeout(15000);
  const response = await fetch(new URL(path, base), options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '프로그램과 통신하지 못했습니다.');
  return result;
}
function settings() { return {title: $('title').value || '강의', source: $('source').value, engine: $('engine').value, terms: $('terms').value, device:$('device').value, paragraph_seconds:Number($('paragraph').value), screenshots:$('screenshots').checked, capture_interval:Number($('captureInterval').value)}; }
function error(e) { localError = e.message || String(e); $('message').textContent = localError; }
function labels() {
  $('prepare').disabled = ['recording','transcribing'].includes(state);
  const tab=$('source').value==='tab';
  if(!tab) $('screenshots').checked=false;
  $('screenshots').disabled=!tab || ['recording','transcribing'].includes(state);
  $('captureInterval').disabled=!$('screenshots').checked || ['recording','transcribing'].includes(state);
  $('sourceHint').textContent = tab ? '공유 창의 ‘Chrome 탭’에서 강의를 선택하고 ‘탭 오디오 공유’를 켜세요. 다른 탭의 소리는 제외됩니다.' : 'Windows 기본 출력 장치의 모든 소리를 녹음합니다. 이미지 캡처는 Chrome 탭 모드에서 사용할 수 있습니다.';
}
function queueAudio(buffer) {
  if (uploadError) return;
  const number = seq++;
  buffered++;
  if (buffered > 30) { uploadError = '오디오 저장이 지연되어 녹음을 중단했습니다.'; stop(uploadError); return; }
  uploads = uploads.then(async () => {
    if (uploadError) return;
    const r = await fetch(new URL(`audio/${sid}/${number}`, base), {method:'POST', body:buffer, signal:AbortSignal.timeout(15000)});
    if (!r.ok) throw new Error((await r.json()).error || '오디오 저장 오류');
  }).catch(e => { uploadError = e.message; setTimeout(() => stop(uploadError), 0); }).finally(() => buffered--);
}
async function release() {
  if(captureWorker) {captureWorker.terminate();captureWorker=null;}
  if(captureTrack) {captureTrack.stop();captureTrack=null;}
  if (node) { node.disconnect(); node = null; }
  if (sourceNode) { sourceNode.disconnect(); sourceNode = null; }
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  if (context) { await context.close().catch(()=>{}); context = null; }
}
async function start() {
  if (starting || recording) return;
  starting = true; localError = ''; $('start').disabled = true;
  const opts = settings();
  try {
    if(opts.screenshots && (opts.capture_interval < 1 || opts.capture_interval > 3600 || !Number.isInteger(opts.capture_interval))) throw new Error('캡처 간격은 1~3600초로 입력하세요.');
    if (opts.source === 'tab') {
      // Must be invoked directly by the Start click, before any asynchronous server request.
      stream = await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser',frameRate:1},
        audio:{suppressLocalAudioPlayback:false, echoCancellation:false, noiseSuppression:false, autoGainControl:false},
        selfBrowserSurface:'exclude', systemAudio:'exclude', surfaceSwitching:'exclude', monitorTypeSurfaces:'exclude'});
      if (stream.getVideoTracks()[0].getSettings().displaySurface !== 'browser') throw new Error('창 대신 ‘Chrome 탭’에서 강의 탭을 선택하세요. 전체 소리는 별도 녹음 범위를 사용하세요.');
      if (!stream.getAudioTracks().length) throw new Error('탭 오디오를 받지 못했습니다. 다시 Start를 누르고 ‘탭 오디오 공유’를 켜세요.');
      if(opts.screenshots && !window.MediaStreamTrackProcessor) throw new Error('이 브라우저는 이미지 캡처를 지원하지 않습니다. 최신 Chrome을 사용하세요.');
      context = new AudioContext({sampleRate:16000});
      await context.audioWorklet.addModule(new URL('worklet.js', base));
      await context.resume();
      opts.rate = context.sampleRate;
    }
    const result = await api('start', opts);
    sid = result.id; seq = 0; buffered = 0; uploads = Promise.resolve(); uploadError = ''; recording = true;
    imageError='';
    if (opts.source === 'tab') {
      node = new AudioWorkletNode(context, 'pcm-recorder');
      node.port.onmessage = e => {
        if (e.data.audio) queueAudio(e.data.audio);
        if (e.data.stopped && stoppedResolve) { stoppedResolve(); stoppedResolve = null; }
      };
      sourceNode = context.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
      sourceNode.connect(node); node.connect(context.destination); // Worklet output is silence; no duplicate playback.
      if(opts.screenshots) {
        captureTrack=stream.getVideoTracks()[0].clone();
        const processor=new MediaStreamTrackProcessor({track:captureTrack});
        captureWorker=new Worker(new URL('capture-worker.js',base));
        captureWorker.onmessage=e=>{
          if(e.data.error) imageError=e.data.error;
          if(e.data.stopped && captureStopped) {captureStopped();captureStopped=null;}
        };
        captureWorker.onerror=e=>{imageError=e.message || '이미지 캡처 작업이 중단되었습니다.';};
        captureWorker.postMessage({type:'start',readable:processor.readable,startedAt:performance.timeOrigin+performance.now(),interval:opts.capture_interval,destination:new URL(`image/${sid}/`,base).href},[processor.readable]);
      }
      stream.getTracks().forEach(track => track.addEventListener('ended', () => { if (recording && !stopping) stop(); }));
    }
    $('stop').disabled = false;
  } catch(e) {
    if (recording) { await api('stop', {error:e.message, seq}).catch(()=>{}); recording = false; }
    await release(); error(e);
  } finally { starting = false; poll(); }
}
async function stop(reason = '') {
  if (!recording || stopping) return;
  stopping = true; $('stop').disabled = true;
  try {
    if (node) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => { stoppedResolve = null; reject(new Error('마지막 오디오 조각을 받지 못했습니다.')); }, 5000);
        stoppedResolve = () => { clearTimeout(timer); resolve(); };
        node.port.postMessage('stop');
      }).catch(e => { reason = reason || e.message; });
    }
    await uploads;
    if(captureWorker) {
      await new Promise(resolve=>{
        const timeout=setTimeout(()=>{imageError=imageError || '이미지 저장 완료를 확인하지 못했습니다.';captureStopped=null;resolve();},25000);
        captureStopped=()=>{clearTimeout(timeout);resolve();};
        captureWorker.postMessage({type:'stop'});
      });
    }
    await api('stop', {error:reason || uploadError, seq:node ? seq : null, image_error:imageError});
  } catch(e) { error(e); }
  finally { recording = false; await release(); stopping = false; poll(); }
}
async function poll() {
  try {
    const s = await api('status');
    state = s.state;
    if (recording && state !== 'recording' && !stopping) { recording = false; await release(); }
    const busy = ['recording','transcribing'].includes(state);
    $('start').disabled = busy || starting;
    $('stop').disabled = state !== 'recording' || stopping;
    for (const id of ['title','source','engine','terms','paragraph','device']) $(id).disabled = busy || starting;
    $('screenshots').disabled=busy || starting || $('source').value!=='tab';
    $('captureInterval').disabled=busy || starting || !$('screenshots').checked;
    $('prepare').disabled = busy;
    $('exit').disabled = busy;
    $('state').textContent = {idle:'준비',recording:'● 녹음 중',transcribing:'텍스트 변환 중',complete:'저장 완료',error:'확인 필요'}[state] || state;
    const n = Math.floor(s.seconds); $('clock').textContent = [Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(v=>String(v).padStart(2,'0')).join(':');
    $('level').style.width = (Math.min(1,s.level*3)*100)+'%';
    $('message').textContent = localError || s.message;
    $('progress').value = s.progress;
    $('progress').hidden = state !== 'transcribing';
    $('output').textContent = s.output;
    $('result').textContent = s.result;
    $('deviceInfo').textContent = s.device ? `처리 장치: ${s.device}${s.device_note ? ' · '+s.device_note : ''}` : '';
    $('captureInfo').textContent=(imageError || s.image_error) ? '이미지 캡처: '+(imageError || s.image_error) : `저장된 이미지 ${s.image_count || 0}장`;
    $('captureInfo').hidden=!(s.image_count || imageError || s.image_error || $('screenshots').checked);
    $('done').hidden = !s.result;
    $('download').download = s.result.split(/[\\/]/).pop() || '강의전사.txt';
    $('soundHint').textContent = state === 'recording' && s.seconds > 8 && s.level === 0 ? '현재 무음입니다. 강의 재생과 오디오 공유 여부를 확인하세요.' : '녹음 중 소리 표시가 움직이는지 확인하세요.';
    const signature = JSON.stringify(s.pending);
    if (signature !== lastPending) {
      lastPending = signature; $('pending').replaceChildren();
      for (const item of s.pending) {
        const row=document.createElement('div'), label=document.createElement('span'), button=document.createElement('button');
        row.className='pending-row'; label.textContent=`${item.title} · ${item.created}`;
        button.textContent='다시 변환'; button.className='secondary';
        button.onclick=async()=>{localError='';try{await api('retry',{...settings(),id:item.id});await poll();}catch(e){error(e);}};
        row.append(label,button);$('pending').append(row);
      }
    }
    $('recovery').hidden = busy || !s.pending.length;
  } catch(e) { if (!stopping) error(new Error('프로그램 연결이 끊겼습니다. EXE를 다시 실행하면 보존된 녹음을 확인할 수 있습니다.')); }
}
$('start').onclick=start;
$('stop').onclick=()=> recording ? stop() : api('stop',{error:'녹음 화면이 다시 열렸습니다. 받은 구간을 보존합니다.'}).catch(error);
$('source').onchange=labels; $('engine').onchange=labels;
$('screenshots').onchange=labels;
$('prepare').onclick=async()=>{localError='';try{await api('model',settings());poll();}catch(e){error(e);}};
$('folder').onclick=()=>api('folder',{}).catch(error);
$('open').onclick=()=>api('open',{}).catch(error);
$('exit').onclick=async()=>{try{await api('exit',{});$('message').textContent='프로그램이 종료되었습니다. 이 창을 닫으세요.';clearInterval(timer);window.close();}catch(e){error(e);}};
window.addEventListener('beforeunload',e=>{if(recording||state==='transcribing'){e.preventDefault();e.returnValue='';}});
labels(); poll(); const timer=setInterval(poll,1000);
