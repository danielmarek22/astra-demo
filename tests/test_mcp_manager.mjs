import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { MCPManager, editableServer, toolChoices, toolState } from '../static/js/mcp-manager.mjs';

const fixture = () => ({
    revision: 'configuration-1', catalog_revision: 1, enabled: true,
    servers: [{ id: 'demo', config: { enabled: true, transport: 'stdio', command: 'python', args: ['server.py'],
        approval_required: true, enabled_tools: ['echo'], tool_fingerprints: { echo: 'a'.repeat(64) }, tool_approvals: { echo: false } },
    status: { state: 'connected' }, tools: [
        { name: 'echo', description: '<img src=x onerror=alert(1)>', valid: true, enabled: true, selected: true,
            approval_required: false, fingerprint: 'a'.repeat(64), input_schema: { type: 'object' } },
        { name: 'changed', description: 'Review this tool', valid: true, enabled: false, approval_required: true,
            pending_review: true, fingerprint: 'b'.repeat(64), input_schema: { type: 'object' } },
        { name: 'invalid', description: 'Invalid schema', valid: false, enabled: false, approval_required: true },
    ] }],
});

class Node {
    constructor(tag = 'div') {
        this.tagName = tag; this.children = []; this.handlers = {}; this.dataset = {}; this.attributes = {};
        this.disabled = false; this.checked = false; this.value = ''; this.textContent = '';
        const values = new Set();
        this.classList = { add: v => values.add(v), remove: v => values.delete(v), contains: v => values.has(v),
            toggle(v, state) { if (state ?? !values.has(v)) values.add(v); else values.delete(v); } };
    }
    set innerHTML(value) { throw new Error('Untrusted markup must never be assigned'); }
    set className(value) { for (const name of value.split(' ')) this.classList.add(name); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(type, handler) { this.handlers[type] = handler; }
    setAttribute(key, value) { this.attributes[key] = value; }
    focus() {}
    querySelectorAll(selector) {
        const all = this.children.flatMap(child => child instanceof Node ? [child, ...child.querySelectorAll('*')] : []);
        return selector === '*' ? all : all.filter(node => selector.includes(node.tagName));
    }
}

function ui(t) {
    const previous = globalThis.document;
    const ids = new Map(['mcp-view', 'mcp-status', 'mcp-servers', 'mcp-server-form', 'mcp-enabled', 'mcp-reload', 'mcp-add', 'mcp-cancel', 'mcp-form-title'].map(id => [id, new Node()]));
    ids.get('mcp-enabled').tagName = 'input';
    const form = ids.get('mcp-server-form');
    form.elements = Object.fromEntries(['id', 'description', 'enabled', 'approval_required', 'transport', 'command', 'args', 'cwd', 'env', 'url', 'headers', 'limits'].map(name => [name, new Node('input')]));
    form.querySelectorAll = () => [];
    form.reset = () => { for (const node of Object.values(form.elements)) { node.value = ''; node.checked = false; } };
    ids.get('mcp-view').append(...[...ids.values()].filter(node => node !== ids.get('mcp-view')));
    globalThis.document = {
        getElementById: id => ids.get(id), querySelector: () => new Node('button'),
        createElement: tag => new Node(tag), createTextNode: text => ({ textContent: text }),
    };
    t.after(() => { globalThis.document = previous; });
    const calls = [];
    const client = {
        getMCP: async () => fixture(),
        changeMCP: async (...args) => { calls.push(args); return fixture(); },
    };
    const manager = new MCPManager(client);
    return { manager, client, ids, form, calls };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test('MCP has its own workspace view beside Knowledge', async () => {
    const html = await readFile(new URL('../static/index.html', import.meta.url), 'utf8');
    assert.match(html, /id="mcp-tab"[^>]*>MCP/);
    assert.match(html, /<section id="mcp-view"[^>]*hidden>/);
    assert.ok(html.indexOf('id="mcp-server-form"') > html.indexOf('id="mcp-view"'));
});

test('tool states distinguish selection, unavailability and renewed review', () => {
    const view = fixture(), server = view.servers[0];
    assert.equal(toolState(server.tools[0], server, true), 'Enabled');
    assert.equal(toolState(server.tools[1], server, true), 'Pending review');
    assert.equal(toolState(server.tools[2], server, true), 'Unavailable');
    assert.equal(toolState({ valid: true, enabled: false }, server, true), 'Disabled');
    assert.equal(toolState(server.tools[0], server, false), 'Unavailable');
    assert.equal(toolState(server.tools[0], { ...server, status: { state: 'disconnected' } }, true), 'Unavailable');
});

test('server edits exclude tool grants; tool choices preserve separate approval and exact pins', () => {
    const view = fixture(), server = view.servers[0];
    const config = editableServer(server.config);
    assert.ok(!('enabled_tools' in config));
    assert.ok(!('tool_fingerprints' in config));
    assert.ok(!('tool_approvals' in config));
    const controls = new Map(server.tools.map(tool => [tool.name, { enable: { checked: true }, approval: { checked: tool.name !== 'echo' } }]));
    assert.deepEqual(toolChoices(server, controls, view), {
        revision: view.revision, catalog_revision: 1, enabled_tools: ['echo', 'changed'],
        fingerprints: { echo: 'a'.repeat(64), changed: 'b'.repeat(64) }, tool_approvals: { echo: false, changed: true },
    });
});

test('a workspace change clears the catalog and discards the open editor', async t => {
    const { manager, ids, form, calls } = ui(t);
    await manager.reload();
    manager.edit(fixture().servers[0]);
    manager.clear();
    assert.equal(ids.get('mcp-enabled').disabled, true);
    assert.equal(ids.get('mcp-add').disabled, true);
    assert.equal(ids.get('mcp-servers').children.length, 0);
    assert.equal(form.classList.contains('hidden'), true);
    await manager.change('', 'PUT', { enabled: true });
    assert.equal(calls.length, 0);
});

test('rendering uses text nodes, disabled invalid checkboxes and explicit tool saves', async t => {
    const { manager, ids, calls } = ui(t);
    await manager.reload();
    const card = ids.get('mcp-servers').children[0];
    const form = card.children.find(node => node.tagName === 'form');
    const rows = form.children.filter(node => node.classList?.contains('mcp-tool'));
    assert.equal(rows[0].children[2].textContent, '<img src=x onerror=alert(1)>');
    assert.equal(rows[2].children[0].children[0].disabled, true);
    rows[0].children[0].children[0].checked = false;
    rows[1].children[0].children[0].checked = true;
    rows[1].children[3].children[0].checked = false;
    form.handlers.submit({ preventDefault() {} });
    await tick();
    assert.equal(calls[0][0], '/servers/demo/tools');
    assert.deepEqual(calls[0][2].enabled_tools, ['changed']);
    assert.equal(calls[0][2].tool_approvals.changed, false);
});

test('rejected schemas display their diagnostic path and reason as text', async t => {
    const { manager, client, ids } = ui(t);
    const data = fixture();
    const reason = 'invalid_schema: input_schema#/$defs/__schema0/properties/date/properties/$gte/pattern: Unsupported regular expression; only the fixed YYYY-MM-DD pattern is supported';
    data.servers[0].tools[2].issue = reason;
    data.servers[0].tools[2].description = 'Tool definition rejected. See the validation reason below.';
    client.getMCP = async () => data;
    await manager.reload();
    const form = ids.get('mcp-servers').children[0].children.find(node => node.tagName === 'form');
    const row = form.children.filter(node => node.classList?.contains('mcp-tool'))[2];
    assert.equal(row.children[0].children[0].disabled, true);
    assert.equal(row.children.find(node => node.classList?.contains('mcp-help')).textContent, reason);
    assert.match(row.children[2].textContent, /validation reason/);
});

test('create and edit forms support both transports and retain their opening revision', async t => {
    const { manager, form, calls } = ui(t);
    await manager.reload();
    manager.edit();
    const fields = form.elements;
    fields.id.value = 'new_server'; fields.command.value = '/python'; fields.args.value = '["server.py"]'; fields.env.value = '{"TOKEN":"ASTRA_TOKEN"}';
    await manager.saveServer();
    assert.equal(calls[0][0], '/servers');
    assert.deepEqual(calls[0][2].config.args, ['server.py']);
    assert.equal(calls[0][2].config.env.TOKEN, 'ASTRA_TOKEN');
    manager.edit(fixture().servers[0]);
    manager.snapshot.revision = 'newer-revision';
    fields.transport.value = 'streamable_http'; fields.url.value = 'http://localhost:3001/mcp'; fields.headers.value = '{"Authorization":"ASTRA_AUTH"}';
    await manager.saveServer();
    assert.equal(calls[1][1], 'PUT');
    assert.equal(calls[1][2].revision, 'configuration-1');
    assert.equal(calls[1][2].config.headers.Authorization, 'ASTRA_AUTH');
    assert.ok(!('command' in calls[1][2].config));
    assert.ok(!('enabled_tools' in calls[1][2].config));
});

test('busy state blocks duplicate operations and conflicts retain unsaved editor fields', async t => {
    const { manager, client, form, ids } = ui(t);
    await manager.reload();
    manager.edit(fixture().servers[0]);
    form.elements.command.value = 'unsaved';
    let release;
    client.changeMCP = () => new Promise((_, reject) => { release = () => reject(Object.assign(new Error('Configuration changed.'), { status: 409 })); });
    const first = manager.change('', 'PUT', { enabled: false });
    assert.equal(ids.get('mcp-view').attributes['aria-busy'], 'true');
    assert.equal(ids.get('mcp-enabled').disabled, true);
    await manager.change('', 'PUT', {});
    release(); await first;
    assert.equal(form.elements.command.value, 'unsaved');
    assert.equal(ids.get('mcp-status').classList.contains('error'), true);
    assert.match(ids.get('mcp-status').textContent, /Reload before trying again/);
    assert.equal(ids.get('mcp-view').attributes['aria-busy'], 'false');
});

test('connection, enable, disconnect and confirmed remove actions use guarded server endpoints', async t => {
    const { manager, ids, calls } = ui(t);
    const previous = globalThis.confirm;
    t.after(() => { globalThis.confirm = previous; });
    globalThis.confirm = () => true;
    await manager.reload();
    const card = ids.get('mcp-servers').children[0];
    const actions = card.children.find(node => node.classList?.contains('mcp-actions'));
    for (const label of ['Connect', 'Refresh tools', 'Disconnect', 'Remove']) {
        const button = actions.children.find(node => node.textContent === label);
        button.handlers.click();
        await tick();
    }
    assert.deepEqual(calls.map(call => [call[0], call[1]]), [
        ['/servers/demo/connect', 'POST'], ['/servers/demo/refresh', 'POST'], ['/servers/demo/disconnect', 'POST'], ['/servers/demo', 'DELETE'],
    ]);
    const enabled = card.children.find(node => node.classList?.contains('mcp-check')).children[0];
    enabled.checked = false; enabled.handlers.change(); await tick();
    assert.equal(calls[4][2].config.enabled, false);
    assert.ok(!('enabled_tools' in calls[4][2].config));
});

test('a failed server setting restores its persisted checkbox state', async t => {
    const { manager, client, ids } = ui(t);
    await manager.reload();
    client.changeMCP = async () => { throw new Error('Cannot save configuration.'); };
    const card = ids.get('mcp-servers').children[0];
    const enabled = card.children.find(node => node.classList?.contains('mcp-check')).children[0];
    enabled.checked = false;
    await enabled.handlers.change();
    assert.equal(enabled.checked, true);
    assert.match(ids.get('mcp-status').textContent, /Cannot save/);
});

test('reviewed unavailable choices remain editable without enabling an unreviewed tool', () => {
    const view = fixture(), server = view.servers[0];
    server.tools[0].valid = false;
    server.tools[0].enabled = false;
    const controls = new Map(server.tools.map(tool => [tool.name, { enable: { checked: false }, approval: { checked: true } }]));
    const payload = toolChoices(server, controls, view);
    assert.deepEqual(payload.enabled_tools, []);
    assert.deepEqual(payload.fingerprints, {});
    assert.deepEqual(payload.tool_approvals, { echo: true, changed: true });
});
