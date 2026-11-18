"""One configured server generation, retained by admitted registry catalogs."""

import threading


class MCPServerGeneration:
    def __init__(self, runtime, server_id, *, owns_runtime=False):
        self.runtime = runtime
        self.server_id = server_id
        self.owns_runtime = owns_runtime
        self._lock = threading.Lock()
        self._users = 0
        self._retired = False
        self._closing = False
        self.closed = threading.Event()
        self.cleanup_failed = False
        self.catalog_loaded = False

    def retain(self):
        with self._lock:
            if self._closing:
                raise RuntimeError("MCP generation is already closing")
            self._users += 1

    def release(self):
        with self._lock:
            self._users -= 1
            self._schedule_cleanup()

    def retire(self):
        with self._lock:
            self._retired = True
            self._schedule_cleanup()

    def _schedule_cleanup(self):
        if self._retired and not self._users and not self._closing:
            self._closing = True
            # Release can run during GC or on the web loop. Never block that caller.
            threading.Thread(target=self._cleanup, name="astra-mcp-retire", daemon=True).start()

    def _cleanup(self):
        try:
            if self.owns_runtime:
                self.runtime.close()
            else:
                # The initial runtime can own several servers; close it at app shutdown.
                if self.runtime.server_settings(self.server_id)["enabled"]:
                    self.runtime.disconnect(self.server_id)
        except Exception as error:  # noqa: BLE001 -- cleanup must not leak untrusted exceptions from a retirement thread
            # Only safe, application-owned diagnostics leave this boundary.
            self.cleanup_failed = getattr(error, "category", None) not in {"disabled", "closed"}
        finally:
            self.closed.set()
