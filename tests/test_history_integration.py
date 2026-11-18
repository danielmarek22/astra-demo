"""On-demand history reuses the episodic index without granting access through references."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import Config
from app.core import orchestrator_factory
from app.core.conversation import (
    InputSource,
    SenderAttribution,
    SenderType,
    SessionKind,
    relay_sender,
)
from app.core.response_generator import ResponseGenerator
from app.core.tool_executor import ToolExecutor
from app.core.turn_completion import AuthoritativeTurnContext
from app.integrations import (
    CapabilityId,
    HistoryIntegration,
    IntegrationRegistry,
    InvocationContext,
    ToolCall,
)
from app.integrations.history import MAX_OUTPUT_BYTES
from app.memory.chat_history import ChatHistoryStore
from app.memory.retriever import MemoryRetriever
from app.storage.database import Database
from tests.support import BufferedResultMock, consume_generator
from tests.test_chat_history import FakeCollection, FakeVectorStore


class HistoryCollection(FakeCollection):
    def __init__(self):
        super().__init__()
        self.ranked = None
        self.queries = []
        self.on_query = None

    def count(self):
        return len(self.ranked) if self.ranked is not None else len(self.records)

    def query(self, query_texts, n_results, where=None, include=None):
        self.queries.append({'query_texts': query_texts, 'n_results': n_results, 'where': where, 'include': include})
        if self.on_query:
            self.on_query()
        if self.ranked is not None:
            pairs = self.ranked[:n_results]
        else:
            records = self.records
            if where and 'session_id' in where:
                value = where['session_id']
                records = [r for r in records if (r['metadata']['session_id'] != value['$ne']
                           if isinstance(value, dict) else r['metadata']['session_id'] == value)]
            if where and 'role' in where:
                records = [r for r in records if r['metadata']['role'] in where['role']['$in']]
            pairs = [(r['id'], r['metadata'].get('distance', 0.2)) for r in records[:n_results]]
        return {'ids': [[ref for ref, _ in pairs]], 'distances': [[score for _, score in pairs]],
                'documents': [['FORGED VECTOR TEXT' for _ in pairs]]}


class HistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        uploads = tempfile.TemporaryDirectory()
        self.addCleanup(uploads.cleanup)
        self.uploads_root = uploads.name
        self.collection = HistoryCollection()
        self.vectors = FakeVectorStore()
        self.vectors.episodic_collection = self.collection
        self.history = ChatHistoryStore(self.db, self.vectors, uploads_root=uploads.name)
        self.registry = IntegrationRegistry([HistoryIntegration(self.history)])
        self.ctx = None

    def configure_trusted_sessions(self, session_ids, *, allow_all_sessions=False):
        # Rebuild from local startup policy, as after an application restart.
        self.history = ChatHistoryStore(self.db, self.vectors, uploads_root=self.uploads_root,
                                        trusted_session_ids=session_ids, allow_all_sessions=allow_all_sessions)
        self.registry = IntegrationRegistry([HistoryIntegration(self.history)])

    def add(self, session, text, role='user', sender=None, kind=SessionKind.DIRECT):
        return self.history.add(session, role, text, sender=sender, session_kind=kind)

    def context(self, session='current', sender=None, kind=SessionKind.DIRECT):
        sender = sender or self.history.default_sender('user')
        message_id = self.add(session, 'Current request', sender=sender, kind=kind)
        turn = AuthoritativeTurnContext('default-agent', session, message_id, 'Current request',
                                        datetime.now(timezone.utc), 'UTC', sender.sender_id,
                                        sender.sender_display_name, sender.sender_type, sender.input_source, kind)
        return InvocationContext(session, 'Current request', authoritative_turn=turn)

    def invoke(self, action, arguments, context=None):
        if context is None:
            if self.ctx is None:
                self.ctx = self.context()
            context = self.ctx
        return self.registry.invoke(ToolCall(CapabilityId('history', action), arguments), context)

    def search(self, **kwargs):
        result = self.invoke('search', {'query': 'our project decision', **kwargs})
        self.assertEqual(result.status.value, 'success', result.content)
        return json.loads(result.content)

    def test_search_retrieves_old_current_messages_outside_the_recent_window(self):
        old = self.add('current', 'We chose the Aurora architecture')
        for index in range(30):
            self.add('current', f'Unrelated later message {index}')
        self.collection.ranked = [(f'message:{old}', 0.12)]
        with patch.object(self.history, 'get_all', side_effect=AssertionError('No full-history loads')):
            match, = self.search()['matches']
        self.assertEqual(match['message_id'], old)
        self.assertEqual(match['text'], 'We chose the Aurora architecture')
        self.assertEqual(match['sender']['id'], 'local-human')
        self.assertEqual(self.collection.queries[-1]['query_texts'], ['our project decision'])
        self.assertLessEqual(self.collection.queries[-1]['n_results'], 64)
        self.assertNotIn('FORGED', str(match))

    def test_search_includes_eligible_previous_direct_chat_and_read_rechecks_ownership(self):
        old = self.add('previous', 'Earlier decision')
        self.add('previous', 'Agreed', role='assistant')
        ref = f'message:{old}'
        match, = self.search(limit=1)['matches']
        self.assertEqual((match['ref'], match['session_id']), (ref, 'previous'))
        self.assertEqual(self.invoke('read', {'ref': ref}).status.value, 'success')
        self.add('previous', 'Foreign participant', sender=relay_sender('human', 'Other person'))
        denied = self.invoke('read', {'ref': ref})
        self.assertEqual(denied.diagnostics['category'], 'not_found')
        self.assertNotIn('Earlier decision', denied.content)

    def test_group_read_is_chronological_and_preserves_relay_attribution(self):
        peer = relay_sender('external_agent', 'Peer Agent')
        human = relay_sender('human', 'Guest')
        first = self.add('group', 'Question', sender=human, kind=SessionKind.MANUAL_GROUP)
        center = self.add('group', 'Peer answer', sender=peer, kind=SessionKind.MANUAL_GROUP)
        last = self.add('group', 'Astra answer', role='assistant', kind=SessionKind.MANUAL_GROUP)
        ctx = self.context('group', sender=peer, kind=SessionKind.MANUAL_GROUP)
        result = self.invoke('read', {'ref': f'message:{center}', 'before': 1, 'after': 1}, ctx)
        records = json.loads(result.content)['messages']
        self.assertEqual([r['message_id'] for r in records], [first, center, last])
        self.assertEqual([r['text'] for r in records], ['Question', 'Peer answer', 'Astra answer'])
        self.assertEqual(records[1]['sender'], {'id': peer.sender_id, 'display_name': 'Peer Agent',
                                               'type': 'external_agent', 'input_source': 'manual_relay'})

    def test_trusted_group_search_and_read_preserve_all_participant_attribution(self):
        peer = relay_sender('external_agent', 'Research Partner')
        first = self.add('archive', 'Let us build Soul-Craft', kind=SessionKind.MANUAL_GROUP)
        center = self.add('archive', 'I propose personality design', sender=peer, kind=SessionKind.MANUAL_GROUP)
        last = self.add('archive', 'Agreed on our shared project', 'assistant', kind=SessionKind.MANUAL_GROUP)
        self.configure_trusted_sessions(['archive'])
        self.collection.ranked = [(f'message:{last}', 0.1), (f'message:{center}', 0.2), (f'message:{first}', 0.3)]
        matches = self.search()['matches']
        self.assertEqual([r['message_id'] for r in matches], [last, center, first])
        payload = json.loads(self.invoke('read', {'ref': f'message:{center}', 'before': 1, 'after': 1}).content)
        self.assertTrue(payload['untrusted'])
        self.assertEqual([r['message_id'] for r in payload['messages']], [first, center, last])
        self.assertEqual([r['sender'] for r in payload['messages']], [
            {'id': 'local-human', 'display_name': 'You', 'type': 'human', 'input_source': 'local_text'},
            {'id': peer.sender_id, 'display_name': 'Research Partner', 'type': 'external_agent', 'input_source': 'manual_relay'},
            {'id': 'default-agent', 'display_name': 'Astra', 'type': 'local_assistant', 'input_source': 'assistant_generation'},
        ])

    def test_archive_trust_is_explicit_per_session_and_is_revoked_on_restart(self):
        peer = relay_sender('external_agent', 'Research Partner')
        trusted = self.add('archive', 'Astra proposal', 'assistant', kind=SessionKind.MANUAL_GROUP)
        untrusted = self.add('other-archive', 'Same participant, private discussion', sender=peer, kind=SessionKind.MANUAL_GROUP)
        self.add('archive', 'Partner response', sender=peer, kind=SessionKind.MANUAL_GROUP)
        self.add('other-archive', 'Astra participated here too', 'assistant', kind=SessionKind.MANUAL_GROUP)
        self.collection.ranked = [(f'message:{untrusted}', 0.1), (f'message:{trusted}', 0.2)]
        self.assertEqual(self.search()['matches'], [])
        configured = ['archive']
        self.configure_trusted_sessions(configured)
        configured.append('other-archive')  # Mutation of an input list cannot expand the runtime snapshot.
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [trusted])
        self.assertEqual(self.invoke('read', {'ref': f'message:{trusted}'}).status.value, 'success')
        self.assertEqual(self.invoke('read', {'ref': f'message:{untrusted}'}).diagnostics['category'], 'not_found')
        self.configure_trusted_sessions([])
        self.assertEqual(self.search()['matches'], [])
        self.assertEqual(self.invoke('read', {'ref': f'message:{trusted}'}).diagnostics['category'], 'not_found')

    def test_allow_all_search_and_read_include_unlisted_groups_and_mixed_direct_archives(self):
        peer = relay_sender('external_agent', 'Research Partner')
        group = self.add('archive', 'Soul-Craft decision', sender=peer, kind=SessionKind.MANUAL_GROUP)
        reply = self.add('archive', 'Astra response', 'assistant', kind=SessionKind.MANUAL_GROUP)
        mixed = self.add('other-direct', 'Another archived discussion', sender=relay_sender('human', 'Guest'))
        self.collection.ranked = [(f'message:{mid}', 0.1) for mid in (group, reply, mixed)]
        self.assertEqual(self.search()['matches'], [])
        self.configure_trusted_sessions([], allow_all_sessions=True)
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [group, reply, mixed])
        payload = json.loads(self.invoke('read', {'ref': f'message:{group}', 'before': 0, 'after': 1}).content)
        self.assertEqual([r['message_id'] for r in payload['messages']], [group, reply])
        self.assertEqual(payload['messages'][0]['sender']['id'], peer.sender_id)
        self.assertEqual(payload['messages'][1]['sender']['id'], 'default-agent')
        self.assertTrue(payload['untrusted'])

    def test_allow_all_still_rejects_group_relay_mixed_and_missing_authority_receivers(self):
        archived = self.add('archive', 'Owner archive', kind=SessionKind.MANUAL_GROUP)
        self.add('mixed', 'Other participant', sender=relay_sender('human', 'Guest'))
        self.configure_trusted_sessions([], allow_all_sessions=True)
        self.collection.ranked = [(f'message:{archived}', 0.1)]
        contexts = [self.context('group', kind=SessionKind.MANUAL_GROUP),
                    self.context('relay', sender=relay_sender('human', 'You')),
                    self.context('mixed'),
                    self.context('other-owner', sender=SenderAttribution('other', 'You', SenderType.HUMAN, InputSource.LOCAL_TEXT))]
        for ctx in contexts:
            with self.subTest(session=ctx.session_id):
                self.assertEqual(json.loads(self.invoke('search', {'query': 'archive'}, ctx).content)['matches'], [])
                self.assertEqual(self.invoke('read', {'ref': f'message:{archived}'}, ctx).diagnostics['category'], 'not_found')
        ctx = self.context()
        for invalid in (replace(ctx, authoritative_turn=None), replace(ctx, event_id='event')):
            self.assertEqual(self.invoke('search', {'query': 'archive'}, invalid).status.value, 'denied')
            self.assertEqual(self.invoke('read', {'ref': f'message:{archived}'}, invalid).status.value, 'denied')

    def test_allow_all_preserves_deleted_excluded_and_missing_session_protections(self):
        good = self.add('archive', 'Visible', kind=SessionKind.MANUAL_GROUP)
        excluded = self.add('archive', 'Excluded', kind=SessionKind.MANUAL_GROUP)
        deleted = self.add('deleted', 'Deleted', kind=SessionKind.MANUAL_GROUP)
        orphan = self.add('orphan', 'Missing metadata', kind=SessionKind.MANUAL_GROUP)
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (excluded,))
            conn.execute("INSERT INTO deleted_sessions(session_id) VALUES ('deleted')")
            conn.execute("DELETE FROM chat_sessions WHERE session_id = 'orphan'")
        self.configure_trusted_sessions([], allow_all_sessions=True)
        self.collection.ranked = [(f'message:{mid}', 0.1) for mid in (good, excluded, deleted, orphan)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [good])
        window = json.loads(self.invoke('read', {'ref': f'message:{good}'}).content)
        self.assertEqual([r['message_id'] for r in window['messages']], [good])
        for mid in (excluded, deleted, orphan):
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')

    def test_disabling_allow_all_revokes_unlisted_archives_but_keeps_explicit_designations(self):
        old = self.add('archive', 'Prior group decision', kind=SessionKind.MANUAL_GROUP)
        self.configure_trusted_sessions([], allow_all_sessions=True)
        self.assertEqual(len(self.search()['matches']), 1)
        self.configure_trusted_sessions([])
        self.assertEqual(self.search()['matches'], [])
        self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}).diagnostics['category'], 'not_found')
        self.configure_trusted_sessions(['archive'])
        self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}).status.value, 'success')

    def test_trusted_mixed_direct_archive_does_not_grant_access_to_a_mixed_receiver(self):
        peer = relay_sender('human', 'Guest')
        old = self.add('mixed-archive', 'Shared direct-session discussion', sender=peer)
        own = self.add('current', 'Current message')
        self.configure_trusted_sessions(['mixed-archive', 'current'])
        self.collection.ranked = [(f'message:{old}', 0.1), (f'message:{own}', 0.2)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [old, own])
        self.add('current', 'Another participant has joined', sender=peer)
        self.ctx = self.context()
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [own])
        self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}).diagnostics['category'], 'not_found')

    def test_group_relay_and_other_human_callers_cannot_use_the_owner_archive(self):
        old = self.add('archive', 'Trusted owner archive', kind=SessionKind.MANUAL_GROUP)
        self.configure_trusted_sessions(['archive', 'group', 'relay-direct'])
        self.collection.ranked = [(f'message:{old}', 0.1)]
        contexts = [
            self.context('group', kind=SessionKind.MANUAL_GROUP),
            self.context('group', sender=relay_sender('external_agent', 'Astra'), kind=SessionKind.MANUAL_GROUP),
            self.context('relay-direct', sender=relay_sender('human', 'You')),
            self.context('other-human', sender=SenderAttribution('other', 'You', SenderType.HUMAN, InputSource.LOCAL_TEXT)),
        ]
        for ctx in contexts:
            with self.subTest(sender=ctx.authoritative_turn.sender_id):
                search = self.invoke('search', {'query': 'archive'}, ctx)
                self.assertEqual(json.loads(search.content)['matches'], [])
                self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}, ctx).diagnostics['category'], 'not_found')
                self.assertEqual(self.collection.queries[-1]['where'], {'session_id': ctx.session_id})

    def test_spoofed_names_and_relay_ids_in_source_sessions_never_establish_trust(self):
        ids = [self.add('untrusted-group', 'Untrusted content', sender=relay_sender(sender_type, name),
                        kind=SessionKind.MANUAL_GROUP)
               for sender_type, name in [('human', 'You'), ('external_agent', 'Astra')]]
        ids.append(self.add('untrusted-group', 'Astra own message', 'assistant', kind=SessionKind.MANUAL_GROUP))
        self.configure_trusted_sessions(['different-group'])
        self.collection.ranked = [(f'message:{mid}', 0.1) for mid in ids]
        self.assertEqual(self.search()['matches'], [])
        for mid in ids:
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')

    def test_trust_never_overrides_deleted_excluded_or_missing_canonical_sessions(self):
        good = self.add('archive', 'Visible decision', kind=SessionKind.MANUAL_GROUP)
        excluded = self.add('archive', 'Excluded decision', kind=SessionKind.MANUAL_GROUP)
        deleted = self.add('deleted-archive', 'Deleted decision', kind=SessionKind.MANUAL_GROUP)
        orphan = self.add('orphan-archive', 'No session metadata', kind=SessionKind.MANUAL_GROUP)
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (excluded,))
            conn.execute("INSERT INTO deleted_sessions(session_id) VALUES ('deleted-archive')")
            conn.execute("DELETE FROM chat_sessions WHERE session_id = 'orphan-archive'")
        self.configure_trusted_sessions(['archive', 'deleted-archive', 'orphan-archive'])
        self.collection.ranked = [(f'message:{mid}', 0.1) for mid in (excluded, deleted, orphan, good)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [good])
        payload = json.loads(self.invoke('read', {'ref': f'message:{good}'}).content)
        self.assertEqual([r['message_id'] for r in payload['messages']], [good])
        for mid in (excluded, deleted, orphan):
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')
        self.history.delete_session('archive')
        self.assertEqual(self.search()['matches'], [])
        self.assertEqual(self.invoke('read', {'ref': f'message:{good}'}).diagnostics['category'], 'not_found')

    def test_archive_session_deleted_during_vector_lookup_cannot_be_returned(self):
        old = self.add('archive', 'Disappearing archive', kind=SessionKind.MANUAL_GROUP)
        self.configure_trusted_sessions(['archive'])
        self.collection.ranked = [(f'message:{old}', 0.1)]
        self.collection.on_query = lambda: self.history.delete_session('archive')
        self.assertEqual(self.search()['matches'], [])

    def test_explicitly_trusted_legacy_archive_does_not_invent_sender_attribution(self):
        old = self.add('archive', 'Legacy message', kind=SessionKind.MANUAL_GROUP)
        with self.db.transaction() as conn:
            conn.execute('''UPDATE chat_history SET sender_id = NULL, sender_display_name = NULL,
                            sender_type = NULL, input_source = NULL WHERE id = ?''', (old,))
        self.configure_trusted_sessions(['archive'])
        match, = self.search()['matches']
        self.assertEqual(match['sender'], {'id': None, 'display_name': None, 'type': None, 'input_source': None})
        payload = json.loads(self.invoke('read', {'ref': match['ref'], 'before': 0, 'after': 0}).content)
        self.assertEqual(payload['messages'][0]['sender'], match['sender'])

    def test_group_and_relay_invocations_cannot_read_other_sessions(self):
        private = self.add('private', 'Private local conversation')
        other_group = self.add('other-group', 'Other group secret', kind=SessionKind.MANUAL_GROUP)
        own = self.add('group', 'Shared group content', kind=SessionKind.MANUAL_GROUP)
        for sender in (self.history.default_sender('user'), relay_sender('external_agent', 'Peer')):
            ctx = self.context('group', sender=sender, kind=SessionKind.MANUAL_GROUP)
            # A corrupt/misleading vector filter cannot override canonical access.
            self.collection.ranked = [(f'message:{private}', 0.1), (f'message:{other_group}', 0.1), (f'message:{own}', 0.2)]
            result = self.invoke('search', {'query': 'secret'}, ctx)
            self.assertEqual([m['message_id'] for m in json.loads(result.content)['matches']], [own])
            self.assertEqual(self.collection.queries[-1]['where'], {'session_id': 'group'})
            for forbidden in (private, other_group):
                self.assertEqual(self.invoke('read', {'ref': f'message:{forbidden}'}, ctx).diagnostics['category'], 'not_found')

    def test_direct_chat_cannot_read_groups_other_owners_or_legacy_unattributed_sessions(self):
        group = self.add('group', 'Group secret', kind=SessionKind.MANUAL_GROUP)
        other = self.add('other-human', 'Other human secret', sender=SenderAttribution(
            'other-human', 'Other', SenderType.HUMAN, InputSource.LOCAL_TEXT))
        foreign_agent = self.add('other-agent', 'Other agent session')
        self.add('other-agent', 'Other agent secret', 'assistant', SenderAttribution(
            'other-agent', 'Other Astra', SenderType.LOCAL_ASSISTANT, InputSource.ASSISTANT_GENERATION))
        legacy = self.add('legacy', 'Unattributed legacy content')
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET sender_id = NULL WHERE id = ?', (legacy,))
        self.collection.ranked = [(f'message:{mid}', 0.1) for mid in (group, other, foreign_agent, legacy)]
        self.assertEqual(self.search()['matches'], [])
        for mid in (group, other, foreign_agent, legacy):
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')

    def test_excluding_foreign_participants_does_not_establish_cross_session_ownership(self):
        previous = self.add('previous', 'Previous local message')
        foreign = self.add('previous', 'Hidden guest message', sender=relay_sender('human', 'Guest'))
        own = self.add('current', 'Current local message')
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (foreign,))
        self.collection.ranked = [(f'message:{previous}', 0.1), (f'message:{own}', 0.2)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [own])
        # A mixed current chat also loses cross-session access, even for its local human.
        eligible = self.add('eligible', 'Another direct chat')
        guest = self.add('current', 'Guest joined', sender=relay_sender('human', 'Guest'))
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (guest,))
        self.ctx = self.context()
        self.collection.ranked = [(f'message:{eligible}', 0.1), (f'message:{own}', 0.2)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [own])
        self.assertEqual(self.collection.queries[-1]['where'], {'session_id': 'current'})

    def test_local_voice_turns_and_custom_local_identities_can_recall_their_direct_chats(self):
        self.history.local_human_id = 'configured-human'
        self.history.local_assistant_id = 'configured-astra'
        old = self.add('previous', 'A previous voice discussion', sender=self.history.default_sender('user', InputSource.LOCAL_VOICE))
        self.add('previous', 'Astra reply', 'assistant')
        self.ctx = self.context(sender=self.history.default_sender('user', InputSource.LOCAL_VOICE))
        self.ctx = replace(self.ctx, authoritative_turn=replace(self.ctx.authoritative_turn, owner_agent_id='configured-astra'))
        match, = self.search(limit=1)['matches']
        self.assertEqual(match['message_id'], old)
        self.assertEqual(match['sender']['id'], 'configured-human')

    def test_missing_forged_or_event_authority_fails_closed_even_with_capability_permission(self):
        self.configure_trusted_sessions(['archive', 'current'])
        ctx = self.context()
        allowed = frozenset({CapabilityId('history', 'search')})
        invalid = [InvocationContext('current', 'untrusted model text'),
                   replace(ctx, event_id='event', allowed_capabilities=allowed),
                   replace(ctx, authoritative_turn=replace(ctx.authoritative_turn, owner_agent_id='other')),
                   replace(ctx, authoritative_turn=replace(ctx.authoritative_turn, sender_id='other')),
                   replace(ctx, authoritative_turn=replace(ctx.authoritative_turn, user_message_id=True)),
                   replace(ctx, session_id='another-session')]
        for context in invalid:
            with self.subTest(context=context):
                self.assertEqual(self.registry.get_native_tools(None, context), [])
                self.assertEqual(self.invoke('search', {'query': 'private'}, context).status.value, 'denied')
        self.assertEqual(self.collection.queries, [])

    def test_excluded_deleted_stale_and_distant_vector_candidates_are_skipped(self):
        good = self.add('current', 'Canonical corrected text')
        excluded = self.add('current', 'Excluded content')
        deleted = self.add('deleted', 'Deleted content')
        far = self.add('current', 'Unrelated distant content')
        diagnostic = self.add('current', 'Internal trace', role='system')
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (excluded,))
            conn.execute("INSERT INTO deleted_sessions(session_id) VALUES ('deleted')")
        self.collection.ranked = [('message:999999', 0.1), (f'message:{excluded}', 0.1), (f'message:{deleted}', 0.1),
                                  (f'message:{far}', 0.9), (f'message:{diagnostic}', 0.1),
                                  ('message:01', 0.1), (f'message:{good}', float('nan')), (f'message:{good}', 0.2)]
        self.assertEqual([r['message_id'] for r in self.search()['matches']], [good])
        for mid in (excluded, deleted, diagnostic):
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')

    def test_access_is_rechecked_after_vector_lookup_and_after_search(self):
        old = self.add('previous', 'Should disappear')
        self.ctx = self.context()
        self.collection.ranked = [(f'message:{old}', 0.1)]
        self.assertEqual(len(self.search()['matches']), 1)
        self.history.delete_session('previous')
        self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}).diagnostics['category'], 'not_found')
        def delete_current():
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO deleted_sessions(session_id) VALUES ('current')")
        self.collection.on_query = delete_current
        self.assertEqual(self.invoke('search', {'query': 'old'}).status.value, 'denied')

    def test_access_is_rechecked_if_session_kind_changes_during_vector_lookup(self):
        old = self.add('previous', 'Private old message')
        self.ctx = self.context()
        self.collection.ranked = [(f'message:{old}', 0.1)]
        def make_current_group():
            with self.db.transaction() as conn:
                conn.execute("UPDATE chat_sessions SET kind = 'manual_group' WHERE session_id = 'current'")
        self.collection.on_query = make_current_group
        self.assertEqual(self.invoke('search', {'query': 'old'}).status.value, 'denied')

    def test_read_window_skips_excluded_nonconversation_and_new_messages(self):
        first = self.add('current', 'First visible message')
        excluded = self.add('current', 'Excluded content')
        self.add('current', 'Internal tool diagnostic', 'tool')
        anchor = self.add('current', 'Anchor')
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (excluded,))
        self.ctx = self.context()
        future = self.add('current', 'Not part of this accepted turn', 'assistant')
        payload = json.loads(self.invoke('read', {'ref': f'message:{anchor}', 'before': 3, 'after': 3}).content)
        self.assertEqual([r['message_id'] for r in payload['messages']], [first, anchor])
        payload = json.loads(self.invoke('read', {'ref': f'message:{anchor}', 'before': 0, 'after': 0}).content)
        self.assertEqual([r['message_id'] for r in payload['messages']], [anchor])
        for mid in (self.ctx.authoritative_turn.user_message_id, future):
            self.assertEqual(self.invoke('read', {'ref': f'message:{mid}'}).diagnostics['category'], 'not_found')
        with self.db.transaction() as conn:
            conn.execute('UPDATE chat_history SET excluded_from_context = 1 WHERE id = ?', (anchor,))
        self.assertEqual(self.invoke('read', {'ref': f'message:{anchor}'}).diagnostics['category'], 'not_found')

    def test_empty_results_unavailable_vectors_and_read_during_vector_outage(self):
        old = self.add('current', 'Can read from SQLite')
        self.ctx = self.context()
        with patch.object(self.collection, 'query', side_effect=RuntimeError('offline')), self.assertLogs('history_integration'):
            self.assertEqual(self.invoke('search', {'query': 'query'}).status.value, 'unavailable')
            self.assertEqual(self.invoke('read', {'ref': f'message:{old}'}).status.value, 'success')
        self.collection.ranked = []
        self.assertEqual(self.search()['matches'], [])

    def test_count_failures_and_malformed_candidates_do_not_return_fabricated_history(self):
        self.ctx = self.context()
        with patch.object(self.collection, 'count', side_effect=RuntimeError('offline')), self.assertLogs('history_integration'):
            self.assertEqual(self.invoke('search', {'query': 'query'}).status.value, 'unavailable')
        with patch.object(self.collection, 'query', return_value={'ids': ['message:1'], 'distances': [[0.1]]}):
            self.assertEqual(self.search()['matches'], [])

    def test_invalid_references_schemas_and_capability_authorization(self):
        self.ctx = self.context()
        for arguments in ({'ref': 'message:999999'}, {'ref': f'message:{2**63}'}, {'ref': 'message:0'},
                          {'ref': 'attachment:1'}, {'ref': '../../private'}, {'ref': 'message:1', 'session_id': 'other'},
                          {'ref': 'message:1', 'before': 4}, {'ref': 'message:1', 'after': True}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke('read', arguments).status.value, 'error')
        for arguments in ({'query': ' '}, {'query': 'x' * 2001}, {'query': 'query', 'limit': 9},
                          {'query': 'query', 'limit': True}, {'query': 'query', 'owner': 'other'},
                          {'query': 'query', 'trusted_session_ids': ['archive']},
                          {'query': 'query', 'allow_all_sessions': True}):
            self.assertEqual(self.invoke('search', arguments).status.value, 'error')
        self.assertEqual(self.invoke('search', {'query': 'query'}, replace(self.ctx, allowed_capabilities=frozenset())).status.value, 'denied')

    def test_safe_attachment_summaries_and_parent_deduplication(self):
        mid = self.add('current', 'See the screenshot')
        with self.db.transaction() as conn:
            attachment = conn.execute('''INSERT INTO chat_attachments
                (message_id,session_id,name,mime_type,storage_path,sha256,summary_text)
                VALUES (?,?,?,?,?,?,?)''', (mid, 'current', '/home/private/screenshot.png', 'image/png',
                                           '/private/never-read.png', 'private-hash', 'Screenshot summary')).lastrowid
        self.collection.ranked = [(f'attachment:{attachment}', 0.1), (f'message:{mid}', 0.2)]
        match, = self.search()['matches']
        payload = json.loads(self.invoke('read', {'ref': match['ref']}).content)
        self.assertEqual(payload['messages'][0]['attachments'], [{'name': 'screenshot.png', 'mime_type': 'image/png',
                                                                  'summary': 'Screenshot summary', 'truncated': False}])
        self.assertNotIn('/home/private', str(payload))
        self.assertNotIn('never-read', str(payload))
        self.assertNotIn('private-hash', str(payload))

    def test_attachment_bounds_and_stale_or_mismatched_attachment_candidates(self):
        mid = self.add('current', 'Attachment message')
        with self.db.transaction() as conn:
            ids = [conn.execute('''INSERT INTO chat_attachments
                (message_id,session_id,name,mime_type,storage_path,sha256,summary_text)
                VALUES (?,?,?,?,?,?,?)''', (mid, 'current', f'C:\\private\\image-{i}.png', 'image/png',
                                           '/never-read.png', 'hash', '🙂' * 900)).lastrowid for i in range(4)]
            mismatch = conn.execute('''INSERT INTO chat_attachments
                (message_id,session_id,name,mime_type,storage_path,sha256,summary_text)
                VALUES (?,?,?,?,?,?,?)''', (mid, 'other', 'foreign.png', 'image/png', '/private.png', 'hash', 'Foreign summary')).lastrowid
        self.collection.ranked = [('attachment:999999', 0.1), (f'attachment:{mismatch}', 0.1), (f'attachment:{ids[0]}', 0.2)]
        match, = self.search()['matches']
        self.assertTrue(match['truncated'])
        self.assertEqual(len(match['attachments']), 3)
        self.assertEqual(match['attachments'][0]['name'], 'image-0.png')
        self.assertEqual(len(match['attachments'][0]['summary']), 300)
        self.assertTrue(match['attachments'][0]['truncated'])
        self.assertNotIn('Foreign summary', str(match))
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM chat_attachments WHERE id = ?', (ids[0],))
        self.assertEqual(self.search()['matches'], [])

    def test_outputs_are_bounded_utf8_and_keep_the_read_anchor(self):
        mids = [self.add('current', f'{i} ' + '🙂\n' * 2500) for i in range(9)]
        search = self.invoke('search', {'query': 'query', 'limit': 8})
        self.assertLessEqual(len(search.content.encode('utf-8')), MAX_OUTPUT_BYTES)
        self.assertTrue(json.loads(search.content)['truncated'])
        read = self.invoke('read', {'ref': f'message:{mids[4]}', 'before': 3, 'after': 3})
        payload = json.loads(read.content)
        self.assertLessEqual(len(read.content.encode('utf-8')), MAX_OUTPUT_BYTES)
        self.assertIn(f'message:{mids[4]}', [r['ref'] for r in payload['messages']])
        self.assertEqual([r['message_id'] for r in payload['messages']], sorted(r['message_id'] for r in payload['messages']))
        self.assertTrue(payload['truncated'])

    def test_native_framework_exposes_search_read_and_dispatches_a_complete_tool_conversation(self):
        mid = self.add('current', 'Earlier project decision')
        ctx = self.context()
        executor = ToolExecutor(self.registry)
        schemas = executor.get_native_tools(session_id='current', authoritative_turn=ctx.authoritative_turn)
        self.assertEqual({s['function']['name'] for s in schemas}, {'history__search', 'history__read'})
        llm = SimpleNamespace(resolve_think_value=lambda override: False,
                              chat_buffered_result=BufferedResultMock(side_effect=[
                                  {'tool_calls': [{'function': {'name': 'history__search', 'arguments': {'query': 'decision'}}}]},
                                  {'tool_calls': [{'function': {'name': 'history__read', 'arguments': {'ref': f'message:{mid}'}}}]},
                                  {'content': 'We chose the earlier project design.'},
                              ]))
        _, response = consume_generator(ResponseGenerator(llm, executor, Mock()).stream_late_routed_response(
            'current', [{'role': 'user', 'content': 'Recall our decision'}], 'Recall our decision', authoritative_turn=ctx.authoritative_turn))
        self.assertEqual(response, 'We chose the earlier project design.')
        self.assertEqual(llm.chat_buffered_result.call_count, 3)
        replay = llm.chat_buffered_result.call_args.kwargs['messages']
        self.assertEqual([m['tool_name'] for m in replay if m['role'] == 'tool'], ['history__search', 'history__read'])

    def test_automatic_memory_retrieval_remains_unchanged(self):
        self.add('previous', 'Earlier episode')
        self.add('current', 'Current session episode')
        self.ctx = self.context()
        memory = SimpleNamespace(get_relevant=lambda *args, **kwargs: ['Semantic fact'])
        retriever = MemoryRetriever(memory, self.history)
        before = retriever.retrieve('episode', 'current')
        self.search()
        after = retriever.retrieve('episode', 'current')
        self.assertEqual(before, after)
        self.assertIn('Earlier episode', after.memory_context)
        self.assertNotIn('Current session episode', after.memory_context)

    def test_factory_registers_the_existing_history_store_and_supports_disabling_tools(self):
        archive = self.add('archive', 'Explicitly trusted group decision', kind=SessionKind.MANUAL_GROUP)
        self.ctx = self.context()
        self.vectors.close = Mock()
        for enabled, allow_all in ((True, False), (True, True), (False, False), (False, True)):
            with self.subTest(enabled=enabled, allow_all=allow_all), tempfile.NamedTemporaryFile('w', suffix='.yaml') as file:
                file.write(f'assistant:\n  id: default-agent\nintegrations:\n  history:\n'
                           f'    enabled: {str(enabled).lower()}\n    allow_all_sessions: {str(allow_all).lower()}\n'
                           f'    trusted_session_ids: {"[]" if allow_all else "[archive]"}\n'
                           '  scratchpad:\n    enabled: false\n')
                file.flush()
                config = Config(file.name)
                with patch.object(orchestrator_factory, 'Database', return_value=self.db), \
                        patch.object(orchestrator_factory, 'VectorStore', return_value=self.vectors), \
                        patch.object(orchestrator_factory, 'OllamaClient'):
                    orchestrator = orchestrator_factory.build_orchestrator(config)
                self.addCleanup(orchestrator.close)
                registry = orchestrator.tool_executor.registry
                integration = registry.get_integration('history')
                self.assertEqual(integration is not None, enabled)
                if enabled:
                    self.assertIs(integration.history, orchestrator.history)
                    self.assertIs(integration.history.collection, self.collection)
                    result = registry.invoke(ToolCall(CapabilityId('history', 'read'), {'ref': f'message:{archive}'}), self.ctx)
                    self.assertEqual(result.status.value, 'success', result.content)
                schemas = orchestrator.tool_executor.get_native_tools(session_id='current', authoritative_turn=self.ctx.authoritative_turn)
                self.assertEqual(sum(s['function']['name'].startswith('history__') for s in schemas), 2 if enabled else 0)
