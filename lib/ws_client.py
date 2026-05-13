"""Minimal WebSocket client using only the Python standard library.

Supports wss:// (TLS) and ws://, text frames, and ping/pong.
Designed to run in a background thread for the beacon dashboard.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from typing import Callable
from urllib.parse import urlparse


class WebSocketClient:
    """Simple WebSocket client (RFC 6455) for receiving text messages."""

    def __init__(self, url: str, on_message: Callable[[str], None],
                 on_error: Callable[[Exception], None] | None = None):
        self._url = url
        self._on_message = on_message
        self._on_error = on_error or (lambda e: None)
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def connect(self):
        """Connect and start receiving messages in a background thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self):
        """Signal the background thread to stop and close the socket."""
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def send_text(self, text: str):
        """Send a text frame."""
        if self._sock:
            try:
                self._send_frame(0x1, text.encode("utf-8"))
            except OSError:
                pass

    def _run(self):
        """Background thread: connect, handshake, receive loop."""
        try:
            self._do_connect()
            self._receive_loop()
        except Exception as e:
            if not self._stop.is_set():
                self._on_error(e)
        finally:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _do_connect(self):
        """TCP connect + WebSocket handshake."""
        parsed = urlparse(self._url)
        use_tls = parsed.scheme == "wss"
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if use_tls else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        sock = socket.create_connection((host, port), timeout=10)
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(5)

        # WebSocket handshake
        ws_key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode())

        # Read response headers
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            response += chunk

        status_line = response.split(b"\r\n")[0].decode()
        if "101" not in status_line:
            raise ConnectionError(f"WebSocket handshake failed: {status_line}")

        # Verify Sec-WebSocket-Accept
        expected_accept = base64.b64encode(
            hashlib.sha1(
                (ws_key + "258EAFA5-E914-47DA-95CA-5AB5AA29ABE5").encode()
            ).digest()
        ).decode()
        if expected_accept.encode() not in response:
            raise ConnectionError("Invalid Sec-WebSocket-Accept")

        self._sock = sock

    def _receive_loop(self):
        """Read WebSocket frames until stopped."""
        buf = b""
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                # Send ping to keep alive
                try:
                    self.send_text("ping")
                except OSError:
                    break
                continue
            except OSError:
                break

            if not data:
                break

            buf += data

            # Process complete frames
            while len(buf) >= 2:
                frame, consumed = self._parse_frame(buf)
                if frame is None:
                    break
                buf = buf[consumed:]

                opcode, payload = frame
                if opcode == 0x1:  # Text
                    self._on_message(payload.decode("utf-8", errors="replace"))
                elif opcode == 0x8:  # Close
                    return
                elif opcode == 0x9:  # Ping
                    self._send_frame(0xA, payload)  # Pong

    @staticmethod
    def _parse_frame(buf: bytes):
        """Parse a WebSocket frame. Returns ((opcode, payload), bytes_consumed) or (None, 0)."""
        if len(buf) < 2:
            return None, 0

        opcode = buf[0] & 0x0F
        masked = (buf[1] & 0x80) != 0
        length = buf[1] & 0x7F
        offset = 2

        if length == 126:
            if len(buf) < 4:
                return None, 0
            length = struct.unpack("!H", buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None, 0
            length = struct.unpack("!Q", buf[2:10])[0]
            offset = 10

        if masked:
            offset += 4

        if len(buf) < offset + length:
            return None, 0

        payload = buf[offset:offset + length]
        if masked:
            mask = buf[offset - 4:offset]
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        return (opcode, payload), offset + length

    def _send_frame(self, opcode: int, payload: bytes):
        """Send a masked WebSocket frame (clients must mask)."""
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode

        length = len(payload)
        if length < 126:
            header.append(0x80 | length)  # MASK bit + length
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)

        header += mask_key
        self._sock.sendall(bytes(header) + masked)
