import json
import threading

import pytest
import requests

from hl7_fhir.errors import PostError
from hl7_fhir.mllp import MllpServer, build_ack, decode, extract_frame, send_message, wrap
from hl7_fhir.pipeline import Pipeline, post_bundle

from .conftest import make, pid, pv1

GOOD = make(pid(), pv1(), ctrl="G1")


@pytest.fixture
def pipe(tmp_path):
    return Pipeline(tmp_path / "out", tmp_path / "dl", post_backoff=0)


def dead(tmp_path):
    return sorted((tmp_path / "dl").glob("*.json"))


def test_good_message_is_written_and_nothing_dead_lettered(pipe, tmp_path):
    out = pipe.process(GOOD)
    assert out.ok and out.ack_code == "AA" and out.bundle_path.name == "G1_ADT-A01.json"
    assert json.loads(out.bundle_path.read_text())["type"] == "transaction"
    assert out.counts == {"Patient": 1, "Encounter": 1}
    assert dead(tmp_path) == []


@pytest.mark.parametrize(
    "raw, stage, ack, fragment",
    [
        ("not hl7 at all", "parse", "AR", "MSH"),
        (make(pid(), pv1(), mtype="ADT^A04", ctrl="U1"), "convert", "AR", "not supported"),
        (make(pv1(), ctrl="C1"), "convert", "AE", "PID"),
        (make(pid(P7="1980-01-02"), pv1(), ctrl="D1"), "convert", "AE", "timestamp"),
    ],
)
def test_failures_go_to_the_dead_letter_queue_with_a_clear_reason(
    pipe, tmp_path, raw, stage, ack, fragment
):
    out = pipe.process(raw)
    assert not out.ok and out.stage == stage and out.ack_code == ack
    assert fragment.lower() in out.error.lower()
    files = dead(tmp_path)
    assert len(files) == 1
    meta = json.loads(files[0].read_text())
    assert meta["stage"] == stage and meta["ack_code"] == ack and meta["error"] == out.error
    assert out.dead_letter_path.read_bytes() == raw.encode()  # the original, byte for byte
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.json"))


def test_validation_failure_is_dead_lettered(pipe, tmp_path, monkeypatch):
    from hl7_fhir import pipeline
    from hl7_fhir.errors import ValidationError

    def boom(_):
        raise ValidationError("Encounter: bad")

    monkeypatch.setattr(pipeline, "validate_or_raise", boom)
    out = pipe.process(GOOD)
    assert out.stage == "validate" and out.ack_code == "AE" and len(dead(tmp_path)) == 1


def test_an_unexpected_bug_is_dead_lettered_not_raised(pipe, tmp_path, monkeypatch):
    from hl7_fhir import pipeline

    def boom(*a, **k):
        raise KeyError("oops")

    monkeypatch.setattr(pipeline, "convert", boom)
    out = pipe.process(GOOD)
    assert out.stage == "internal" and "converter bug" in out.error
    assert out.dead_letter_path.read_bytes() == GOOD.encode()


def test_dead_letter_names_never_collide_for_the_same_message(pipe, tmp_path):
    for _ in range(3):
        pipe.process("garbage")
    assert len(dead(tmp_path)) == 3


# ---- HAPI / FHIR server posting -------------------------------------------------


def test_post_success_sends_the_bundle(fhir_server):
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    post_bundle(fhir_server.url, bundle, retries=0)
    assert fhir_server.requests[0]["json"] == bundle
    assert fhir_server.requests[0]["ctype"] == "application/fhir+json"


def test_pipeline_posts_after_writing(fhir_server, tmp_path):
    p = Pipeline(tmp_path / "o", tmp_path / "d", post_url=fhir_server.url)
    out = p.process(GOOD)
    assert out.ok and out.posted and len(fhir_server.requests) == 1


def test_4xx_surfaces_the_operation_outcome_and_is_not_retried(fhir_server, tmp_path):
    fhir_server.status = 400
    fhir_server.body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "diagnostics": "Unknown code 'Z'"}],
    }
    p = Pipeline(tmp_path / "o", tmp_path / "d", post_url=fhir_server.url, post_backoff=0)
    out = p.process(GOOD)
    assert not out.ok and out.stage == "post" and out.ack_code == "AE"
    assert "HTTP 400" in out.error and "Unknown code 'Z'" in out.error
    assert len(fhir_server.requests) == 1
    assert out.dead_letter_path.read_bytes() == GOOD.encode()
    assert out.bundle_path.exists()  # the converted bundle is kept for inspection


def test_5xx_is_retried_then_fails(fhir_server):
    fhir_server.status = 503
    with pytest.raises(PostError, match="HTTP 503"):
        post_bundle(fhir_server.url, {}, retries=2, backoff=0)
    assert len(fhir_server.requests) == 3


def test_5xx_then_success(fhir_server, monkeypatch):
    codes = iter([500, 200])
    original = requests.post

    def flaky(*a, **k):
        fhir_server.status = next(codes)
        return original(*a, **k)

    monkeypatch.setattr(requests, "post", flaky)
    post_bundle(fhir_server.url, {}, retries=2, backoff=0)
    assert len(fhir_server.requests) == 2


def test_unreachable_server_is_a_clean_post_error(tmp_path):
    p = Pipeline(
        tmp_path / "o",
        tmp_path / "d",
        post_url="http://127.0.0.1:9/fhir",
        post_retries=1,
        post_backoff=0,
    )
    out = p.process(GOOD)
    assert out.stage == "post" and "could not reach" in out.error


# ---- MLLP -----------------------------------------------------------------------


def test_framing_helpers():
    assert wrap(b"MSH|") == b"\x0bMSH|\x1c\r"
    frame, rest = extract_frame(b"noise\x0bONE\x1c\r\x0bTWO")
    assert frame == b"ONE" and rest == b"\x0bTWO"
    assert extract_frame(b"\x0bpartial") == (None, b"\x0bpartial")
    assert decode("é".encode("cp1252")) == "é" and decode("é".encode()) == "é"


def test_ack_codes_reference_the_original_message():
    ack = build_ack(GOOD, "AA").split("\r")
    assert ack[0].startswith("MSH|^~\\&|HL7FHIR|CONVERTER|APP|FAC|") and "ACK^A01" in ack[0]
    assert ack[1] == "MSA|AA|G1"
    err = build_ack(GOOD, "AE", "convert: bad | pipe ^ caret").split("\r")[1]
    assert err.startswith("MSA|AE|G1|") and "|" not in err.split("|", 3)[3]


def test_ack_for_unparseable_input_still_works():
    ack = build_ack("total garbage", "AR", "parse: no MSH")
    assert "MSA|AR|" in ack and ack.startswith("MSH|")


@pytest.fixture
def listener(pipe):
    server = MllpServer(("127.0.0.1", 0), pipe.handle_mllp, idle_timeout=5)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_mllp_round_trip_over_a_real_socket(listener, tmp_path):
    ack = send_message("127.0.0.1", listener, GOOD)
    assert "MSA|AA|G1" in ack
    assert (tmp_path / "out" / "G1_ADT-A01.json").exists()


@pytest.mark.parametrize(
    "raw, code",
    [
        ("garbage", "AR"),
        (make(pid(), pv1(), mtype="ADT^A04", ctrl="U"), "AR"),
        (make(pv1(), ctrl="C"), "AE"),
    ],
)
def test_mllp_error_acks(listener, tmp_path, raw, code):
    ack = send_message("127.0.0.1", listener, raw)
    assert f"MSA|{code}|" in ack
    assert len(dead(tmp_path)) == 1


def test_several_messages_on_one_connection_and_split_packets(listener):
    import socket

    with socket.create_connection(("127.0.0.1", listener), timeout=5) as sock:
        payload = wrap(make(pid(), pv1(), ctrl="M1").encode()) + wrap(
            make(pid(), pv1(), ctrl="M2").encode()
        )
        for i in range(0, len(payload), 7):  # dribble it out in tiny chunks
            sock.sendall(payload[i : i + 7])
        buf, acks = b"", []
        while len(acks) < 2:
            buf += sock.recv(4096)
            frame, buf = extract_frame(buf)
            while frame is not None:
                acks.append(decode(frame))
                frame, buf = extract_frame(buf)
    assert "MSA|AA|M1" in acks[0] and "MSA|AA|M2" in acks[1]
