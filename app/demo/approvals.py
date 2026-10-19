"""HTTP-stream transport for the base project's approval callback contract."""

import asyncio
import uuid
from dataclasses import dataclass


@dataclass
class PendingApproval:
    generation: str
    session_id: str
    client_id: str
    future: asyncio.Future


class ApprovalBroker:
    def __init__(self, timeout_s=300):
        self.timeout_s = timeout_s
        self.pending: dict[str, PendingApproval] = {}

    async def request(
        self, payload, *, generation, session_id, client_id, queue, disconnected
    ):
        if not client_id or disconnected.is_set():
            return False
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = PendingApproval(
            generation, session_id, client_id, future
        )
        await queue.put(
            {**payload, "type": "tool_approval_request", "request_id": request_id}
        )
        try:
            return await asyncio.wait_for(future, self.timeout_s)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(request_id, None)
            await queue.put(
                {"type": "tool_approval_resolved", "request_id": request_id}
            )

    def resolve(self, request_id, *, generation, client_id, approved):
        pending = self.pending.get(request_id)
        if (
            pending is None
            or pending.generation != generation
            or pending.client_id != client_id
            or pending.future.done()
        ):
            return False
        pending.future.set_result(approved)
        return True

    def disconnect(self, generation, session_id, client_id):
        for pending in list(self.pending.values()):
            if (pending.generation, pending.session_id, pending.client_id) == (
                generation,
                session_id,
                client_id,
            ) and not pending.future.done():
                pending.future.set_result(False)
