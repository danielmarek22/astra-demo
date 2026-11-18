from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
import json
import sys
from threading import RLock
import time
import uuid

from app.tools.web.contracts import DocumentSnapshot, ReadDocument, WebReaderError


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    url: str
    title: str
    retrieved_at: str
    snapshot_id: str | None = None


@dataclass(frozen=True)
class _Entry:
    session_id: str
    value: SourceReference | DocumentSnapshot
    expires_at: float
    size: int


class SourceCache:
    def __init__(
        self, *, ttl_s=600.0, max_document_bytes=524288,
        max_session_bytes=4194304, max_total_bytes=16777216,
        max_session_entries=64, max_total_entries=256,
        max_document_lines=10000, is_session_deleted=None, clock=time.monotonic,
    ):
        if min(ttl_s, max_document_bytes, max_session_bytes, max_total_bytes,
               max_session_entries, max_total_entries, max_document_lines) <= 0:
            raise ValueError("Web cache limits must be positive.")
        self.ttl_s = ttl_s
        self.max_document_bytes = max_document_bytes
        self.max_session_bytes = max_session_bytes
        self.max_total_bytes = max_total_bytes
        self.max_session_entries = max_session_entries
        self.max_total_entries = max_total_entries
        self.max_document_lines = max_document_lines
        self.is_session_deleted = is_session_deleted
        self.clock = clock
        self._entries = OrderedDict()
        self._expired = OrderedDict()
        self._lock = RLock()

    def _check_session(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise WebReaderError("invalid_session", "An authoritative conversation is required.")
        if self.is_session_deleted is not None and self.is_session_deleted(session_id):
            raise WebReaderError("session_deleted", "This conversation was deleted; its web evidence is unavailable.")

    @staticmethod
    def _size(value):
        if isinstance(value, SourceReference):
            value = replace(value, snapshot_id="page_" + "0" * 32)
        encoded = len(json.dumps(asdict(value), ensure_ascii=False).encode("utf-8"))
        if isinstance(value, DocumentSnapshot):
            encoded += sys.getsizeof(value.lines) + sum(sys.getsizeof(line) for line in value.lines)
        return encoded + 256

    def _remove(self, source_id):
        entry = self._entries.pop(source_id)
        self._expired[source_id] = (entry.session_id, self.clock() + self.ttl_s)
        while len(self._expired) > self.max_total_entries:
            self._expired.popitem(last=False)

    def _prune(self):
        now = self.clock()
        for source_id, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                self._remove(source_id)
        for source_id, (_owner, expires) in tuple(self._expired.items()):
            if expires <= now:
                del self._expired[source_id]

    def _store(self, session_id, source_id, value):
        self._check_session(session_id)
        self._prune()
        size = self._size(value)
        if size > min(self.max_session_bytes, self.max_total_bytes):
            raise WebReaderError("cache_limit", "This source cannot fit in the configured web cache.")
        if source_id in self._entries:
            previous = self._entries[source_id]
            if previous.session_id != session_id:
                raise WebReaderError("forbidden_reference", "This web reference is not owned by this conversation.")
            del self._entries[source_id]
        while True:
            owned = [(key, entry) for key, entry in self._entries.items() if entry.session_id == session_id]
            if len(owned) < self.max_session_entries and sum(entry.size for _key, entry in owned) + size <= self.max_session_bytes:
                break
            self._remove(owned[0][0])
        while len(self._entries) >= self.max_total_entries or sum(entry.size for entry in self._entries.values()) + size > self.max_total_bytes:
            self._remove(next(iter(self._entries)))
        self._entries[source_id] = _Entry(session_id, value, self.clock() + self.ttl_s, size)
        self._expired.pop(source_id, None)

    def retain_search(self, session_id, records):
        with self._lock:
            self._check_session(session_id)
            for record in records:
                self._store(session_id, record["source_id"], SourceReference(
                    record["source_id"], record["url"], record["title"], record["retrieved_at"],
                ))

    def get(self, session_id, source_id):
        with self._lock:
            self._check_session(session_id)
            self._prune()
            entry = self._entries.get(source_id)
            if entry is None:
                expired = self._expired.get(source_id)
                if expired is not None and expired[0] != session_id:
                    raise WebReaderError("forbidden_reference", "This web reference is not owned by this conversation.")
                category = "expired_reference" if expired else "unknown_reference"
                raise WebReaderError(category, "This web reference expired or is unknown. Open its URL again.")
            if entry.session_id != session_id:
                raise WebReaderError("forbidden_reference", "This web reference is not owned by this conversation.")
            self._entries.move_to_end(source_id)
            return entry.value

    def find_url(self, session_id, url):
        with self._lock:
            self._check_session(session_id)
            self._prune()
            for source_id, entry in reversed(tuple(self._entries.items())):
                value = entry.value
                if entry.session_id == session_id and isinstance(value, DocumentSnapshot) and url in {value.requested_url, value.final_url}:
                    self._entries.move_to_end(source_id)
                    return value
            return None

    def bind_snapshot(self, session_id, source_id, snapshot_id):
        with self._lock:
            source = self.get(session_id, source_id)
            snapshot = self.get(session_id, snapshot_id)
            if not isinstance(source, SourceReference) or not isinstance(snapshot, DocumentSnapshot):
                raise WebReaderError("invalid_reference", "Only a search source can reference a page snapshot.")
            entry = self._entries[source_id]
            updated = replace(source, snapshot_id=snapshot_id)
            self._entries[source_id] = replace(entry, value=updated, size=self._size(updated))

    def add_document(self, session_id, document: ReadDocument, source_id=None):
        with self._lock:
            self._check_session(session_id)
            if source_id is not None and not isinstance(self.get(session_id, source_id), SourceReference):
                raise WebReaderError("invalid_reference", "Only a search source can own a new page snapshot.")
            raw = document.markdown.encode("utf-8")
            clipped = raw[:self.max_document_bytes].decode("utf-8", errors="ignore")
            lines = tuple(clipped.splitlines())
            truncated = document.truncated or len(raw) > self.max_document_bytes or len(lines) > self.max_document_lines
            lines = lines[:self.max_document_lines]
            if not any(line.strip() for line in lines):
                raise WebReaderError("empty_content", "The page contained no usable extracted text.")
            snapshot_id = f"page_{uuid.uuid4().hex}"
            snapshot = DocumentSnapshot(
                snapshot_id, source_id or snapshot_id, document.requested_url,
                document.final_url, document.title, document.retrieved_at, lines,
                document.links, truncated, document.warnings,
            )
            self._store(session_id, snapshot_id, snapshot)
            source = self._entries.get(source_id)
            if source is not None and source.session_id == session_id and isinstance(source.value, SourceReference):
                updated = replace(source.value, snapshot_id=snapshot_id)
                self._entries[source_id] = replace(source, value=updated, size=self._size(updated))
            return snapshot

    def delete_session(self, session_id):
        with self._lock:
            for source_id, entry in tuple(self._entries.items()):
                if entry.session_id == session_id:
                    del self._entries[source_id]
            for source_id, (owner, _expires) in tuple(self._expired.items()):
                if owner == session_id:
                    del self._expired[source_id]

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._expired.clear()
