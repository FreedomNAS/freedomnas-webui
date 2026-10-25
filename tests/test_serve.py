"""One port for https:// and for plain http://, which is sent to https:// on the same host, port and path."""

import asyncio
import functools
import socket
import ssl
import subprocess
import threading

import pytest
import uvicorn

from api import serve


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    client = scope["client"][0]
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": f"hello {client}".encode()})


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=nas",
                    "-addext", "subjectAltName=DNS:nas,IP:127.0.0.1"], check=True, capture_output=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, loop="asyncio", log_level="warning",
                            ssl_certfile=str(cert), ssl_keyfile=str(key))
    server = uvicorn.Server(config)
    sock = config.bind_socket()
    port = sock.getsockname()[1]

    async def run():
        loop = asyncio.get_running_loop()
        loop.create_server = functools.partial(serve._split_create_server, loop)
        await server.serve(sockets=[sock])

    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        threading.Event().wait(0.1)
    yield port, cert
    server.should_exit = True
    thread.join(5)


def raw(port, data):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(data)
        out = b""
        while chunk := conn.recv(4096):
            out += chunk
    return out.decode("latin-1")


def test_https_reaches_the_app_with_the_clients_own_address(running):
    port, cert = running
    context = ssl.create_default_context(cafile=str(cert))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        with context.wrap_socket(conn, server_hostname="nas") as tls:
            tls.sendall(b"GET / HTTP/1.1\r\nHost: nas\r\nConnection: close\r\n\r\n")
            out = b""
            while chunk := tls.recv(4096):
                out += chunk
    assert out.startswith(b"HTTP/1.1 200") and b"hello 127.0.0.1" in out


def test_plain_http_is_sent_to_https_on_the_same_host_port_and_path(running):
    port, _ = running
    out = raw(port, b"GET /storage/?x=1 HTTP/1.1\r\nHost: freedomnas:8080\r\n\r\n")
    assert out.startswith("HTTP/1.1 308")
    assert f"Location: https://freedomnas:{port}/storage/?x=1\r\n" in out
    assert f'href="https://freedomnas:{port}/storage/?x=1"' in out


def test_a_strange_host_or_path_falls_back_to_the_address_and_root(running):
    port, _ = running
    out = raw(port, b"GET <script> HTTP/1.1\r\nHost: evil\"host\r\n\r\n")
    assert f"Location: https://127.0.0.1:{port}/\r\n" in out


def test_ipv6_host_keeps_its_brackets():
    assert serve._target(b"GET / HTTP/1.1\r\nHost: [fd00::5]:8080\r\n\r\n", ("::", 8080)) == "https://[fd00::5]:8080/"
