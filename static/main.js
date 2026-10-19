import { MessageRenderer } from './js/message-renderer.mjs';
import { readEvents } from './js/demo-client.mjs';
import { AudioManager } from './js/audio-manager.js';
import { MicrophoneRecorder } from './js/microphone-recorder.mjs';
import { ScreenCapture } from './js/screen-capture.mjs';
import { isImageFile, extractBase64Payload, extractImageFilesFromDataTransfer } from './js/attachment-utils.mjs';

const $ = (id) => document.getElementById(id);
const renderer = new MessageRenderer();
let generation = null;
let sessionId = null;
let examples = [];
let pending = false;
let remoteBusy = false;
let view = 'chat';
let knowledgeKind = 'memories';
let refreshInProgress = false;
const STORAGE_KEY = 'astra-demo.workspace.v1';
// Per-page identity binds approval replies to the stream that requested them.
const clientId = crypto.randomUUID();
let capabilities = {};
let draftAttachments = [];
let recording = false;
let transcribing = false;
let mediaEpoch = 0;
let speechEpoch = 0;
let speechRequest = null;
let approvalId = null;
const audioUrls = new Set();
const audio = new AudioManager();
const capture = new ScreenCapture((active) => {
    $('screen-preview').hidden = !active;
    $('share-screen').textContent = active ? 'Stop sharing' : 'Share screen';
    $('share-screen').setAttribute('aria-pressed', String(active));
    $('include-screen').checked = active;
});
$('screen-video').replaceWith(capture.video);
capture.video.id = 'screen-video';
const microphone = new MicrophoneRecorder({
    onStateChange: (kind, active) => {
        if (kind !== 'recording') return;
        recording = active;
        $('record').textContent = active ? 'Stop recording' : 'Dictate';
        $('record').setAttribute('aria-pressed', String(active));
        controls();
    },
    onAudio: (blob) => transcribe(blob),
});
function stopSpeech() {
    speechEpoch++;
    speechRequest?.abort(); speechRequest = null;
    audio.stop();
    for (const url of audioUrls) URL.revokeObjectURL(url);
    audioUrls.clear();
    $('voice-status').textContent = '';
}
function cancelMedia() {
    mediaEpoch++;
    transcribing = false;
    microphone.cancelManualRecording();
    capture.stop();
    draftAttachments = []; renderDraftAttachments();
    stopSpeech();
}
async function checkedResponse(response) {
    if (response.ok) return response;
    const error = await response.json();
    throw new Error(typeof error.detail === 'string' ? error.detail : 'The request could not be completed.');
}
async function transcribe(blob) {
    if (!blob.size) return;
    const epoch = mediaEpoch;
    const currentGeneration = generation;
    transcribing = true; controls();
    $('voice-status').textContent = 'Transcribing… The first recording may take longer while Whisper loads.';
    try {
        const response = await checkedResponse(await fetch('/api/transcribe', {
            method: 'POST', headers: {'Content-Type': blob.type || 'audio/webm', 'X-Demo-Generation': currentGeneration}, body: blob,
        }));
        const result = await response.json();
        if (epoch !== mediaEpoch || currentGeneration !== generation) return;
        if (!result.text) throw new Error('No speech was detected. Try again or type your message.');
        const input = $('message-input');
        input.value = [input.value.trim(), result.text].filter(Boolean).join(' ');
        input.focus();
        $('voice-status').textContent = 'Transcript ready. Review it, then send.';
    } catch (error) { if (epoch === mediaEpoch) { $('voice-status').textContent = ''; showError(error); } }
    finally { if (epoch === mediaEpoch) { transcribing = false; controls(); } }
}
async function readAloud(messageId) {
    stopSpeech();
    audio.init();
    const epoch = speechEpoch;
    const currentGeneration = generation;
    const currentSession = sessionId;
    speechRequest = new AbortController();
    $('voice-status').textContent = 'Preparing the English voice… The first reply may take longer while Kokoro loads.';
    try {
        const response = await checkedResponse(await fetch(`/api/sessions/${encodeURIComponent(currentSession)}/speech`, {
            method: 'POST', headers: {'Content-Type':'application/json','X-Demo-Generation':currentGeneration},
            body: JSON.stringify({message_id:messageId}), signal:speechRequest.signal,
        }));
        const blob = await response.blob();
        if (epoch !== speechEpoch || generation !== currentGeneration || sessionId !== currentSession) return;
        const url = URL.createObjectURL(blob); audioUrls.add(url);
        audio.queueAudio(url);
        $('voice-status').textContent = 'Voice ready. If playback is paused, select Resume voice.';
    } catch (error) { if (epoch === speechEpoch && error.name !== 'AbortError') { $('voice-status').textContent = ''; showError(error); } }
    finally { if (epoch === speechEpoch) speechRequest = null; }
}
function renderDraftAttachments() {
    $('draft-attachments').replaceChildren();
    for (const [index, attachment] of draftAttachments.entries()) {
        const node = renderer.createAttachmentNode(attachment);
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Remove';
        remove.setAttribute('aria-label', `Remove ${attachment.name}`);
        remove.addEventListener('click', () => { draftAttachments.splice(index,1); renderDraftAttachments(); });
        node.append(remove); $('draft-attachments').append(node);
    }
}
async function attachFiles(files) {
    const epoch = mediaEpoch;
    try {
        const incoming = [];
        for (const file of files) {
            if (!isImageFile(file)) continue;
            if (file.size > 10 * 1024 * 1024) throw new Error('Images must be smaller than 10 MB.');
            const data = await new Promise((resolve,reject) => {
                const reader = new FileReader(); reader.onload = () => resolve(extractBase64Payload(reader.result)); reader.onerror = reject; reader.readAsDataURL(file);
            });
            incoming.push({name:file.name || 'screenshot.png', mimeType:file.type || 'image/png', data});
        }
        if (epoch !== mediaEpoch || pending || remoteBusy) return;
        if (draftAttachments.length + incoming.length > 8) throw new Error('Attach at most eight images per message.');
        draftAttachments.push(...incoming); renderDraftAttachments();
    } catch (error) { if (epoch === mediaEpoch) showError(error); }
}
function showApproval(event) {
    approvalId = event.request_id;
    $('approval-title').textContent = event.title || 'Allow this command?';
    $('approval-reason').textContent = event.reason || '';
    $('approval-command').textContent = event.detail || '';
    $('approval').hidden = false;
    $('approve-command').disabled = false; $('deny-command').disabled = false;
    $('deny-command').focus();
}
async function resolveApproval(approved) {
    const id = approvalId;
    if (!id) return;
    $('approve-command').disabled = true; $('deny-command').disabled = true;
    try { await api(`/api/approvals/${id}`, {method:'POST',body:JSON.stringify({approved})}); }
    catch (error) { showError(error); }
}


function showError(error) {
    $('error').textContent = error.message || String(error);
    $('error').hidden = false;
}
function clearError() { $('error').hidden = true; }
function controls() {
    for (const id of ['send', 'reset', 'new-chat', 'message-input']) $(id).disabled = pending || remoteBusy || !generation || (['send','message-input'].includes(id) && (recording || transcribing));
    $('record').disabled = pending || remoteBusy || transcribing || !generation || !capabilities.speech_input;
    for (const id of ['share-screen','attach-image']) $(id).disabled = pending || remoteBusy || !generation;
    $('speak-replies').disabled = !capabilities.speech_output;
    document.querySelectorAll('.read-aloud').forEach((button) => { button.disabled = pending || remoteBusy || !capabilities.speech_output; });
    document.querySelectorAll('.session-button,.example,.retry-button').forEach((button) => { button.disabled = pending || remoteBusy; });
}
async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers: {
        'Content-Type': 'application/json', 'X-Demo-Generation': generation, 'X-Demo-Client':clientId, ...options.headers,
    }});
    if (!response.ok) {
        const error = await response.json();
        throw new Error(typeof error.detail === 'string' ? error.detail : 'The request could not be completed.');
    }
    return response.json();
}
function saveSelection() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({generation, sessionId})); } catch { /* Storage can be unavailable in private sessions. */ }
}
function setView(next) {
    view = next;
    $('chat-view').hidden = next !== 'chat';
    $('knowledge-view').hidden = next !== 'knowledge';
    for (const name of ['chat', 'knowledge']) {
        $(`${name}-tab`).classList.toggle('active', next === name);
        $(`${name}-tab`).setAttribute('aria-pressed', String(next === name));
    }
    if (next === 'knowledge') loadKnowledge().catch(showError);
}
function addMessage(role, text, row = {}) {
    const node = document.createElement('article');
    node.className = `message ${role === 'assistant' ? 'astra' : role === 'user' ? 'user' : 'activity'}`;
    const label = document.createElement('span');
    label.className = 'role-label';
    label.textContent = role === 'assistant' ? 'ASTRA' : role === 'user' ? (examples.some((item) => item.id === sessionId) ? 'SAMPLE PARTICIPANT' : 'YOU') : 'TOOL ACTIVITY';
    const body = document.createElement('div');
    if (!['assistant', 'user'].includes(role)) {
        const details = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = 'Inspect tool activity';
        const content = document.createElement('pre'); content.textContent = text;
        details.append(summary, content); body.append(details);
    } else renderer.setMessageContent(body, text, row.attachments || []);
    // Renderer detects the assistant class on the node it formats.
    if (role === 'assistant') { body.classList.add('astra'); renderer.setMessageContent(body, text); }
    node.append(label, body);
    if (role === 'assistant' && row.id && text) {
        const read = document.createElement('button'); read.type = 'button'; read.className = 'read-aloud'; read.textContent = 'Read aloud';
        read.addEventListener('click', () => readAloud(row.id)); node.append(read);
    }
    if (row.retry_error) {
        const failure = document.createElement('p'); failure.textContent = row.retry_error;
        const retry = document.createElement('button'); retry.textContent = 'Try again'; retry.className = 'retry-button';
        retry.addEventListener('click', () => sendMessage('', row.id)); node.append(failure, retry);
    }
    $('messages').append(node);
    return body;
}
async function loadSessions() {
    const data = await api('/api/sessions');
    $('session-count').textContent = String(data.sessions.length);
    $('sessions').replaceChildren();
    for (const row of data.sessions) {
        const example = examples.find((item) => item.id === row.session_id);
        const button = document.createElement('button'); button.className = 'session-button';
        button.classList.toggle('active', sessionId === row.session_id);
        const title = document.createElement('strong'); title.textContent = example?.title || row.preview || 'New conversation';
        const meta = document.createElement('small'); meta.textContent = `${example ? 'SAMPLE · ' : ''}${row.message_count} messages`;
        button.append(title, meta); button.addEventListener('click', () => openSession(row.session_id).catch(showError));
        $('sessions').append(button);
    }
    controls();
}
async function openSession(id, preserveError = false) {
    if (pending || remoteBusy) return;
    if (!preserveError) clearError();
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (id !== sessionId) cancelMedia();
    sessionId = id; saveSelection();
    $('welcome').hidden = true;
    $('messages').replaceChildren();
    data.messages.forEach((row) => addMessage(row.role, row.content, row));
    const example = examples.find((item) => item.id === id);
    $('conversation-title').textContent = example?.title || 'Your conversation';
    $('suggestion').hidden = !example;
    $('suggestion-text').textContent = example?.suggestion || '';
    await loadSessions();
    if (view === 'knowledge') await loadKnowledge();
    $('messages').scrollTop = $('messages').scrollHeight;
    return data.messages;
}
async function createConversation() {
    const data = await api('/api/sessions', {method: 'POST'});
    await openSession(data.session_id);
    setView('chat'); $('message-input').focus();
}
async function sendMessage(text, retryId = null) {
    if (pending || remoteBusy || recording || transcribing) return;
    if (!retryId && !text.trim() && !draftAttachments.length && !(capture.stream && $('include-screen').checked)) return;
    clearError();
    let finalReply = false;
    try {
        const attachments = retryId ? [] : [...draftAttachments, ...($('include-screen').checked ? capture.capture() : [])];
        if (attachments.length > 8) throw new Error('Attach at most eight images, including the screen frame.');
        if (!sessionId) await createConversation();
        stopSpeech();
        pending = true; controls();
        $('suggestion').hidden = true;
        if (!retryId) { addMessage('user', text, {attachments}); $('message-input').value = ''; draftAttachments = []; renderDraftAttachments(); }
        const body = addMessage('assistant', '');
        let output = '';
        const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
            method: 'POST', headers: {'Content-Type': 'application/json', 'X-Demo-Generation': generation, 'X-Demo-Client':clientId},
            body: JSON.stringify(retryId ? {retry_message_id: retryId} : {text, attachments}),
        });
        await readEvents(response, (event) => {
            if (event.type === 'state') $('status').textContent = event.state === 'idle' ? 'Finishing this turn…' : `${event.state.charAt(0).toUpperCase()}${event.state.slice(1)}…`;
            if (event.type === 'speech') {
                if (event.is_final && event.text) finalReply = true;
                output = event.is_final ? event.text : output + event.text;
                renderer.setMessageContent(body, output);
                $('messages').scrollTop = $('messages').scrollHeight;
            }
            if (event.type === 'tool_approval_request') showApproval(event);
            if (event.type === 'tool_approval_resolved' && approvalId === event.request_id) { approvalId = null; $('approval').hidden = true; }
            if (event.type === 'image_unavailable') showError(new Error(event.message));
            if (event.type === 'turn_failure' || event.type === 'error') showError(new Error(event.message));
        });
    } catch (error) { showError(error); }
    finally {
        pending = false; approvalId = null; $('approval').hidden = true;
        try {
            const state = await fetch('/api/status').then((r) => r.json());
            remoteBusy = state.busy;
            if (state.generation !== generation) await synchronize(state);
            else if (!remoteBusy && sessionId) {
                const rows = await openSession(sessionId, true);
                const last = rows?.at(-1);
                if (finalReply && last?.role === 'assistant' && $('speak-replies').checked) void readAloud(last.id);
            }
        } catch (error) { showError(error); }
        $('status').textContent = remoteBusy ? 'Finishing the current operation…' : 'Ready when you are';
        controls();
        $('message-input').focus();
    }
}
function card(title, content, meta) {
    const node = document.createElement('article'); node.className = 'knowledge-card';
    const heading = document.createElement('h3'); heading.textContent = title;
    const body = document.createElement('div'); body.textContent = content;
    const footer = document.createElement('small'); footer.textContent = meta;
    node.append(heading, body, footer); return node;
}
async function loadKnowledge() {
    if (pending || remoteBusy) throw new Error('Knowledge is available when the current operation finishes.');
    $('knowledge-records').replaceChildren();
    if (knowledgeKind === 'context') {
        if (!sessionId) { $('knowledge-records').textContent = 'Open a conversation to inspect its current belief context.'; return; }
        const data = await api(`/api/knowledge/belief-context?session_id=${encodeURIComponent(sessionId)}`);
        const pre = document.createElement('pre'); pre.textContent = data.text || 'No current beliefs apply.';
        const node = card('Context supplied to the assistant', '', 'Computed from the current belief records.'); node.append(pre);
        $('knowledge-records').append(node); return;
    }
    let offset = 0;
    do {
        const data = await api(knowledgeKind === 'memories' ? '/api/knowledge/memories' : `/api/knowledge/beliefs?limit=100&offset=${offset}`);
        for (const record of data.records) {
            const node = knowledgeKind === 'memories'
                ? card(record.category || 'Saved memory', record.content, `Importance ${record.importance ?? '—'}`)
                : card(record.predicate.replaceAll('_', ' '), typeof record.value === 'string' ? record.value : JSON.stringify(record.value),
                    `${record.subject.display_name_at_evidence_time} · ${record.record_status} · ${record.source.display_name_at_evidence_time} · revision ${record.revision}`);
            $('knowledge-records').append(node);
        }
        offset += data.records.length;
        if (knowledgeKind === 'memories' || offset >= data.total || !data.records.length) break;
    } while (offset < 1000);
    if (!$('knowledge-records').children.length) $('knowledge-records').textContent = 'No saved records yet.';
}
async function synchronize(state) {
    if (!state.available) { generation = null; remoteBusy = false; controls(); throw new Error('The workspace is unavailable. Restart the demo server.'); }
    if (generation !== state.generation) cancelMedia();
    capabilities = state.capabilities || {};
    generation = state.generation; remoteBusy = state.busy; examples = state.examples;
    $('model').textContent = state.model;
    $('examples').replaceChildren();
    examples.forEach((example, index) => {
        const button = document.createElement('button'); button.className = 'example';
        const icon = document.createElement('span'); icon.textContent = ['↗', '◇', '↻'][index];
        const title = document.createElement('strong'); title.textContent = example.title;
        const action = document.createElement('small'); action.textContent = 'EXPLORE ↗';
        button.append(icon, title, action);
        button.addEventListener('click', () => openSession(example.id).catch(showError)); $('examples').append(button);
    });
    sessionId = null;
    try { const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY)); if (saved?.generation === generation) sessionId = saved.sessionId; } catch { /* Fresh tab. */ }
    if (!remoteBusy) {
        await loadSessions();
        if (sessionId) await openSession(sessionId);
        else { $('messages').replaceChildren(); $('welcome').hidden = false; $('suggestion').hidden = true; $('conversation-title').textContent = 'A little context goes a long way.'; }
        if (view === 'knowledge') await loadKnowledge();
    }
    $('status').textContent = remoteBusy ? 'Another tab is working…' : 'Ready when you are';
    controls();
}
document.querySelector('.brand').addEventListener('click', (event) => {
    event.preventDefault();
    if (pending || remoteBusy) return;
    cancelMedia(); sessionId = null; saveSelection(); clearError();
    $('messages').replaceChildren(); $('welcome').hidden = false; $('suggestion').hidden = true;
    $('conversation-title').textContent = 'A little context goes a long way.';
    setView('chat'); loadSessions().catch(showError);
});
$('composer').addEventListener('submit', (event) => { event.preventDefault(); sendMessage($('message-input').value); });
$('message-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
$('new-chat').addEventListener('click', () => createConversation().catch(showError));
$('suggestion-text').addEventListener('click', () => { $('message-input').value = $('suggestion-text').textContent; $('message-input').focus(); });
$('chat-tab').addEventListener('click', () => setView('chat'));
$('knowledge-tab').addEventListener('click', () => setView('knowledge'));
for (const button of document.querySelectorAll('[data-kind]')) button.addEventListener('click', () => {
    knowledgeKind = button.dataset.kind;
    document.querySelectorAll('[data-kind]').forEach((node) => node.classList.toggle('active', node === button));
    loadKnowledge().catch(showError);
});
$('reset').addEventListener('click', async () => {
    if (!window.confirm('Restore the fictional examples? Changes made during this demo run will be removed.')) return;
    cancelMedia(); clearError(); pending = true; controls(); $('status').textContent = 'Restoring the sample workspace…';
    try { await api('/api/reset', {method: 'POST'}); pending = false; await synchronize(await fetch('/api/status').then((r) => r.json())); }
    catch (error) { showError(error); }
    finally { pending = false; controls(); }
});
async function poll() {
    if (refreshInProgress) return;
    refreshInProgress = true;
    try {
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error('The demo is unavailable. Check that the server is running.');
        const state = await response.json();
        if (!pending && (state.generation !== generation || (remoteBusy && !state.busy))) await synchronize(state);
        remoteBusy = state.busy;
        if (!pending) $('status').textContent = remoteBusy ? 'Another tab is working…' : 'Ready when you are';
        controls();
    } catch (error) { $('status').textContent = 'Connection unavailable'; showError(error); }
    finally { refreshInProgress = false; }
}
await poll();
setInterval(poll, 3000);

$('record').addEventListener('click', async () => {
    clearError();
    if (recording) { microphone.stopRecording(); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { showError(new Error('Voice recording is unavailable in this browser. You can still type.')); return; }
    const epoch = mediaEpoch;
    stopSpeech(); $('record').disabled = true;
    try {
        await microphone.startManualRecording();
        if (epoch !== mediaEpoch) return;
        if (!recording) showError(new Error('The microphone could not start. Check browser permissions.'));
    } catch (error) { if (epoch === mediaEpoch) { microphone.cancelManualRecording(); showError(error); } }
    finally { controls(); }
});
$('share-screen').addEventListener('click', async () => {
    clearError();
    if (capture.stream) { capture.stop(); return; }
    const epoch = mediaEpoch;
    $('share-screen').disabled = true;
    try { await capture.start(); }
    catch (error) { if (epoch === mediaEpoch) showError(new Error(error.name === 'NotAllowedError' ? 'Screen sharing was cancelled. You can attach a screenshot instead.' : error.message)); }
    finally { controls(); }
});
$('attach-image').addEventListener('click', () => $('image-input').click());
$('image-input').addEventListener('change', async (event) => { await attachFiles(event.target.files); event.target.value = ''; });
$('message-input').addEventListener('paste', (event) => {
    const images = extractImageFilesFromDataTransfer(event.clipboardData);
    if (images.length) { event.preventDefault(); void attachFiles(images); }
});
$('stop-voice').addEventListener('click', stopSpeech);
$('resume-voice').addEventListener('click', () => audio.init());
$('speak-replies').addEventListener('change', () => { if ($('speak-replies').checked) audio.init(); else stopSpeech(); });
$('approve-command').addEventListener('click', () => resolveApproval(true));
$('deny-command').addEventListener('click', () => resolveApproval(false));
window.addEventListener('pagehide', cancelMedia);
