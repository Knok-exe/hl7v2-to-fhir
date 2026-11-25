"""Regression tests for the fixes from the code review."""

import pytest

from hl7_fhir.cli import main
from hl7_fhir.datatypes import parse_decimal
from hl7_fhir.errors import ConversionError
from hl7_fhir.mllp import build_ack
from hl7_fhir.pipeline import Pipeline, wait_for_server
from hl7_fhir.validate import validate_bundle

from .conftest import convert_text, make, pid, pv1, resources, seg

OBR = seg("OBR", {1: "1", 3: "F1^LAB", 4: "51990-0^BMP^LN", 7: "20240116060000"})


def glucose(value: str) -> str:
    return seg("OBX", {1: "1", 2: "NM", 3: "2345-7^Glucose^LN", 5: value, 6: "mg/dL", 11: "F"})


# ---- a thin PID in a lab/scheduling/order message must not wipe the stored Patient ----------


def test_adt_replaces_the_patient_but_other_messages_only_create_it():
    adt = convert_text(make(pid(), pv1()))
    assert adt.bundle["entry"][0]["request"]["method"] == "PUT"

    thin_pid = seg("PID", {1: "1", 3: "MRN1^^^FAC&1.2.3&ISO^MR"})
    oru = convert_text(make(thin_pid, OBR, glucose("95"), mtype="ORU^R01"))
    patient = oru.bundle["entry"][0]
    assert patient["request"] == {
        "method": "POST",
        "url": "Patient",
        "ifNoneExist": "identifier=urn%3Aoid%3A1.2.3%7CMRN1",
    }
    # same fullUrl as the ADT patient, so references still line up
    assert patient["fullUrl"] == adt.bundle["entry"][0]["fullUrl"]
    assert validate_bundle(oru.bundle) == []


# ---- FHIR instant fields --------------------------------------------------------------------


def test_date_only_appointment_start_is_a_conversion_error():
    sch = seg("SCH", {1: "A1^EHR", 2: "A1^SCHED", 9: "30", 10: "MIN", 11: "^^^20240301"})
    with pytest.raises(ConversionError, match="needs a time"):
        convert_text(make(sch, pid(), mtype="SIU^S12"))


def test_date_only_obr22_is_dropped_with_a_warning():
    obr = seg("OBR", {1: "1", 3: "F1^LAB", 4: "51990-0^BMP^LN", 22: "20240116"})
    conv = convert_text(make(pid(), obr, glucose("95"), mtype="ORU^R01"))
    assert "issued" not in resources(conv, "DiagnosticReport")[0]
    assert any("OBR-22" in w for w in conv.warnings)


def test_validator_rejects_a_date_only_instant():
    bundle = convert_text(make(pid(), OBR, glucose("95"), mtype="ORU^R01")).bundle
    report = next(e for e in bundle["entry"] if e["resource"]["resourceType"] == "DiagnosticReport")
    report["resource"]["issued"] = "2024-01-16"
    assert any("instant" in p for p in validate_bundle(bundle))


# ---- numbers ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["NaN", "inf", "-Infinity", "1e5", "1_000", "", "1.2.3"])
def test_non_hl7_numbers_are_rejected(text):
    with pytest.raises(ConversionError):
        parse_decimal(text)


@pytest.mark.parametrize(
    "text, value", [("7", 7), ("-3", -3), ("+5", 5), ("7.10", 7.1), (".5", 0.5), (" 12 ", 12)]
)
def test_hl7_numbers_parse(text, value):
    assert parse_decimal(text) == value and type(parse_decimal(text)) is type(value)


def test_nan_result_is_a_clean_ae_not_an_internal_bug(tmp_path):
    outcome = Pipeline(tmp_path / "out", tmp_path / "dl").process(
        make(pid(), OBR, glucose("NaN"), mtype="ORU^R01")
    )
    assert not outcome.ok and outcome.stage == "convert" and outcome.ack_code == "AE"


# ---- ACKs, send, wait ---------------------------------------------------------------------


def test_ack_control_ids_are_unique_and_echo_processing_id_and_version():
    raw = make(pid(), pv1()).replace("|P|2.5.1", "|T|2.3")
    a, b = (build_ack(raw, "AA").split("\r")[0].split("|") for _ in range(2))
    assert a[9] != b[9] and len(a[9]) <= 20
    assert a[10] == "T" and a[11] == "2.3"


def test_send_without_a_listener_is_a_clean_error(tmp_path, capsys):
    msg = tmp_path / "m.hl7"
    msg.write_text(make(pid(), pv1()))
    assert main(["send", str(msg), "--port", "9"]) == 1
    assert "Is `hl7-fhir listen` running?" in capsys.readouterr().err


def test_wait_for_server_gives_up_after_the_timeout():
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    assert wait_for_server("http://127.0.0.1:9/fhir", 6, sleep=sleep, clock=lambda: now[0]) is False
    assert now[0] >= 6


def test_wait_for_server_returns_once_metadata_answers():
    import http.server
    import threading

    class Meta(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/fhir/metadata" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Meta)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/fhir/"
        assert wait_for_server(url, 5, sleep=lambda s: None) is True
    finally:
        srv.shutdown()
        srv.server_close()
