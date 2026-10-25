"""
The WebUI's listener: one port for https:// and for the plain http:// a
browser tries first.

Every connection is looked at before anything reads from it. One that starts
with a TLS record goes to uvicorn as it is, the accepted socket itself, so the
app sees the client's own address; a plain HTTP request gets a 308 to
https:// on the same host, port and path, with a short page linking there for
clients that do not follow redirects. Without a certificate uvicorn serves
plain HTTP as it always did.

    python3 -m api.serve --host 0.0.0.0 --port 8080 [--ssl-certfile C --ssl-keyfile K]
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import html
import re
import socket
from typing import Optional

import uvicorn

TLS_HANDSHAKE = 0x16
PEEK_TIMEOUT = 10.0
REQUEST_LIMIT = 8192
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]")
_PATH = re.compile(r"/[\x21-\x7e]{0,2047}")

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>FreedomNAS: use HTTPS</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}}main{{max-width:32rem;padding:2rem}}
a{{color:#60a5fa}}</style></head>
<body><main><h1>FreedomNAS answers over HTTPS</h1>
<p>This address was opened with <code>http://</code>. The WebUI speaks only HTTPS, so the
password never travels in clear.</p>
<p><a href="{url}">Continue to {label}</a></p></main></body></html>
"""


def _target(request: bytes, local: tuple) -> str:
    """https:// on the host the client asked for (or the address it reached), same port and path."""
    lines = request.split(b"\r\n")
    words = lines[0].decode("latin-1").split(" ")
    path = words[1] if len(words) >= 2 and _PATH.fullmatch(words[1]) else "/"
    host: Optional[str] = None
    for line in lines[1:]:
        name, _, value = line.decode("latin-1").partition(":")
        if name.strip().lower() == "host":
            value = value.strip()
            name_part = value[:value.find("]") + 1] if value.startswith("[") else value.split(":", 1)[0]
            if _HOST.fullmatch(name_part):
                host = name_part
            break
    if host is None:
        address = local[0]
        host = f"[{address}]" if ":" in address else address
    return f"https://{host}:{local[1]}{path}"


class SplitServer:
    """What uvicorn gets back from create_server: accepts, then routes each connection."""

    def __init__(self, loop, protocol_factory, sock: socket.socket, context, backlog: int = 2048):
        self.loop = loop
        self.factory = protocol_factory
        self.sock = sock
        self.context = context
        self.sockets = [sock]
        self.sock.setblocking(False)
        self.sock.listen(backlog)
        self.connections: set = set()
        self.task = loop.create_task(self._accept())

    async def _accept(self) -> None:
        while True:
            try:
                conn, _ = await self.loop.sock_accept(self.sock)
            except OSError:
                return
            task = self.loop.create_task(self._route(conn))
            self.connections.add(task)
            task.add_done_callback(self.connections.discard)

    async def _readable(self, conn: socket.socket) -> None:
        done = self.loop.create_future()
        self.loop.add_reader(conn.fileno(), lambda: done.done() or done.set_result(None))
        try:
            await done
        finally:
            self.loop.remove_reader(conn.fileno())

    async def _route(self, conn: socket.socket) -> None:
        conn.setblocking(False)
        try:
            await asyncio.wait_for(self._readable(conn), PEEK_TIMEOUT)
            first = conn.recv(1, socket.MSG_PEEK)
        except (asyncio.TimeoutError, OSError):
            conn.close()
            return
        if not first:
            conn.close()
            return
        if first[0] == TLS_HANDSHAKE:
            try:
                await self.loop.connect_accepted_socket(self.factory, conn, ssl=self.context)
            except (OSError, ConnectionError, asyncio.TimeoutError):
                conn.close()
            return
        await self._redirect(conn)

    async def _redirect(self, conn: socket.socket) -> None:
        request = b""
        try:
            while b"\r\n\r\n" not in request and len(request) < REQUEST_LIMIT:
                chunk = await asyncio.wait_for(self.loop.sock_recv(conn, 2048), PEEK_TIMEOUT)
                if not chunk:
                    break
                request += chunk
            url = _target(request, conn.getsockname()[:2])
            page = PAGE.format(url=html.escape(url, quote=True), label=html.escape(url)).encode()
            head = (f"HTTP/1.1 308 Permanent Redirect\r\nLocation: {url}\r\n"
                    f"Content-Type: text/html; charset=utf-8\r\nContent-Length: {len(page)}\r\n"
                    "Connection: close\r\n\r\n").encode("latin-1")
            await self.loop.sock_sendall(conn, head + page)
        except (asyncio.TimeoutError, OSError):
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.task.cancel()

    async def wait_closed(self) -> None:
        for task in [self.task, *self.connections]:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def is_serving(self) -> bool:
        return not self.task.done()


async def _split_create_server(loop, protocol_factory, *args, sock=None, ssl=None, backlog=2048, **kwargs):
    return SplitServer(loop, protocol_factory, sock, ssl, backlog)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="api.serve")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    args = parser.parse_args(argv)
    if not (args.ssl_certfile and args.ssl_keyfile):
        uvicorn.run("api.main:app", host=args.host, port=args.port)
        return
    config = uvicorn.Config("api.main:app", host=args.host, port=args.port, loop="asyncio",
                            ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile)
    server = uvicorn.Server(config)
    sock = config.bind_socket()

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        loop.create_server = functools.partial(_split_create_server, loop)
        await server.serve(sockets=[sock])

    asyncio.run(serve())


if __name__ == "__main__":
    main()
