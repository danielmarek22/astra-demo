// Frame capture and track cleanup migrated from Astra static/main.js.
export function captureVideoFrame(video, canvas, maxLongEdge) {
    if (!video || !canvas || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        return null;
    }

    const sourceWidth = video.videoWidth;
    const sourceHeight = video.videoHeight;
    if (!sourceWidth || !sourceHeight) {
        return null;
    }

    const scale = Math.min(1, maxLongEdge / Math.max(sourceWidth, sourceHeight));
    canvas.width = Math.max(1, Math.round(sourceWidth * scale));
    canvas.height = Math.max(1, Math.round(sourceHeight * scale));

    const context = canvas.getContext('2d');
    if (!context) return null;

    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.5);
    const commaIndex = dataUrl.indexOf(',');
    return commaIndex >= 0 ? dataUrl.slice(commaIndex + 1) : null;
}

export function stopStream(stream) {
    if (!stream) return;
    stream.getTracks().forEach((track) => track.stop());
}


export class ScreenCapture {
    constructor(onChange = () => {}) {
        this.onChange = onChange;
        this.stream = null;
        this.video = document.createElement('video');
        this.video.muted = true;
        this.video.playsInline = true;
        this.canvas = document.createElement('canvas');
        this.version = 0;
    }
    async start() {
        if (!navigator.mediaDevices?.getDisplayMedia) throw new Error('Screen sharing is unavailable here. You can attach a screenshot instead.');
        const version = ++this.version;
        const stream = await navigator.mediaDevices.getDisplayMedia({video:true});
        if (version !== this.version) { stopStream(stream); return; }
        this.stream = stream;
        this.video.srcObject = stream;
        stream.getVideoTracks().forEach((track) => track.addEventListener('ended', () => this.stop(), {once:true}));
        try { await this.video.play(); }
        catch (error) { if (version === this.version) this.stop(); throw error; }
        if (version !== this.version) return;
        this.onChange(true);
    }
    stop() {
        this.version++;
        stopStream(this.stream);
        this.stream = null;
        this.video.srcObject = null;
        this.onChange(false);
    }
    capture() {
        if (!this.stream?.active) return [];
        const data = captureVideoFrame(this.video, this.canvas, 960);
        if (!data) throw new Error('The shared screen is not ready. Try sending again.');
        return [{name:`screen-${new Date().toISOString().replaceAll(':','-')}.jpg`,mimeType:'image/jpeg',data}];
    }
}
