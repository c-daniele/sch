"""Best-effort CommandShell client presence leases."""

import threading
import uuid

from . import runtime


PRESENCE_TTL_S = 30
PRESENCE_RENEW_INTERVAL_S = 10
PRESENCE_CALL_TIMEOUT_S = 2


def new_shell_id():
    return "sch-{}".format(uuid.uuid4().hex)


def new_attachment_id():
    return "client-{}".format(uuid.uuid4().hex)


class PresenceLease:
    """Publish one local CommandShell attachment until its child exits."""

    def __init__(
        self,
        cfg,
        session_id,
        shell_id,
        workspace="",
        harness="",
        storage_backend="",
        session_epoch=0,
        attachment_id=None,
        ttl_s=PRESENCE_TTL_S,
        renew_interval_s=PRESENCE_RENEW_INTERVAL_S,
        invoke=None,
        stop_event=None,
    ):
        self.cfg = cfg
        self.session_id = session_id
        self.shell_id = shell_id
        self.workspace = workspace
        self.harness = harness
        self.storage_backend = storage_backend
        self.session_epoch = session_epoch
        self.attachment_id = attachment_id or new_attachment_id()
        self.ttl_s = ttl_s
        self.renew_interval_s = renew_interval_s
        self._invoke = invoke or (
            lambda cfg, sid, payload: runtime.invoke_best_effort(
                cfg, sid, payload, timeout=PRESENCE_CALL_TIMEOUT_S
            )
        )
        self._stop = stop_event or threading.Event()
        self._thread = None
        self._started = False
        self._closed = False

    def _publish(self, state):
        try:
            self._invoke(
                self.cfg,
                self.session_id,
                runtime.payload_presence(
                    self.shell_id,
                    self.attachment_id,
                    state,
                    self.ttl_s,
                    self.workspace,
                    self.harness,
                    self.storage_backend,
                    self.session_epoch,
                ),
            )
        except Exception:
            # Presence is an advisory compatibility feature. An old runtime or
            # a failed heartbeat must never prevent terminal access.
            pass

    def _renew(self):
        while not self._stop.wait(self.renew_interval_s):
            self._publish("attached")

    def start(self):
        if self._started or self._closed:
            return
        self._started = True
        self._publish("attached")
        try:
            self._thread = threading.Thread(
                target=self._renew,
                name="sch-command-shell-presence",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self._thread = None

    def close(self):
        if not self._started or self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._publish("detached")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
