import test from 'node:test';
import assert from 'node:assert/strict';
import {readEvents} from '../static/js/demo-client.mjs';

function responseFromChunks(chunks) {
    return new Response(new ReadableStream({start(controller) {
        chunks.forEach((value) => controller.enqueue(value)); controller.close();
    }}));
}

test('stream preserves UTF-8 and event boundaries across arbitrary network chunks', async () => {
    const bytes = new TextEncoder().encode('{"type":"speech","text":"Cześć 🌙"}\n{"type":"done"}\n');
    const events = [];
    await readEvents(responseFromChunks(Array.from(bytes, (byte) => Uint8Array.of(byte))), (event) => events.push(event));
    assert.equal(events[0].text, 'Cześć 🌙');
    assert.equal(events[1].type, 'done');
});

test('truncated streams surface a recoverable connection failure', async () => {
    const bytes = new TextEncoder().encode('{"type":"speech","text":"partial"}\n');
    await assert.rejects(readEvents(responseFromChunks([bytes]), () => {}), /Connection interrupted/);
});

test('a server busy result is visible without replaying the request', async () => {
    let events = 0;
    await assert.rejects(readEvents(new Response('{"detail":"Demo is busy"}', {status:409}), () => events++), /Demo is busy/);
    assert.equal(events, 0);
});
