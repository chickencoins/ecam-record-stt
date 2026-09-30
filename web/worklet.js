class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super(); this.buffer = new Int16Array(sampleRate); this.used = 0; this.active = true;
    this.port.onmessage = e => {
      if (e.data === 'stop') {
        this.active = false; this.flush(); this.port.postMessage({stopped: true});
      }
    };
  }
  flush() {
    if (this.used) {
      const data = this.buffer.slice(0, this.used).buffer;
      this.port.postMessage({audio: data}, [data]); this.used = 0;
    }
  }
  process(inputs) {
    if (!this.active) return true;
    const channels = inputs[0];
    // Keep elapsed time even when a paused tab provides no input.
    const count = channels && channels.length ? channels[0].length : 128;
    for (let i = 0; i < count; i++) {
      let sample = 0;
      if (channels && channels.length) {
        for (const channel of channels) sample += channel[i] || 0;
        sample /= channels.length;
      }
      sample = Math.max(-1, Math.min(1, sample));
      this.buffer[this.used++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.used === this.buffer.length) this.flush();
    }
    return true;
  }
}
registerProcessor('pcm-recorder', PCMRecorder);
