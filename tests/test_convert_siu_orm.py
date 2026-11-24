import pytest

from hl7_fhir.errors import ConversionError
from hl7_fhir.validate import validate_bundle

from .conftest import convert_text, make, pid, resources, seg

SCH = seg(
    "SCH",
    {
        1: "A1^EHR",
        2: "A1^SCHED",
        7: "FOLLOWUP^Follow-up^HL70276",
        11: "^^30^20240201093000^20240201100000",
        25: "Booked",
    },
)


def siu(sch=SCH, *rest, mtype="SIU^S12"):
    return convert_text(make(sch, pid(), *rest, mtype=mtype))


def test_s12_appointment_mapping():
    conv = siu(
        SCH,
        seg("AIS", {1: "1", 3: "99213^Office visit^CPT"}),
        seg("AIL", {1: "1", 3: "CLINIC-B^EXAM2"}),
        seg("AIP", {1: "1", 3: "5678^Samplestone^Riley^^^Dr"}),
    )
    appt = resources(conv, "Appointment")[0]
    assert appt["status"] == "booked"
    assert appt["start"] == "2024-02-01T09:30:00Z" and appt["end"] == "2024-02-01T10:00:00Z"
    assert appt["serviceType"][0]["coding"][0]["code"] == "99213"
    actors = [p["actor"].get("display", "") for p in appt["participant"]]
    assert any("Samplestone" in a for a in actors) and any("CLINIC-B" in a for a in actors)
    assert any(p["actor"]["reference"].startswith("urn:uuid:") for p in appt["participant"])
    assert validate_bundle(conv.bundle) == []


def test_duration_in_minutes_is_used_when_there_is_no_end_time():
    sch = seg(
        "SCH", {1: "A2^EHR", 9: "45", 10: "min^minutes", 11: "^^^20240201093000", 25: "Booked"}
    )
    appt = resources(siu(sch), "Appointment")[0]
    assert appt["start"] == "2024-02-01T09:30:00Z"
    assert appt["minutesDuration"] == 45 or appt["end"] == "2024-02-01T10:15:00Z"


@pytest.mark.parametrize(
    "event, status", [("S12", "booked"), ("S13", "booked"), ("S14", "booked"), ("S15", "cancelled")]
)
def test_event_defaults_status_when_sch25_is_missing(event, status):
    sch = seg("SCH", {1: "A3^EHR", 11: "^^^20240201093000^20240201100000"})
    assert resources(siu(sch, mtype=f"SIU^{event}"), "Appointment")[0]["status"] == status


@pytest.mark.parametrize(
    "hl7, fhir",
    [
        ("Pending", "pending"),
        ("Cancelled", "cancelled"),
        ("Noshow", "noshow"),
        ("Complete", "fulfilled"),
        ("Started", "arrived"),
    ],
)
def test_sch25_statuses(hl7, fhir):
    sch = seg("SCH", {1: "A4^EHR", 11: "^^^20240201093000^20240201100000", 25: hl7})
    assert resources(siu(sch), "Appointment")[0]["status"] == fhir


def test_missing_start_is_caught_by_validation_and_missing_ids_warn():
    conv = siu(seg("SCH", {1: "A5^EHR", 25: "Booked"}))
    assert any("start" in p for p in validate_bundle(conv.bundle))
    conv = siu(seg("SCH", {11: "^^^20240201093000^20240201100000"}))
    assert resources(conv, "Appointment")[0]["identifier"][0]["value"] == "T1"
    assert any("SCH-1/SCH-2" in w for w in conv.warnings)


def test_siu_without_sch_is_an_error():
    with pytest.raises(ConversionError, match="SCH"):
        convert_text(make(pid(), mtype="SIU^S12"))


ORC = seg(
    "ORC", {1: "NW", 2: "ORD1^EHR", 5: "IP", 9: "20240116055000", 12: "1234^Examplesmith^Dana^^^Dr"}
)
ORDER_OBR = seg(
    "OBR",
    {1: "1", 2: "ORD1^EHR", 4: "51990-0^Basic metabolic panel^LN", 27: "^^^^^S", 31: "Annual labs"},
)


def orm(orc=ORC, obr=ORDER_OBR):
    return convert_text(make(pid(), orc, obr, mtype="ORM^O01"))


def test_order_becomes_a_service_request():
    conv = orm()
    sr = resources(conv, "ServiceRequest")[0]
    assert sr["status"] == "active" and sr["intent"] == "order"
    assert sr["code"]["coding"][0] == {
        "system": "http://loinc.org",
        "code": "51990-0",
        "display": "Basic metabolic panel",
    }
    assert sr["priority"] == "stat"
    assert sr["authoredOn"] == "2024-01-16T05:50:00Z"
    assert sr["requester"]["display"] == "Dana Examplesmith"
    assert sr["reasonCode"][0]["text"] == "Annual labs"
    assert validate_bundle(conv.bundle) == []


@pytest.mark.parametrize(
    "control, status", [("IP", "active"), ("CM", "completed"), ("CA", "revoked")]
)
def test_order_status_from_orc5_or_orc1(control, status):
    orc = seg("ORC", {1: "NW", 2: "ORD1^EHR", 5: control})
    assert resources(orm(orc), "ServiceRequest")[0]["status"] == status


def test_cancel_control_code_revokes_the_order():
    orc = seg("ORC", {1: "CA", 2: "ORD1^EHR"})
    assert resources(orm(orc), "ServiceRequest")[0]["status"] == "revoked"


def test_order_without_a_code_is_an_error_and_without_orc_still_converts_with_a_warning():
    with pytest.raises(ConversionError, match="OBR-4"):
        orm(obr=seg("OBR", {1: "1", 2: "ORD1^EHR"}))
    conv = convert_text(make(pid(), ORDER_OBR, mtype="ORM^O01"))
    assert resources(conv, "ServiceRequest")[0]["intent"] == "order"
