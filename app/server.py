"""Standalone demo HTTP API over migrated Astra services and owned state."""

import asyncio
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders

from app.config import Config
from app.core.orchestrator_factory import build_orchestrator, validate_demo_config
from app.core.session_ids import SESSION_ID_PATTERN, SESSION_ID_MAX_LENGTH
from app.demo.runtime import DemoRuntime
from app.demo.approvals import ApprovalBroker
from app.demo.speech import SpeechServices
from app.http.audio_input import is_invalid_stt_audio_error
from app.http.message_input import _validate_attachment_batch
from app.perception.attachments import ImageAttachment, attachment_from_payload
from app.tts.text import _prepare_tts_text
from app.http.knowledge import router as knowledge_router
from app.http.mcp import router as mcp_router
from app.logging import setup_logging_from_config
from app.paths import STATIC_DIR, owned_state_path

logger = logging.getLogger(__name__)
SessionId = Annotated[
    str,
    Path(min_length=1, max_length=SESSION_ID_MAX_LENGTH, pattern=SESSION_ID_PATTERN),
]


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(default="", max_length=16000)
    retry_message_id: int | None = Field(default=None, gt=0)
    attachments: list[dict] = Field(default_factory=list, max_length=8)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: int = Field(gt=0)


class SameOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                )
            await send(message)

        request = Request(scope)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            try:
                parsed = urlsplit(origin)
                valid_origin = parsed.scheme == request.url.scheme and parsed.netloc == request.url.netloc
            except ValueError:
                valid_origin = False
            if not valid_origin:
                response = JSONResponse({"detail": "Use the demo from its own origin."}, status_code=403)
                await response(scope, receive, send_with_headers)
                return
        await self.app(scope, receive, send_with_headers)


class TurnStream(StreamingResponse):
    def __init__(self, content, on_close):
        super().__init__(content, media_type="application/x-ndjson")
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.on_close()


def _check_generation(runtime, generation):
    if runtime.current is None or runtime.current.id != generation:
        raise HTTPException(409, "The workspace changed. Reload the demo.")


def _owned_task(application, coro):
    task = asyncio.create_task(coro)
    application.state.turn_tasks.add(task)

    def completed(done):
        application.state.turn_tasks.discard(done)
        # A disconnected caller may no longer await a late failed media job.
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)
    return task


def _runtime(request: Request) -> DemoRuntime:
    return request.app.state.runtime


def _acquire(runtime: DemoRuntime, generation: str | None) -> None:
    if not runtime.gate.acquire(blocking=False):
        raise HTTPException(
            409, "Demo is busy. Wait for the current operation to finish."
        )
    if runtime.current is None:
        runtime.gate.release()
        raise HTTPException(503, "Demo workspace is unavailable. Restart the server.")
    if generation != runtime.current.id:
        runtime.gate.release()
        raise HTTPException(
            409, "The workspace changed. Reload the demo to use the current examples."
        )


async def workspace(request: Request, x_demo_generation: Annotated[str, Header()]):
    runtime = _runtime(request)
    _acquire(runtime, x_demo_generation)
    try:
        yield runtime.current
    finally:
        runtime.gate.release()


async def _run_mcp(request: Request, method: str, *args):
    runtime = _runtime(request)
    _acquire(runtime, request.headers.get("x-demo-generation"))
    management = runtime.current.mcp_management
    if management is None:
        runtime.gate.release()
        raise HTTPException(503, "MCP management is unavailable.")

    async def work():
        try:
            return await asyncio.to_thread(getattr(management, method), *args)
        finally:
            runtime.gate.release()

    return await asyncio.shield(_owned_task(request.app, work()))


def create_app(
    settings: Config | None = None,
    *,
    orchestrator_builder=build_orchestrator,
    speech_builder=SpeechServices,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings or Config()
        validate_demo_config(config)
        logging_config = dict(config.logging)
        logging_config["dir"] = str(owned_state_path(logging_config["dir"]))
        setup_logging_from_config(logging_config)
        runtime = DemoRuntime(config, builder=orchestrator_builder)
        application.state.runtime = runtime
        application.state.turn_tasks = set()
        application.state.mcp_token = secrets.token_urlsafe(32)
        application.state.approvals = ApprovalBroker(
            config.autonomy["approval_timeout_s"]
        )
        application.state.speech = speech_builder(config)
        application.state.speech.start()
        try:
            generation = await asyncio.to_thread(runtime.replace)
            application.state.orchestrator = generation.orchestrator
            yield
        finally:
            tasks = list(application.state.turn_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await application.state.speech.close()
            await asyncio.to_thread(runtime.close)

    application = FastAPI(title="Astra Demo", lifespan=lifespan)
    application.state.run_mcp = _run_mcp
    application.add_middleware(SameOriginMiddleware)

    application.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR, follow_symlink=False),
        name="static",
    )
    application.include_router(knowledge_router, dependencies=[Depends(workspace)])
    application.include_router(mcp_router)

    @application.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/api/status")
    async def status(request: Request):
        runtime = _runtime(request)
        current = runtime.current
        return {
            "generation": current.id if current else None,
            "busy": runtime.gate.locked(),
            "available": current is not None,
            "model": runtime.settings.llm["model"],
            "capabilities": {
                "speech_input": runtime.settings.stt["enabled"],
                "speech_output": runtime.settings.tts["enabled"],
                "shell": runtime.settings.integrations["shell"]["enabled"],
                "history": runtime.settings.integrations["history"]["enabled"],
                "web": runtime.settings.integrations["web"]["enabled"],
                "mcp_management": current is not None and current.mcp_management is not None,
            },
            "notice": current.manifest["notice"]
            if current
            else "Workspace unavailable",
            "examples": [
                {k: item[k] for k in ("id", "title", "suggestion")}
                for item in current.manifest["sessions"]
            ]
            if current
            else [],
        }

    @application.get("/api/sessions", dependencies=[Depends(workspace)])
    async def sessions(request: Request):
        return {
            "sessions": _runtime(request).current.orchestrator.history.list_sessions()
        }

    @application.post("/api/sessions", dependencies=[Depends(workspace)])
    async def new_session(request: Request):
        import uuid

        session_id = uuid.uuid4().hex
        _runtime(request).current.orchestrator.history.ensure_session(session_id)
        return {"session_id": session_id}

    @application.get("/api/sessions/{session_id}", dependencies=[Depends(workspace)])
    async def history(session_id: SessionId, request: Request):
        orchestrator = _runtime(request).current.orchestrator
        if not orchestrator.history.session_exists(session_id):
            raise HTTPException(404, "Conversation not found")
        summary = orchestrator.summary_store.get(session_id)
        messages = orchestrator.history.get_all(session_id)
        for row in messages:
            row["attachments"] = [
                {
                    **item.to_api_payload(),
                    "url": f"/api/attachments/{_runtime(request).current.id}/{item.attachment_id}",
                }
                for item in row.get("attachments", [])
            ]
        return {
            "session_id": session_id,
            "messages": messages,
            "summary": summary[0] if summary else None,
        }

    @application.get("/api/attachments/{generation}/{attachment_id}")
    async def attachment(generation: str, attachment_id: int, request: Request):
        runtime = _runtime(request)
        _acquire(runtime, generation)
        try:
            with runtime.current.orchestrator.history.db.connection() as conn:
                row = conn.execute(
                    "SELECT storage_path, mime_type FROM chat_attachments WHERE id = ?",
                    (attachment_id,),
                ).fetchone()
            if row is None:
                raise HTTPException(404, "Image not found")
            target = owned_state_path(row["storage_path"])
            target.relative_to(runtime.current.directory)
            return Response(target.read_bytes(), media_type=row["mime_type"])
        finally:
            runtime.gate.release()

    @application.post("/api/approvals/{request_id}")
    async def approve(
        request_id: str,
        payload: ApprovalResponse,
        request: Request,
        x_demo_generation: Annotated[str, Header()],
        x_demo_client: Annotated[str, Header()],
    ):
        runtime = _runtime(request)
        _check_generation(runtime, x_demo_generation)
        if not request.app.state.approvals.resolve(
            request_id,
            generation=x_demo_generation,
            client_id=x_demo_client,
            approved=payload.approved,
        ):
            raise HTTPException(409, "Approval expired or belongs to another tab")
        return {"accepted": True}

    @application.post("/api/transcribe")
    async def transcribe(request: Request, x_demo_generation: Annotated[str, Header()]):
        runtime = _runtime(request)
        _check_generation(runtime, x_demo_generation)
        if not runtime.settings.stt["enabled"]:
            raise HTTPException(503, "Speech input is disabled")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise HTTPException(413, "Recording is too large")
            chunks.append(chunk)
        if not size:
            raise HTTPException(422, "Recording is empty")
        job = _owned_task(
            request.app,
            asyncio.to_thread(request.app.state.speech.transcribe, b"".join(chunks)),
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(job), 120)
        except Exception as exc:
            logger.exception("Speech recognition failed")
            code = 422 if is_invalid_stt_audio_error(exc) else 503
            raise HTTPException(
                code,
                "Recording could not be transcribed. Typed chat remains available.",
            )
        _check_generation(runtime, x_demo_generation)
        return {"text": result.text, "language": result.language}

    @application.post("/api/sessions/{session_id}/speech")
    async def speech(
        session_id: SessionId,
        payload: SpeechRequest,
        request: Request,
        x_demo_generation: Annotated[str, Header()],
    ):
        runtime = _runtime(request)
        _acquire(runtime, x_demo_generation)
        try:
            if not runtime.settings.tts["enabled"]:
                raise HTTPException(503, "Speech output is disabled")
            rows = runtime.current.orchestrator.history.get_all(session_id)
            row = next(
                (
                    row
                    for row in rows
                    if row["id"] == payload.message_id and row["role"] == "assistant"
                ),
                None,
            )
            if row is None:
                raise HTTPException(404, "Completed assistant message not found")
            text = _prepare_tts_text(row["content"])[:12000]
            if not text:
                raise HTTPException(422, "This answer has no speakable text")
        finally:
            runtime.gate.release()
        job = _owned_task(
            request.app, request.app.state.speech.synthesize(text, session_id)
        )
        try:
            audio = await asyncio.shield(job)
        except Exception:
            logger.exception("Speech synthesis failed")
            raise HTTPException(
                503, "Speech is unavailable. The text answer is preserved."
            )
        _check_generation(runtime, x_demo_generation)
        return Response(audio, media_type="audio/wav")

    @application.post("/api/reset")
    async def reset(request: Request, x_demo_generation: Annotated[str, Header()]):
        runtime = _runtime(request)
        _acquire(runtime, x_demo_generation)

        async def replace_workspace():
            try:
                replacement = await asyncio.to_thread(runtime.replace)
                request.app.state.orchestrator = replacement.orchestrator
                return replacement.id
            finally:
                runtime.gate.release()

        task = asyncio.create_task(replace_workspace())
        request.app.state.turn_tasks.add(task)
        task.add_done_callback(request.app.state.turn_tasks.discard)
        try:
            generation_id = await asyncio.shield(task)
        except Exception:
            logger.exception("Demo reset failed")
            raise HTTPException(
                503,
                "Reset failed. The previous workspace is retained if still available.",
            )
        return {"generation": generation_id}

    @application.post("/api/sessions/{session_id}/messages")
    async def message(
        session_id: SessionId,
        payload: MessageRequest,
        request: Request,
        x_demo_generation: Annotated[str, Header()],
        x_demo_client: Annotated[str, Header()] = "",
    ):
        runtime = _runtime(request)
        _acquire(runtime, x_demo_generation)
        current = runtime.current
        orchestrator = current.orchestrator
        try:
            if not orchestrator.history.session_exists(session_id):
                raise HTTPException(404, "Conversation not found")
            attachments = [
                attachment_from_payload(item) for item in payload.attachments
            ]
            if any(not isinstance(item, ImageAttachment) for item in attachments):
                raise HTTPException(422, "Only image attachments are supported")
            _validate_attachment_batch(attachments)
            text = payload.text.strip()
            has_images = bool(attachments)
            if payload.retry_message_id is not None:
                if text or attachments:
                    raise HTTPException(
                        422, "Retry uses the original message; omit text."
                    )
                original = orchestrator.history.get_retryable_user_message(
                    session_id, payload.retry_message_id
                )
                if original is None:
                    raise HTTPException(409, "This message cannot be retried.")
                text = original["content"]
                has_images = bool(original.get("attachments"))
            elif not text and not attachments:
                raise HTTPException(422, "Enter a message.")
        except ValueError as exc:
            runtime.gate.release()
            raise HTTPException(422, str(exc))
        except BaseException:
            runtime.gate.release()
            raise

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        disconnected = threading.Event()
        broker = request.app.state.approvals

        def approve_tool(payload):
            if disconnected.is_set():
                return False
            return asyncio.run_coroutine_threadsafe(
                broker.request(
                    payload,
                    generation=current.id,
                    session_id=session_id,
                    client_id=x_demo_client,
                    queue=queue,
                    disconnected=disconnected,
                ),
                loop,
            ).result()

        def produce():
            image_dropped = False
            flags = (
                "last_chat_dropped_current_images",
                "last_stream_dropped_current_images",
            )
            for flag in flags:
                if hasattr(orchestrator.llm, flag):
                    setattr(orchestrator.llm, flag, False)
            try:
                for event in orchestrator.handle_user_input(
                    session_id,
                    text,
                    existing_user_message_id=payload.retry_message_id,
                    attachments=attachments,
                    tool_approval_callback=approve_tool,
                ):
                    image_dropped |= any(
                        getattr(orchestrator.llm, flag, False) for flag in flags
                    )
                    if event.type in {
                        "speech",
                        "state",
                        "user_message_accepted",
                        "turn_failure",
                    }:
                        loop.call_soon_threadsafe(queue.put_nowait, event.model_dump())
                image_dropped |= any(
                    getattr(orchestrator.llm, flag, False) for flag in flags
                )
                if has_images and image_dropped:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {
                            "type": "image_unavailable",
                            "message": "The model could not process the attached image. Its answer may be based on text only.",
                        },
                    )
            except Exception:
                logger.exception("Demo turn failed")
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "error",
                        "message": "The turn failed. Reload the conversation to inspect its saved state.",
                    },
                )

        async def run_turn():
            try:
                await asyncio.to_thread(produce)
            finally:
                runtime.gate.release()
                queue.put_nowait({"type": "done", "generation": current.id})

        task = asyncio.create_task(run_turn())
        request.app.state.turn_tasks.add(task)
        task.add_done_callback(request.app.state.turn_tasks.discard)

        async def stream():
            # The owned task continues to finalization after a client disconnect.
            # Reset stays busy until it has stopped touching the old stores.
            while True:
                item = await queue.get()
                yield json.dumps(item, ensure_ascii=False) + "\n"
                if item["type"] == "done":
                    break

        def close_stream():
            disconnected.set()
            broker.disconnect(current.id, session_id, x_demo_client)

        return TurnStream(stream(), close_stream)

    return application


app = create_app()
