"""Captured Gmail catalog regression; never starts Gmail or accesses an account."""

import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from jsonschema.exceptions import ValidationError

from app.integrations import IntegrationRegistry, InvocationContext, ToolCall
from app.integrations.mcp import (
    MCPArgumentLimit,
    MCPIntegration,
    _validate_schema,
    capability_name,
    validate_mcp_arguments,
)
from app.integrations.mcp_runtime import MCPRuntime, tool_fingerprint

CATALOG = json.loads((Path(__file__).parent / 'fixtures/mcp_gmail_2_3_3/catalog.json').read_text())
AFFECTED = ('message-search', 'messages-export-csv')


class GmailSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tools = deepcopy(CATALOG['tools'])
        self.runtime = MCPRuntime({'enabled': True, 'servers': {'gmail': {
            'enabled': True, 'transport': 'stdio', 'command': sys.executable,
        }}})
        self.addCleanup(self.runtime.close)
        self.runtime._publish('gmail', 'connected')
        self.runtime.refresh_catalog = Mock(side_effect=lambda _: deepcopy(self.tools))
        self.runtime.invoke = Mock(return_value={'content': [{'type': 'text', 'text': 'synthetic result'}]})
        self.adapter = MCPIntegration(self.runtime)
        self.registry = IntegrationRegistry([self.adapter])
        self.adapter.refresh_catalog('gmail', self.registry)

    def schema(self, name='message-search'):
        return next(tool['input_schema'] for tool in self.tools if tool['name'] == name)

    def call(self, arguments, name='message-search', **context):
        return self.registry.invoke(ToolCall(capability_name('gmail', name), arguments), InvocationContext('test', 'test', **context))

    def enable(self, names=AFFECTED):
        self.adapter.configure_tools('gmail', list(names), self.registry, approval_required=True)

    def test_real_catalog_and_exact_original_recursive_date_constraints(self):
        self.assertEqual(CATALOG['version'], '2.3.3')
        self.assertEqual(len(self.tools), 15)
        for tool in self.tools:
            with self.subTest(tool=tool['name']):
                _validate_schema(tool['input_schema'], 65536)
                if tool.get('output_schema'):
                    _validate_schema(tool['output_schema'], 65536, allow_recursive=False)
        for name in AFFECTED:
            schema = self.schema(name)
            query = schema['$defs']['__schema0']
            self.assertEqual(schema['properties']['query']['anyOf'][0]['$ref'], '#/$defs/__schema0')
            for op in ('$and', '$or'):
                self.assertEqual(query['properties'][op]['items']['$ref'], '#/$defs/__schema0')
            self.assertEqual(query['properties']['$not']['$ref'], '#/$defs/__schema0')
            for op in ('$gte', '$lt'):
                self.assertEqual(query['properties']['date']['properties'][op]['pattern'], r'^\d{4}-\d{2}-\d{2}$')
        self.assertEqual(self.adapter.catalog_issues(), {})
        self.assertEqual(self.registry.get_native_tools(), [])  # Catalog acceptance grants nothing.

    def test_schemas_fingerprints_and_nested_arguments_are_not_rewritten(self):
        self.enable()
        exposed = {row['function']['name']: row['function']['parameters'] for row in self.registry.get_native_tools()}
        arguments = {'query': {'$and': [{'from': 'sender'}, {'$or': [{'isRead': True}, {'$not': {'subject': 'subject'}}]}], 'date': {'$gte': '2026-11-04', '$lt': '2026-12-01'}}}
        original = deepcopy(arguments)
        for name in AFFECTED:
            with self.subTest(tool=name):
                self.assertEqual(exposed[str(capability_name('gmail', name))], self.schema(name))
                self.assertEqual(self.call(arguments, name, approval_callback=lambda _: True).status.value, 'success')
                definition = next(t for t in self.tools if t['name'] == name)
                self.assertEqual(self.runtime.invoke.call_args.args[2:], (original, tool_fingerprint(definition)))
                self.assertEqual(self.adapter.enabled_tool_fingerprints('gmail')[name], tool_fingerprint(definition))
        self.assertEqual(arguments, original)

    def test_nested_constraint_failures_never_request_approval_or_dispatch(self):
        self.enable()
        approval = Mock(return_value=True)
        invalid = [
            {'query': {'$and': [{'isRead': 'true'}]}},
            {'query': {'$not': {'date': {'$gte': '2026/11/04'}}}},
            {'query': {'$or': [{'date': {'$lt': 'yesterday'}}]}},
            {'query': {'unknownFilter': True}}, {'query': ''},
        ]
        for name in AFFECTED:
            for arguments in invalid:
                with self.subTest(tool=name, arguments=arguments):
                    result = self.call(arguments, name, approval_callback=approval)
                    self.assertEqual(result.status.value, 'error')
                    self.assertEqual(result.diagnostics['category'], 'mcp_argument_validation')
        for arguments in ({'pageSize': 0}, {'pageSize': 1001}, {'pageSize': '50'}, {'shape': 'unknown'}):
            self.assertEqual(self.call(arguments, approval_callback=approval).status.value, 'error')
        approval.assert_not_called()
        self.runtime.invoke.assert_not_called()
        with self.assertRaises(ValidationError):
            validate_mcp_arguments(self.schema(), {'query': {'$not': {'isRead': 'true'}}})

    def test_enablement_authority_approval_and_frozen_catalog_remain_separate(self):
        arguments = {'query': {'rawGmailQuery': 'subject:synthetic'}}
        self.assertEqual(self.call(arguments).status.value, 'denied')
        self.enable()
        self.assertEqual(self.call(arguments, allowed_capabilities=frozenset(), approval_callback=lambda _: True).status.value, 'denied')
        self.assertEqual(self.call(arguments).status.value, 'denied')
        self.assertEqual(self.call(arguments, approval_callback=lambda _: False).status.value, 'denied')
        self.runtime.invoke.assert_not_called()
        frozen = self.registry.capture_tool_catalog()
        self.adapter.configure_tools('gmail', [], self.registry)
        self.assertEqual(self.call(arguments, approval_callback=lambda _: True).status.value, 'denied')
        self.assertEqual(self.call(arguments, approval_callback=lambda _: True, tool_catalog=frozen).status.value, 'success')
        frozen.close()

    def test_changed_recursive_contract_requires_explicit_review(self):
        self.enable()
        schema = self.schema()
        schema['$defs']['__schema0']['properties']['subject']['minLength'] = 2
        self.adapter.refresh_catalog('gmail', self.registry)
        self.assertIn('schema_changed', self.adapter.catalog_issues()[('gmail', 'message-search')])
        self.assertEqual(self.call({'query': {'subject': 'valid'}}, approval_callback=lambda _: True).status.value, 'denied')
        self.runtime.invoke.assert_not_called()
        self.enable()
        self.assertEqual(self.call({'query': {'subject': 'x'}}, approval_callback=lambda _: True).status.value, 'error')

    def test_previously_rejected_contract_stays_disabled_until_manual_review(self):
        original = deepcopy(self.tools)
        self.schema()['$defs']['__schema0']['properties']['date']['properties']['$gte']['pattern'] = '(a+)+$'
        self.adapter.refresh_catalog('gmail', self.registry)
        self.assertIn('invalid_schema', self.adapter.catalog_issues()[('gmail', 'message-search')])
        self.tools = original
        self.adapter.refresh_catalog('gmail', self.registry)
        tool = next(t for t in self.adapter.management_view()['servers'][0]['tools'] if t['name'] == 'message-search')
        self.assertTrue(tool['valid'])
        self.assertTrue(tool['pending_review'])
        self.assertFalse(tool['enabled'])
        self.assertEqual(self.registry.get_native_tools(), [])
        self.runtime.invoke.assert_not_called()

    def test_deep_oversized_and_nonfinite_arguments_fail_before_approval(self):
        self.enable()
        approval = Mock(return_value=True)
        deep = {'isRead': True}
        for _ in range(40):
            deep = {'$not': deep}
        cyclic = {}
        cyclic['query'] = cyclic
        for args in ({'query': deep}, {'query': 'x' * 65537}, {'pageSize': float('nan')}, cyclic):
            result = self.call(args, approval_callback=approval)
            self.assertEqual(result.status.value, 'error')
            self.assertEqual(result.diagnostics['category'], 'mcp_argument_limit')
        approval.assert_not_called()
        self.runtime.invoke.assert_not_called()

    def test_branching_recursive_validation_is_bounded_and_thread_local(self):
        # Each recursion consumes a property, but repeated oneOf branches are exponential.
        schema = {
            'type': 'object',
            '$defs': {'node': {'type': 'object', 'properties': {
                'next': {'oneOf': [{'$ref': '#/$defs/node'}, {'$ref': '#/$defs/node'}]},
            }}},
            'properties': {'node': {'$ref': '#/$defs/node'}},
        }
        _validate_schema(schema, 65536)
        value = {}
        for _ in range(18):
            value = {'next': value}
        with self.assertRaises(MCPArgumentLimit):
            validate_mcp_arguments(schema, {'node': value})
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(validate_mcp_arguments, self.schema(), {'query': {'$not': {'isRead': bool(i % 2)}}}) for i in range(12)]
            for future in futures:
                future.result(timeout=3)
        self.tools = [{'name': 'message-search', 'input_schema': schema}]
        self.adapter.refresh_catalog('gmail', self.registry)
        self.enable(('message-search',))
        approval = Mock(return_value=True)
        result = self.call({'node': value}, approval_callback=approval)
        self.assertEqual(result.diagnostics['category'], 'mcp_argument_limit')
        approval.assert_not_called()
        self.runtime.invoke.assert_not_called()

    def test_unsafe_references_regexes_and_output_recursion_remain_rejected(self):
        bad = [
            ({'type': 'object', '$ref': 'https://example.invalid/private'}, 'external references'),
            ({'type': 'object', '$defs': {'node': {'$ref': '#/$defs/node'}}, '$ref': '#/$defs/node'}, 'cycle must descend'),
            ({'type': 'object', 'properties': {'value': {'pattern': '(a+)+$'}}}, 'Unsupported regular expression'),
            ({'type': 'object', 'patternProperties': {'x': {}}}, 'patternProperties'),
            ({'type': 'object', 'properties': {'value': {'$schema': 'https://json-schema.org/draft/2020-12/schema'}}}, 'Nested schema dialect'),
        ]
        for schema, reason in bad:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                _validate_schema(schema, 65536)
        with self.assertRaisesRegex(ValueError, 'Recursive output schemas'):
            _validate_schema(self.schema(), 65536, allow_recursive=False)

    def test_management_diagnostics_identify_keyword_location_and_redact_secrets(self):
        secret = 'fixture/secret~in-property-name'
        self.runtime._secret_values.add(secret)
        self.tools = [{'name': 'unsupported', 'input_schema': {'type': 'object', 'properties': {
            secret: {'type': 'string', 'pattern': '(a+)+$'}
        }}}]
        self.adapter.refresh_catalog('gmail', self.registry)
        tool = next(t for t in self.adapter.management_view()['servers'][0]['tools'] if t['name'] == 'unsupported')
        self.assertFalse(tool['valid'])
        self.assertFalse(tool['enabled'])
        self.assertIn('input_schema#/properties/', tool['issue'])
        self.assertIn('/pattern', tool['issue'])
        self.assertIn('Unsupported regular expression', tool['issue'])
        self.assertNotIn(secret, json.dumps(tool))
        self.assertNotIn(secret.replace('~', '~0').replace('/', '~1'), json.dumps(tool))
        self.assertNotIn('(a+)+$', json.dumps(tool))
        self.assertIn('validation reason', tool['description'])


if __name__ == '__main__':
    unittest.main()
