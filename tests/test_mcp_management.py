import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.http.mcp import router
from app.integrations import IntegrationRegistry, InvocationContext, ToolCall
from app.integrations.mcp import MCPIntegration, capability_name
from app.integrations.mcp_config_store import MCPConfigStore, MCPManagementError
from app.integrations.mcp_management import MCPManagement
from app.integrations.mcp_runtime import MCPRuntime, MCPRuntimeError, tool_fingerprint
from tests.test_mcp_integration import definition


class MCPConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "assistant.yaml"
        self.original = '# before\nllm:\n  model: "leave this quote" # keep\nintegrations:\n  web: {enabled: true} # keep web\n  mcp:\n    enabled: false\n    servers: {}\n# after MCP\nother:\n  literal: |\n    untouched\n'
        self.path.write_text(self.original)
        self.path.chmod(0o600)
        self.store = MCPConfigStore(self.path)

    def test_only_mcp_is_replaced_with_unrelated_bytes_comments_and_permissions_preserved(self):
        config, revision = self.store.read()
        config["enabled"] = True
        updated_revision = self.store.write(config, revision)
        result = self.path.read_text()
        self.assertTrue(result.startswith(self.original.split('  mcp:')[0]))
        self.assertTrue(result.endswith(self.original[self.original.index('# after MCP'):]))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(updated_revision, revision)
        self.assertEqual(self.store.read(), (config, updated_revision))

    def test_missing_and_empty_parent_sections_are_supported_without_rewriting_other_sections(self):
        for initial in ['llm: {model: unchanged}\n', 'llm: {model: unchanged}\nintegrations: {}\n',
                        'integrations:\n  web: {enabled: false}\nother: {untouched: true}\n']:
            with self.subTest(initial=initial):
                self.path.write_text(initial)
                config, revision = self.store.read()
                self.store.write(config, revision)
                expected = yaml.safe_load(initial)
                actual = yaml.safe_load(self.path.read_text())
                actual['integrations'].pop('mcp')
                if not expected.get('integrations'):
                    expected['integrations'] = {}
                self.assertEqual(actual, expected)

    def test_atomic_replace_failure_retains_original_and_removes_temporary_file(self):
        config, revision = self.store.read()
        with patch('app.integrations.mcp_config_store.os.replace', side_effect=OSError('private-data')), self.assertRaises(MCPManagementError) as error:
            self.store.write(config, revision)
        self.assertNotIn('private-data', str(error.exception))
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse(list(self.path.parent.glob('.mcp-*.yaml')))

    def test_competing_writers_and_external_edits_reject_stale_revisions(self):
        config, revision = self.store.read()
        config['enabled'] = True
        def write():
            try:
                return MCPConfigStore(self.path).write(config, revision)
            except MCPManagementError as error:
                return error.category
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: write(), range(2)))
        self.assertEqual(outcomes.count('stale_revision'), 1)
        self.path.write_text(self.path.read_text() + '# concurrent manual edit\n')
        with self.assertRaises(MCPManagementError) as error:
            self.store.write(config, outcomes[0] if outcomes[0] != 'stale_revision' else outcomes[1])
        self.assertEqual(error.exception.category, 'stale_revision')

    def test_ambiguous_yaml_is_refused_without_echoing_content(self):
        for source in ['integrations: {}\nintegrations: {}\n', 'sensitive: &key private-secret\nintegrations: {mcp: *key}\n']:
            self.path.write_text(source)
            with self.assertRaises(MCPManagementError) as error:
                self.store.read()
            self.assertNotIn('private-secret', str(error.exception))
            self.assertEqual(self.path.read_text(), source)


class MCPManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'assistant.yaml'
        self.settings = {'enabled': True, 'servers': {'demo': {'transport': 'stdio', 'command': sys.executable, 'enabled': True}}}
        self.path.write_text(yaml.safe_dump({'integrations': {'mcp': self.settings}, 'llm': {'model': 'keep-me'}}))
        self.runtime = MCPRuntime(self.settings)
        self.integration = MCPIntegration(self.runtime)
        self.registry = IntegrationRegistry([self.integration])
        self.addCleanup(self.integration.close)
        self.manager = MCPManagement(self.integration, self.registry, MCPConfigStore(self.path))
        self.definitions = [definition()]
        self.runtime.refresh_catalog = Mock(side_effect=lambda _: deepcopy(self.definitions))
        self.runtime.invoke = Mock(return_value={'content': [{'type': 'text', 'text': 'done'}]})
        self.runtime._publish('demo', 'connected')
        self.call = ToolCall(capability_name('demo', 'echo'), {'text': 'hello'})
        self.app = FastAPI()
        self.app.include_router(router)

        async def run_mcp(_request, method, *args):
            return await asyncio.to_thread(getattr(self.manager, method), *args)

        self.app.state.run_mcp = run_mcp
        self.app.state.mcp_token = 'test-same-app-token'
        self.app.state.settings = SimpleNamespace(server={'host': '127.0.0.1'})
        self.client = TestClient(self.app, base_url='http://localhost')
        self.addCleanup(self.client.close)
        self.headers = {'X-Astra-MCP-Client': '1', 'X-Astra-MCP-Token': 'test-same-app-token', 'Origin': 'http://localhost'}

    def view(self):
        return self.manager.view()

    def change(self, operation, *, server_id='demo', **payload):
        return self.manager.change(operation, {'revision': self.view()['revision'], **payload}, server_id)

    def refresh(self):
        return self.change('refresh')

    def select(self, *, enabled=True, approval=False):
        view = self.view()
        return self.change('tools', catalog_revision=view['catalog_revision'], enabled_tools=['echo'] if enabled else [],
                           fingerprints={'echo': tool_fingerprint(self.definitions[0])} if enabled else {},
                           tool_approvals={'echo': approval})

    def invoke(self, snapshot=None, **context):
        return self.registry.invoke(self.call, InvocationContext('s', '', tool_catalog=snapshot, **context))

    def test_server_crud_enable_disable_and_atomic_persistence(self):
        created = self.change('create', id='http_server', config={'transport': 'streamable_http', 'url': 'http://127.0.0.1:3001/mcp'})
        server = next(item for item in created['servers'] if item['id'] == 'http_server')
        self.assertEqual(server['status']['state'], 'disabled')
        self.assertEqual(server['tools'], [])
        for enabled in (True, False):
            updated = self.change('edit', server_id='http_server', config={'transport': 'streamable_http', 'url': 'http://127.0.0.1:3001/mcp', 'enabled': enabled})
            server = next(item for item in updated['servers'] if item['id'] == 'http_server')
            self.assertEqual(server['status']['state'], 'disconnected' if enabled else 'disabled')
        removed = self.change('remove', server_id='http_server')
        self.assertEqual([item['id'] for item in removed['servers']], ['demo'])
        self.assertEqual(yaml.safe_load(self.path.read_text())['llm'], {'model': 'keep-me'})

    def test_tools_default_disabled_and_enablement_authority_and_approval_are_separate(self):
        view = self.refresh()
        tool = view['servers'][0]['tools'][0]
        self.assertFalse(tool['enabled'])
        self.assertEqual(self.invoke().status.value, 'denied')
        self.select(approval=True)
        self.assertEqual(self.invoke().status.value, 'denied')
        self.assertEqual(self.invoke(approval_callback=lambda _: True, allowed_capabilities=frozenset()).status.value, 'denied')
        self.assertEqual(self.invoke(approval_callback=lambda _: True).status.value, 'success')
        self.select(approval=False)
        self.assertEqual(self.invoke().status.value, 'success')
        persisted = MCPConfigStore(self.path).read()[0]['servers']['demo']
        self.assertEqual(persisted['tool_fingerprints'], {'echo': tool_fingerprint(self.definitions[0])})
        self.assertEqual(persisted['tool_approvals'], {'echo': False})

    def test_schema_drift_stays_pending_until_review_and_old_revision_is_rejected(self):
        self.refresh(); self.select()
        stale = self.view()
        self.definitions[0]['description'] = 'Changed operation'
        self.refresh()
        self.refresh()  # Another refresh must not clear the pending-review marker.
        tool = self.view()['servers'][0]['tools'][0]
        self.assertTrue(tool['pending_review'])
        self.assertFalse(tool['enabled'])
        self.assertEqual(self.invoke().status.value, 'denied')
        with self.assertRaises(MCPManagementError):
            self.manager.change('tools', {'revision': stale['revision'], 'catalog_revision': stale['catalog_revision'],
                                        'enabled_tools': ['echo'], 'fingerprints': {'echo': '0' * 64}}, 'demo')
        self.select()
        self.assertFalse(self.view()['servers'][0]['tools'][0]['pending_review'])
        self.assertEqual(self.invoke().status.value, 'success')

    def test_active_snapshot_keeps_generation_enablement_and_approval_until_release(self):
        self.refresh(); self.select()
        snapshot = self.registry.capture_tool_catalog()
        self.addCleanup(snapshot.close)
        old = self.integration._generations['demo']
        started, release = threading.Event(), threading.Event()
        def invoking(*_):
            started.set()
            self.assertTrue(release.wait(3))
            return {'content': [{'type': 'text', 'text': 'old endpoint'}]}
        self.runtime.invoke.side_effect = invoking
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.invoke, snapshot)
            self.assertTrue(started.wait(2))
            changed = self.change('edit', config={'transport': 'stdio', 'command': sys.executable, 'args': ['changed-server.py'], 'enabled': True})
            self.assertFalse(old.closed.is_set())
            self.assertFalse(changed['servers'][0]['tools'][0]['enabled'])
            self.assertEqual(self.invoke().status.value, 'denied')
            release.set()
            self.assertEqual(running.result(timeout=3).status.value, 'success')
        self.assertEqual(self.invoke(snapshot).status.value, 'success')
        snapshot.close()
        self.assertTrue(old.closed.wait(3))
        self.assertFalse(old.cleanup_failed)

    def test_policy_change_applies_only_to_new_snapshots_and_removal_waits_for_admitted_turn(self):
        self.refresh(); self.select()
        snapshot = self.registry.capture_tool_catalog()
        self.addCleanup(snapshot.close)
        generation = self.integration._generations['demo']
        self.select(approval=True)
        self.assertEqual(self.invoke().status.value, 'denied')
        self.assertEqual(self.invoke(snapshot).status.value, 'success')
        self.change('remove')
        self.assertEqual(self.registry.get_native_tools(), [])
        self.assertEqual(self.invoke().status.value, 'denied')
        self.assertFalse(generation.closed.is_set())
        self.assertEqual(self.invoke(snapshot).status.value, 'success')
        snapshot.close()
        self.assertTrue(generation.closed.wait(3))
        self.assertEqual(self.invoke(snapshot).status.value, 'denied')

    def test_reviewed_choices_can_be_removed_while_server_is_disabled(self):
        self.refresh()
        self.select()
        self.change('global', enabled=False)
        view = self.view()
        tool = view['servers'][0]['tools'][0]
        self.assertTrue(tool['selected'])
        self.assertFalse(tool['enabled'])
        self.assertFalse(tool['valid'])
        self.change('tools', catalog_revision=view['catalog_revision'], enabled_tools=[], fingerprints={},
                    tool_approvals={'echo': True})
        self.assertEqual(self.view()['servers'][0]['config']['enabled_tools'], [])
        self.assertEqual(self.invoke().status.value, 'denied')

    def test_persistence_failure_does_not_publish_permissions(self):
        self.refresh()
        original = self.path.read_bytes()
        with patch.object(self.manager.store, 'write', side_effect=MCPManagementError('persistence_failed', 'Cannot save', 503)), self.assertRaises(MCPManagementError):
            self.select()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.invoke().status.value, 'denied')

    def test_shutdown_rejects_later_publication_without_changing_the_file(self):
        original = self.path.read_bytes()
        self.integration.close()
        with self.assertRaises(MCPRuntimeError) as error:
            self.change('global', enabled=False)
        self.assertEqual(error.exception.category, 'closed')
        self.assertEqual(self.path.read_bytes(), original)

    def test_concurrent_management_and_stale_catalog_are_fail_closed(self):
        self.refresh()
        view = self.view()
        payload = {'revision': view['revision'], 'config': {'transport': 'stdio', 'command': sys.executable, 'enabled': False}}
        def changing():
            try:
                return self.manager.change('edit', payload, 'demo')
            except MCPManagementError as error:
                return error.category
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: changing(), range(2)))
        self.assertEqual(results.count('stale_revision'), 1)
        with self.assertRaises(MCPManagementError) as error:
            self.change('tools', catalog_revision=view['catalog_revision'], enabled_tools=[], fingerprints={})
        self.assertEqual(error.exception.category, 'stale_catalog')

    def test_api_same_application_boundary_and_sanitized_validation(self):
        self.assertEqual(self.client.get('/api/mcp').status_code, 403)
        self.assertEqual(self.client.get('/api/mcp', headers={**self.headers, 'Origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/mcp', headers={**self.headers, 'Host': 'evil.example', 'Origin': 'http://evil.example'}).status_code, 403)
        get = self.client.get('/api/mcp', headers=self.headers)
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.headers['cache-control'], 'no-store')
        revision = get.json()['revision']
        url = '/api/mcp/servers'
        body = {'revision': revision, 'id': 'bad', 'config': {'transport': 'stdio', 'command': 'python', 'env': {'KEY': 'private-token-value!'}}}
        wrong = self.client.post(url, headers={**self.headers, 'X-Astra-MCP-Token': 'wrong'}, json=body)
        self.assertEqual(wrong.status_code, 403)
        invalid = self.client.post(url, headers=self.headers, json=body)
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('private-token-value', invalid.text)
        self.assertEqual(self.client.post(url, headers=self.headers, content='x' * (256 * 1024 + 1)).status_code, 415)
        self.assertEqual(self.client.post(url, headers={**self.headers, 'Content-Type': 'application/json'}, content='x' * (256 * 1024 + 1)).status_code, 413)
        body['config'] = {'transport': 'streamable_http', 'url': 'http://127.0.0.1:3001/mcp'}
        valid = self.client.post(url, headers=self.headers, json=body)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 409)

    def test_api_credentials_and_transport_errors_are_redacted(self):
        with patch.dict(os.environ, {'ASTRA_MCP_PRIVATE': 'private-value'}):
            self.runtime._resolve_references({'TOKEN': 'ASTRA_MCP_PRIVATE'})
        self.definitions = [definition('private-value')]
        view = self.refresh()
        self.assertNotIn('private-value', json.dumps(view))
        self.runtime.connect = Mock(side_effect=MCPRuntimeError('authentication_failed', 'MCP authentication was rejected.'))
        result = self.client.post('/api/mcp/servers/demo/connect', headers=self.headers, json={'revision': self.view()['revision']})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error']['category'], 'authentication_failed')
        # A new connection generation must retain redaction of prior credentials,
        # including invalid catalog names which are still shown as diagnostics.
        changed = self.change('edit', config={'transport': 'stdio', 'command': sys.executable, 'args': ['different.py'], 'enabled': True})
        self.assertNotIn('private-value', json.dumps(changed))

    def test_connection_work_does_not_block_web_event_loop(self):
        async def exercise():
            started, release = threading.Event(), threading.Event()
            def connecting(_):
                started.set()
                self.assertTrue(release.wait(3))
            self.runtime.connect = Mock(side_effect=connecting)
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://localhost', headers=self.headers) as client:
                task = asyncio.create_task(client.post('/api/mcp/servers/demo/connect', json={'revision': self.view()['revision']}))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                # This coroutine can run while the connection worker remains blocked.
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                self.assertEqual((await task).status_code, 200)
        asyncio.run(exercise())


class MCPManagementWireTests(unittest.TestCase):
    def test_reconfigured_process_generation_drains_and_executor_releases_its_own_catalog(self):
        from app.core.tool_executor import ToolExecutor
        from tests.support import consume_generator
        from tests.test_mcp_integration import FIXTURE
        from tests.test_mcp_runtime import process_exists

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'assistant.yaml'
            old_state = Path(directory) / 'old.json'
            new_state = Path(directory) / 'new.json'
            old_state.write_text('{}')
            new_state.write_text('{}')
            config = {'enabled': True, 'servers': {'fixture': {
                'transport': 'stdio', 'command': sys.executable, 'args': [str(FIXTURE)], 'enabled': True,
                'env': {'ASTRA_MCP_STATE_FILE': 'ASTRA_MCP_OLD_STATE'}, 'approval_required': False,
            }}}
            path.write_text(yaml.safe_dump({'integrations': {'mcp': config}}))
            runtime = MCPRuntime(config)
            integration = MCPIntegration(runtime)
            registry = IntegrationRegistry([integration])
            manager = MCPManagement(integration, registry, MCPConfigStore(path))
            def change(operation, **payload):
                return manager.change(operation, {'revision': manager.view()['revision'], **payload}, 'fixture')
            def select():
                view = manager.view()
                tool = next(tool for tool in view['servers'][0]['tools'] if tool['name'] == 'echo')
                return change('tools', catalog_revision=view['catalog_revision'], enabled_tools=['echo'],
                              fingerprints={'echo': tool['fingerprint']}, tool_approvals={'echo': False})
            with patch.dict(os.environ, {'ASTRA_MCP_OLD_STATE': str(old_state), 'ASTRA_MCP_NEW_STATE': str(new_state)}):
                try:
                    change('refresh')
                    select()
                    old_generation = integration._generations['fixture']
                    old_pid = int(old_state.with_suffix('.pid').read_text())
                    snapshot = registry.capture_tool_catalog()
                    try:
                        changed = deepcopy(manager.view()['servers'][0]['config'])
                        for key in ('enabled_tools', 'tool_fingerprints', 'tool_approvals'):
                            changed.pop(key)
                        changed['env'] = {'ASTRA_MCP_STATE_FILE': 'ASTRA_MCP_NEW_STATE'}
                        change('edit', config=changed)
                        self.assertFalse(old_generation.closed.is_set())
                        self.assertTrue(process_exists(old_pid))
                        change('refresh')
                        self.assertFalse(next(tool for tool in manager.view()['servers'][0]['tools'] if tool['name'] == 'echo')['enabled'])
                        select()
                        call = ToolCall(capability_name('fixture', 'echo'), {'text': 'old'})
                        self.assertEqual(registry.invoke(call, InvocationContext('s', '', tool_catalog=snapshot)).status.value, 'success')
                        self.assertEqual(json.loads(old_state.with_suffix('.calls').read_text()), ['echo'])
                        self.assertFalse(new_state.with_suffix('.calls').exists())
                        # A standalone executor also holds its generation across yields.
                        execution = ToolExecutor(registry).execute(call, 's', '')
                        next(execution)
                        current = integration._generations['fixture']
                        view = manager.view()
                        manager.change('global', {'revision': view['revision'], 'enabled': False})
                        self.assertFalse(current.closed.is_set())
                        _, outcome = consume_generator(execution)
                        self.assertEqual(outcome.status.value, 'success')
                        self.assertTrue(current.closed.wait(4))
                        self.assertEqual(json.loads(new_state.with_suffix('.calls').read_text()), ['echo'])
                    finally:
                        snapshot.close()
                    self.assertTrue(old_generation.closed.wait(4))
                    self.assertFalse(old_generation.cleanup_failed)
                    self.assertFalse(process_exists(old_pid))
                finally:
                    integration.close()
                self.assertFalse(process_exists(int(new_state.with_suffix('.pid').read_text())))
