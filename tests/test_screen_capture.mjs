import test from 'node:test';
import assert from 'node:assert/strict';
import { ScreenCapture, captureVideoFrame } from '../static/js/screen-capture.mjs';

test('capture reuses the base resize and JPEG encoding and releases late permission grants', async t => {
    const originals = new Map(['document','navigator','HTMLMediaElement'].map(key => [key,Object.getOwnPropertyDescriptor(globalThis,key)]));
    t.after(() => { for (const [key,value] of originals) { if (value) Object.defineProperty(globalThis,key,value); else delete globalThis[key]; } });
    let grant;
    let stops = 0;
    const track = {stop:() => stops++,addEventListener() {}};
    const stream = {active:true,getTracks:() => [track], getVideoTracks:() => [track]};
    const video = {readyState:2,videoWidth:1920,videoHeight:1080,play:async () => {}};
    const canvas = {getContext:() => ({drawImage() {}}),toDataURL:(mime,quality) => { assert.equal(mime,'image/jpeg'); assert.equal(quality,0.5); return 'data:image/jpeg;base64,frame'; }};
    Object.defineProperty(globalThis,'document',{configurable:true,value:{createElement:tag => tag==='video' ? video : canvas}});
    Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getDisplayMedia:() => new Promise(resolve => { grant=resolve; })}}});
    Object.defineProperty(globalThis,'HTMLMediaElement',{configurable:true,value:{HAVE_CURRENT_DATA:2}});
    assert.equal(captureVideoFrame(video,canvas,960),'frame');
    assert.equal(canvas.width,960); assert.equal(canvas.height,540);
    const states = [];
    const capture = new ScreenCapture(active => states.push(active));
    const pending = capture.start(); capture.stop(); grant(stream); await pending;
    assert.equal(stops,1); assert.deepEqual(states,[false]);
    assert.deepEqual(capture.capture(),[]);
    const next = capture.start(); grant(stream); await next;
    assert.equal(capture.capture()[0].data,'frame');
    capture.stop(); assert.equal(stops,2);
});
