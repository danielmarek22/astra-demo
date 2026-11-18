const POLICY_KEYS = new Set(['enabled_tools', 'tool_fingerprints', 'tool_approvals']);
const CONNECTION_KEYS = new Set(['description', 'transport', 'command', 'args', 'cwd', 'env', 'url', 'headers', 'enabled', 'approval_required']);

export function editableServer(config) {
    return Object.fromEntries(Object.entries(config).filter(([key]) => !POLICY_KEYS.has(key)));
}

export function toolState(tool, server, enabled) {
    if (tool.pending_review) return 'Pending review';
    if (!tool.valid) return 'Unavailable';
    if ((tool.selected ?? tool.enabled) && (!enabled || !server.config.enabled || server.status.state !== 'connected')) return 'Unavailable';
    return (tool.selected ?? tool.enabled) ? 'Enabled' : 'Disabled';
}

export function toolChoices(server, controls, snapshot) {
    const eligible = server.tools.filter(tool => tool.valid || tool.selected);
    const enabled = eligible.filter(tool => controls.get(tool.name)?.enable.checked);
    return {
        revision: snapshot.revision, catalog_revision: snapshot.catalog_revision,
        enabled_tools: enabled.map(tool => tool.name),
        fingerprints: Object.fromEntries(enabled.map(tool => [tool.name, tool.fingerprint])),
        tool_approvals: Object.fromEntries(eligible.map(tool => [tool.name, controls.get(tool.name).approval.checked])),
    };
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function checkbox(labelText, checked, disabled = false) {
    const label = element('label', 'mcp-check');
    const input = element('input');
    input.type = 'checkbox'; input.checked = Boolean(checked); input.disabled = disabled;
    label.append(input, document.createTextNode(labelText));
    return { label, input };
}

export class MCPManager {
    constructor(client) {
        this.client = client;
        this.root = document.getElementById('mcp-view');
        this.status = document.getElementById('mcp-status');
        this.list = document.getElementById('mcp-servers');
        this.form = document.getElementById('mcp-server-form');
        this.global = document.getElementById('mcp-enabled');
        this.add = document.getElementById('mcp-add');
        this.snapshot = null;
        this.busy = false;
        this.editing = null;
        document.getElementById('mcp-reload')?.addEventListener('click', () => void this.reload());
        this.add?.addEventListener('click', () => this.edit());
        document.getElementById('mcp-cancel')?.addEventListener('click', () => this.cancel());
        this.global?.addEventListener('change', () => {
            void this.change('', 'PUT', { enabled: this.global.checked }, 'MCP setting saved.');
        });
        this.form?.elements.transport.addEventListener('change', () => this.transportFields());
        this.form?.addEventListener('submit', event => {
            event.preventDefault();
            void this.saveServer();
        });
    }

    message(text, error = false) {
        this.status.textContent = text;
        this.status.classList.toggle('error', error);
    }

    async run(work, success) {
        if (this.busy) return;
        this.busy = true;
        this.root.setAttribute('aria-busy', 'true');
        const disabled = new Map(Array.from(this.root.querySelectorAll('button, input, select, textarea'), node => [node, node.disabled]));
        for (const node of disabled.keys()) node.disabled = true;
        this.message('Working…');
        try {
            this.snapshot = await work();
            this.render();
            this.message(success || 'MCP loaded.');
        } catch (error) {
            this.message(error.status === 409 ? `${error.message} Reload before trying again.` : error.message, true);
            if (this.snapshot) this.global.checked = this.snapshot.enabled;
        } finally {
            for (const [node, value] of disabled) node.disabled = value;
            this.global.disabled = this.add.disabled = !this.snapshot;
            this.root.setAttribute('aria-busy', 'false');
            this.busy = false;
        }
    }

    async reload() {
        await this.run(() => this.client.getMCP(), 'MCP loaded.');
    }

    async change(path, method, payload, success) {
        if (!this.snapshot) return;
        await this.run(() => this.client.changeMCP(path, method, { revision: this.snapshot.revision, ...payload }), success);
    }

    cancel() {
        this.editing = null;
        this.form.classList.add('hidden');
    }

    clear() {
        this.snapshot = null;
        this.cancel();
        this.list.replaceChildren();
        this.global.checked = false;
        this.global.disabled = this.add.disabled = true;
        this.message('');
    }

    transportFields() {
        for (const section of this.form.querySelectorAll('[data-mcp-transport]')) {
            section.classList.toggle('hidden', section.dataset.mcpTransport !== this.form.elements.transport.value);
        }
    }

    edit(server = null) {
        if (this.busy) return;
        this.editing = server;
        this.editRevision = this.snapshot?.revision;
        this.form.reset();
        const fields = this.form.elements;
        const config = server?.config || { transport: 'stdio', enabled: false, approval_required: true };
        fields.id.value = server?.id || '';
        fields.id.readOnly = Boolean(server);
        for (const name of ['description', 'transport', 'command', 'cwd', 'url']) fields[name].value = config[name] || (name === 'transport' ? 'stdio' : '');
        for (const name of ['enabled', 'approval_required']) fields[name].checked = Boolean(config[name]);
        for (const name of ['args', 'env', 'headers']) fields[name].value = JSON.stringify(config[name] || (name === 'args' ? [] : {}), null, 2);
        fields.limits.value = JSON.stringify(Object.fromEntries(Object.entries(editableServer(config)).filter(([key]) => !CONNECTION_KEYS.has(key))), null, 2);
        document.getElementById('mcp-form-title').textContent = server ? `Edit ${server.id}` : 'Add server';
        this.transportFields();
        this.form.classList.remove('hidden');
        fields.id.focus();
    }

    async saveServer() {
        const fields = this.form.elements;
        let config;
        try {
            const limits = JSON.parse(fields.limits.value || '{}');
            if (!limits || Array.isArray(limits) || typeof limits !== 'object' || Object.keys(limits).some(key => CONNECTION_KEYS.has(key) || POLICY_KEYS.has(key))) throw new Error();
            config = { ...limits, description: fields.description.value, enabled: fields.enabled.checked,
                approval_required: fields.approval_required.checked, transport: fields.transport.value };
            if (config.transport === 'stdio') {
                Object.assign(config, { command: fields.command.value, args: JSON.parse(fields.args.value || '[]'), env: JSON.parse(fields.env.value || '{}') });
                if (fields.cwd.value) config.cwd = fields.cwd.value;
            } else {
                Object.assign(config, { url: fields.url.value, headers: JSON.parse(fields.headers.value || '{}') });
            }
        } catch {
            this.message('Check the JSON arguments, environment references, headers and limits.', true);
            return;
        }
        const server = this.editing;
        const path = server ? `/servers/${encodeURIComponent(server.id)}` : '/servers';
        await this.change(path, server ? 'PUT' : 'POST', { config, revision: this.editRevision, ...(!server ? { id: fields.id.value } : {}) }, 'Server saved. Connect and refresh to load its catalog.');
        if (!this.status.classList.contains('error')) this.cancel();
    }

    action(container, text, handler, disabled = false) {
        const button = element('button', 'config-action-btn', text);
        button.type = 'button'; button.disabled = disabled;
        button.addEventListener('click', handler);
        container.append(button);
        return button;
    }

    render() {
        this.global.checked = this.snapshot.enabled;
        this.list.replaceChildren();
        if (!this.snapshot.servers.length) this.list.append(element('p', 'mcp-help', 'No MCP servers configured. Add a local process or Streamable HTTP server.'));
        if (this.snapshot.cleanup_pending) this.list.append(element('p', 'mcp-help', 'Previous connections are draining admitted turns.'));
        if (this.snapshot.cleanup_failed) this.list.append(element('p', 'mcp-error', 'A previous connection could not confirm cleanup. That generation will not be reused; inspect or restart Astra.'));
        for (const server of this.snapshot.servers) this.renderServer(server);
    }

    renderServer(server) {
        const card = element('section', 'mcp-card');
        card.append(element('h3', null, server.id), element('p', 'mcp-help', server.config.description || server.config.transport));
        card.append(element('p', 'mcp-status-line', `Connection: ${server.status.state}${server.status.protocol_version ? ` · MCP ${server.status.protocol_version}` : ''}`));
        if (server.status.error_message) card.append(element('p', 'mcp-error', `${server.status.error_category}: ${server.status.error_message}`));
        const endpoint = server.config.transport === 'stdio' ? server.config.command : server.config.url;
        card.append(element('p', 'mcp-endpoint', endpoint));
        const active = checkbox('Server enabled', server.config.enabled);
        active.input.addEventListener('change', async () => {
            await this.change(`/servers/${encodeURIComponent(server.id)}`, 'PUT', {
                config: { ...editableServer(server.config), enabled: active.input.checked },
            }, 'Server setting saved.');
            if (this.status.classList.contains('error')) active.input.checked = server.config.enabled;
        });
        card.append(active.label);
        const actions = element('div', 'mcp-actions');
        this.action(actions, 'Edit', () => this.edit(server));
        for (const [operation, label] of [['connect', 'Connect'], ['refresh', 'Refresh tools'], ['disconnect', 'Disconnect']]) {
            this.action(actions, label, () => void this.change(`/servers/${encodeURIComponent(server.id)}/${operation}`, 'POST', {}, `${label} completed.`),
                operation !== 'disconnect' && (!this.snapshot.enabled || !server.config.enabled));
        }
        this.action(actions, 'Remove', () => {
            if (globalThis.confirm(`Remove ${server.id}? Running turns keep their admitted connection until they finish.`)) {
                void this.change(`/servers/${encodeURIComponent(server.id)}`, 'DELETE', {}, 'Server removed.');
            }
        });
        card.append(actions);
        const form = element('form', 'mcp-tools');
        form.append(element('h4', null, 'Tools'));
        if (!server.tools.length) form.append(element('p', 'mcp-help', 'No catalog loaded. Enable the server, then connect and refresh tools.'));
        if (server.tools.some(tool => tool.valid || tool.selected)) {
            const save = element('button', 'config-action-btn mcp-tool-save', 'Save tool choices');
            save.type = 'submit'; form.append(save);
        }
        const controls = new Map();
        for (const tool of server.tools) {
            const row = element('div', 'mcp-tool');
            const enable = checkbox(tool.name, tool.selected ?? tool.enabled, !tool.valid && !tool.selected);
            const approval = checkbox('Require execution approval', tool.approval_required, !tool.valid && !tool.selected);
            controls.set(tool.name, { enable: enable.input, approval: approval.input });
            row.append(enable.label, element('span', 'mcp-badge', toolState(tool, server, this.snapshot.enabled)), element('p', null, tool.description), approval.label);
            if (tool.issue) row.append(element('p', 'mcp-help', tool.issue));
            if (tool.pending_review) row.append(element('p', 'mcp-review', 'Contract changed. Review the definition; selecting and saving accepts the updated contract.'));
            if (tool.definition || tool.input_schema) {
                const details = element('details');
                details.append(element('summary', null, 'Tool definition'), element('pre', null, JSON.stringify(tool.definition || tool.input_schema, null, 2)), element('small', null, tool.fingerprint));
                row.append(details);
            }
            form.append(row);
        }
        form.addEventListener('submit', event => {
            event.preventDefault();
            void this.change(`/servers/${encodeURIComponent(server.id)}/tools`, 'PUT', toolChoices(server, controls, this.snapshot), 'Tool choices saved for new turns.');
        });
        card.append(form); this.list.append(card);
    }
}
