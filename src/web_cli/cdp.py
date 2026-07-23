"""Minimal async CDP client: one websocket, JSON-RPC request/response + event stream.

A single reader task pulls every frame off the socket. Frames carrying an `id`
resolve the matching pending Future (raising CDPError on protocol errors); frames
without an `id` are events, pushed onto a queue that `events()` drains. This is
exactly the correlation model `listen` needs — fire `Network.getResponseBody`
while a stream of events keeps arriving.
"""

import asyncio
import contextlib
import json

import websockets

from .discovery import MODE_BROWSER_WS
from .output import die


class CDPError(Exception):
    """A CDP command returned an `error` object."""

    def __init__(self, err):
        self.code = err.get("code")
        self.message = err.get("message", "CDP error")
        super().__init__(f"{self.message} (code {self.code})")


class CDPSession:
    def __init__(self, ws):
        self._ws = ws
        self._id = 0
        self._pending = {}
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, ws_url):
        try:
            # max_size=None: CDP response bodies can exceed the 1MB default frame cap.
            ws = await websockets.connect(ws_url, max_size=None)
        except (OSError, websockets.exceptions.WebSocketException) as e:
            die(
                "Could not open the CDP websocket. Is the debug Chrome running? "
                f"({e})",
                code=3,
            )
        session = cls(ws)
        try:
            yield session
        finally:
            await session.close()

    async def _read_loop(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CDPError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    self._events.put_nowait(msg)
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass

    async def send(self, method, params=None, session_id=None, timeout=30):
        self._id += 1
        mid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        await self._ws.send(json.dumps(payload))
        try:
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            self._pending.pop(mid, None)
            die(f"CDP command '{method}' timed out after {timeout}s.", code=1)

    async def events(self):
        """Yield CDP events (messages without an id) as they arrive."""
        while True:
            yield await self._events.get()

    async def close(self):
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()


@contextlib.asynccontextmanager
async def open_target(conn, target):
    """Open a session scoped to `target`, yielding (session, session_id).

    HTTP mode connects straight to the target's own websocket (session_id=None).
    Browser-ws mode attaches through the browser socket and returns a sessionId
    that every send() threads through.
    """
    if conn.mode == MODE_BROWSER_WS:
        async with CDPSession.connect(conn.browser_ws) as session:
            res = await session.send(
                "Target.attachToTarget",
                {"targetId": target["id"], "flatten": True},
            )
            yield session, res["sessionId"]
    else:
        async with CDPSession.connect(target["webSocketDebuggerUrl"]) as session:
            yield session, None
