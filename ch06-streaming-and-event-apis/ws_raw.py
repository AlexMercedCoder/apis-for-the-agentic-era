"""A WebSocket client written against a bare socket, to show the bytes.

    uv run python ws_raw.py [--port 8006]

Sends the HTTP upgrade by hand (with the sample key from RFC 6455, so the
expected Sec-WebSocket-Accept is the RFC's own example), then a masked
text frame asking to watch ord-5003. Prints each frame's header bits and
payload until order.shipped arrives, then closes with code 1000.
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEY = "dGhlIHNhbXBsZSBub25jZQ=="
OPCODES = {0x1: "text", 0x2: "binary", 0x8: "close", 0x9: "ping",
           0xA: "pong"}


def show(direction: str, fin: int, opcode: int, masked: int,
         payload: bytes) -> None:
    name = OPCODES.get(opcode, hex(opcode))
    print(f"{direction}  FIN={fin} opcode=0x{opcode:x} ({name}) "
          f"MASK={masked} len={len(payload)}")
    if opcode == 0x8:
        code = struct.unpack("!H", payload[:2])[0] if payload else None
        print(f"    close code {code}")
    else:
        text = payload.decode()
        while len(text) > 64:
            cut = text.rindex(",", 0, 64) + 1
            print(f"    {text[:cut]}")
            text = text[cut:]
        print(f"    {text}")


def send(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    head = bytes([0x80 | opcode, 0x80 | len(payload)])
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(head + mask + masked)
    show("client -> server", 1, opcode, 1, payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def recv(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = recv_exact(sock, 2)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    payload = recv_exact(sock, length)
    show("server -> client", b1 >> 7, b1 & 0x0F, b2 >> 7, payload)
    return b1 & 0x0F, payload


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8006)
    port = p.parse_args().port
    sock = socket.create_connection(("127.0.0.1", port))
    request = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {KEY}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(request.encode())
    print(request.replace("\r\n", "\n"), end="")
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(1)
    for line in response.decode().split("\r\n"):
        if not line.lower().startswith("date:"):
            print(line)
    accept = base64.b64encode(
        hashlib.sha1((KEY + GUID).encode()).digest()).decode()
    print(f"expected Sec-WebSocket-Accept: {accept}\n")

    watch = {"type": "watch", "order_id": "ord-5003"}
    send(sock, 0x1, json.dumps(watch, separators=(",", ":")).encode())
    while True:
        opcode, payload = recv(sock)
        if opcode == 0x1 and json.loads(payload)["type"] == "order.shipped":
            break
    send(sock, 0x8, struct.pack("!H", 1000))
    recv(sock)
    sock.close()


if __name__ == "__main__":
    main()
