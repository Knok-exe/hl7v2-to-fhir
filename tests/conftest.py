import http.server
import json
import threading

import pytest

from hl7_fhir.convert import convert
from hl7_fhir.parser import parse_message

AUTH = "FAC&1.2.3&ISO"


def seg(name: str, fields: dict[int, str]) -> str:
    top = max(fields) if fields else 0
    return name + "".join("|" + fields.get(i, "") for i in range(1, top + 1))


def make(*segments: str, mtype: str = "ADT^A01", ctrl: str = "T1") -> str:
    head = f"MSH|^~\\&|APP|FAC|EHR|FAC|20240115083000||{mtype}|{ctrl}|P|2.5.1"
    return "\r".join([head, *segments]) + "\r"


def pid(**over: str) -> str:
    fields = {1: "1", 3: f"MRN1^^^{AUTH}^MR", 5: "Doe^Jane^Q", 7: "19800102", 8: "F"}
    fields.update({int(k[1:]): v for k, v in over.items()})
    return seg("PID", fields)


def pv1(**over: str) -> str:
    fields = {1: "1", 2: "I", 19: f"V1^^^{AUTH}", 44: "20240115080000"}
    fields.update({int(k[1:]): v for k, v in over.items()})
    return seg("PV1", fields)


def convert_text(text: str):
    return convert(parse_message(text))


def resources(conversion, rtype: str) -> list[dict]:
    return [
        e["resource"] for e in conversion.bundle["entry"] if e["resource"]["resourceType"] == rtype
    ]


class MockFhir:
    """A tiny local FHIR endpoint. Records requests; replies with a configurable status."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.body: dict | None = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append(
                    {
                        "path": self.path,
                        "json": json.loads(self.rfile.read(length)),
                        "ctype": self.headers.get("Content-Type"),
                    }
                )
                payload = json.dumps(outer.body or {"resourceType": "Bundle"}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/fhir"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fhir_server():
    server = MockFhir()
    yield server
    server.close()
