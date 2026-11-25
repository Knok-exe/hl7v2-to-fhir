"""MLLP framing (0x0B <message> 0x1C 0x0D), a threaded listener, ACK building and a sender."""

from __future__ import annotations

import re
import socket
import socketserver
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

SB, EB, CR = b"\x0b", b"\x1c", b"\r"
MAX_FRAME = 10 * 1024 * 1024


def wrap(data: bytes) -> bytes:
    return SB + data + EB + CR


def extract_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Return (frame, remaining). frame is None until a complete frame is buffered.
    Bytes before a start block are discarded; a CR after the end block is consumed."""
    start = buffer.find(SB)
    if start < 0:
        return None, b""  # nothing useful; drop line noise
    end = buffer.find(EB, start + 1)
    if end < 0:
        return None, buffer[start:]
    rest = buffer[end + 1 :]
    if rest.startswith(CR):
        rest = rest[1:]
    return buffer[start + 1 : end], rest


def decode(data: bytes) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _escape(
    text: str, field: str = "|", comp: str = "^", rep: str = "~", esc: str = "\\", sub: str = "&"
) -> str:
    table = {
        esc: f"{esc}E{esc}",
        field: f"{esc}F{esc}",
        comp: f"{esc}S{esc}",
        sub: f"{esc}T{esc}",
        rep: f"{esc}R{esc}",
    }
    text = re.sub(r"[\r\n]+", " ", text)
    return "".join(table.get(ch, ch) for ch in text)


def build_ack(raw: str, code: str, text: str = "") -> str:
    """ACK for `raw` (which may be unparseable). code: AA accepted, AE error, AR rejected."""
    flat = raw.replace("\x0b", "").replace("\x1c", "").replace("\r\n", "\r").replace("\n", "\r")
    first = flat.split("\r", 1)[0]
    sep = first[3] if first.startswith("MSH") and len(first) > 3 else "|"
    parts = first.split(sep) if first.startswith("MSH") else []

    def field(n: int) -> str:  # MSH-n, n >= 3
        return parts[n - 1] if len(parts) >= n else ""

    sending_app, sending_fac = field(3).split("^")[0], field(4).split("^")[0]
    control = field(10)
    processing_id = field(11).split("^")[0] or "P"  # echo P/T/D so test traffic stays test
    version = field(12).split("^")[0] or "2.5.1"
    mtype = field(9).split("^")
    event = mtype[1] if len(mtype) > 1 else ""
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    msh = [
        "MSH",
        "^~\\&",
        "HL7FHIR",
        "CONVERTER",
        sending_app,
        sending_fac,
        now,
        "",
        f"ACK^{event}" if event else "ACK",
        f"ACK{uuid.uuid4().hex[:17]}",  # MSH-10 must be unique; 20 chars max in v2.5.1
        processing_id,
        version,
    ]
    msa = ["MSA", code, control or "UNKNOWN"]
    if text:
        msa.append(_escape(text))
    return "|".join(msh) + "\r" + "|".join(msa) + "\r"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        buffer = b""
        self.request.settimeout(self.server.idle_timeout)  # type: ignore[attr-defined]
        while True:
            try:
                chunk = self.request.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                frame, buffer = extract_frame(buffer)
                if frame is None:
                    break
                ack = self.server.process(decode(frame))  # type: ignore[attr-defined]
                self.request.sendall(wrap(ack.encode("utf-8")))
            if len(buffer) > MAX_FRAME:
                return


class MllpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], process: Callable[[str], str], idle_timeout: float = 60.0
    ):
        super().__init__(address, _Handler)
        self.process = process
        self.idle_timeout = idle_timeout


def send_message(host: str, port: int, text: str, timeout: float = 10.0) -> str:
    """Send one message over MLLP and return the ACK text."""
    payload = text.replace("\r\n", "\r").replace("\n", "\r")
    if not payload.endswith("\r"):
        payload += "\r"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(wrap(payload.encode("utf-8")))
        buffer = b""
        while True:
            frame, buffer = extract_frame(buffer)
            if frame is not None:
                return decode(frame)
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed before an ACK was received")
            buffer += chunk
