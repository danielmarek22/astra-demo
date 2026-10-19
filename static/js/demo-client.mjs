// Each request resolves against the page origin. Generations fence stale tabs.
export async function readEvents(response, onEvent) {
    if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'The request failed.');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    let complete = false;
    const accept = (line) => {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === 'done') complete = true;
        onEvent(event);
    };
    try {
        while (true) {
            const {value, done} = await reader.read();
            pending += decoder.decode(value, {stream: !done});
            const lines = pending.split('\n');
            pending = lines.pop();
            lines.forEach(accept);
            if (done) break;
        }
        if (pending.trim()) accept(pending);
        if (!complete) throw new Error('Connection interrupted. Reload the conversation to check its saved state.');
    } finally {
        reader.releaseLock();
    }
}
