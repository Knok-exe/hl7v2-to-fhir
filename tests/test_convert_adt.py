import pytest

from hl7_fhir.convert import SUPPORTED, convert
from hl7_fhir.errors import ConversionError, UnsupportedMessage
from hl7_fhir.parser import parse_message
from hl7_fhir.validate import validate_bundle

from .conftest import AUTH, convert_text, make, pid, pv1, resources, seg


def test_patient_mapping():
    text = make(
        seg("EVN", {1: "A01"}),
        pid(
            P5="Doe^Jane^Q^Jr^Dr^^L~Roe^Janie^^^^^N",
            P11="1 Main St^Unit 2^Town^IL^62701^USA^H",
            P13="(555)555-0100^PRN^PH~(555)555-0199^WPN^CP~^NET^Internet^jane@example.org",
            P29="20240101120000",
            P30="Y",
        ),
        pv1(),
    )
    patient = resources(convert_text(text), "Patient")[0]
    assert patient["identifier"][0] == {
        "system": "urn:oid:1.2.3",
        "value": "MRN1",
        "type": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]
        },
    }
    assert patient["name"][0] == {
        "family": "Doe",
        "given": ["Jane", "Q"],
        "suffix": ["Jr"],
        "prefix": ["Dr"],
        "use": "official",
    }
    assert patient["name"][1]["use"] == "nickname"
    assert patient["birthDate"] == "1980-01-02" and patient["gender"] == "female"
    assert patient["address"][0] == {
        "line": ["1 Main St", "Unit 2"],
        "city": "Town",
        "state": "IL",
        "postalCode": "62701",
        "country": "USA",
        "use": "home",
    }
    systems = [(t["system"], t.get("use")) for t in patient["telecom"]]
    assert systems == [("phone", "home"), ("phone", "mobile"), ("email", None)]
    assert patient["deceasedDateTime"] == "2024-01-01T12:00:00Z"


@pytest.mark.parametrize(
    "code, expected",
    [
        ("M", "male"),
        ("F", "female"),
        ("O", "other"),
        ("U", "unknown"),
        ("A", "other"),
        ("N", "other"),
    ],
)
def test_gender_codes(code, expected):
    assert resources(convert_text(make(pid(P8=code), pv1())), "Patient")[0]["gender"] == expected


def test_unknown_gender_is_omitted_with_a_warning():
    conv = convert_text(make(pid(P8="Q"), pv1()))
    assert "gender" not in resources(conv, "Patient")[0]
    assert any("PID-8" in w for w in conv.warnings)


def test_identifier_without_authority_falls_back_to_sending_facility():
    conv = convert_text(make(pid(P3="MRN9"), pv1()))
    assert resources(conv, "Patient")[0]["identifier"][0]["system"] == "urn:id:FAC"


def test_a01_admit_is_an_in_progress_inpatient_encounter():
    conv = convert_text(
        make(seg("EVN", {1: "A01"}), pid(), pv1(P3="3W^312^A", P7="12^Smith^Dana^^^Dr"))
    )
    enc = resources(conv, "Encounter")[0]
    assert enc["status"] == "in-progress"
    assert enc["class"]["code"] == "IMP"
    assert enc["period"] == {"start": "2024-01-15T08:00:00Z"}
    assert enc["location"][0]["location"]["display"] == "3W/312/A"
    assert enc["participant"][0]["individual"]["display"] == "Dana Smith"
    assert enc["identifier"][0] == {"system": "urn:oid:1.2.3", "value": "V1"}
    assert validate_bundle(conv.bundle) == []


def test_a03_discharge_is_finished_with_an_end_time_and_disposition():
    conv = convert_text(
        make(seg("EVN", {1: "A03"}), pid(), pv1(P36="01", P45="20240118140000"), mtype="ADT^A03")
    )
    enc = resources(conv, "Encounter")[0]
    assert enc["status"] == "finished"
    assert enc["period"]["end"] == "2024-01-18T14:00:00Z"
    assert enc["hospitalization"]["dischargeDisposition"]["coding"][0]["code"] == "01"


def test_a03_without_pv1_45_uses_the_evn_event_time():
    conv = convert_text(
        make(seg("EVN", {1: "A03", 6: "20240118135500"}), pid(), pv1(), mtype="ADT^A03")
    )
    assert resources(conv, "Encounter")[0]["period"]["end"] == "2024-01-18T13:55:00Z"


def test_a08_with_a_discharge_time_is_finished_otherwise_in_progress():
    open_ = convert_text(make(pid(), pv1(), mtype="ADT^A08"))
    closed = convert_text(make(pid(), pv1(P45="20240118140000"), mtype="ADT^A08"))
    assert resources(open_, "Encounter")[0]["status"] == "in-progress"
    assert resources(closed, "Encounter")[0]["status"] == "finished"


@pytest.mark.parametrize("klass, code", [("I", "IMP"), ("O", "AMB"), ("E", "EMER"), ("P", "PRENC")])
def test_patient_class_mapping(klass, code):
    assert (
        resources(convert_text(make(pid(), pv1(P2=klass))), "Encounter")[0]["class"]["code"] == code
    )


@pytest.mark.parametrize("klass", ["", "X", "R"])
def test_missing_or_unsupported_patient_class_is_an_error(klass):
    with pytest.raises(ConversionError, match="PV1-2"):
        convert_text(make(pid(), pv1(P2=klass)))


def test_missing_visit_number_derives_an_identifier_and_warns():
    conv = convert_text(make(pid(), pv1(P19="")))
    enc = resources(conv, "Encounter")[0]
    assert enc["identifier"][0]["value"] == "MRN1-20240115080000"
    assert any("PV1-19" in w for w in conv.warnings)


@pytest.mark.parametrize(
    "segments, fragment",
    [
        ((pv1(),), "PID segment"),
        ((pid(),), "PV1 segment"),
        ((pid(P3=""), pv1()), "PID-3"),
    ],
)
def test_missing_required_segments(segments, fragment):
    with pytest.raises(ConversionError, match=fragment):
        convert_text(make(*segments))


def test_unsupported_message_type_lists_what_is_supported():
    with pytest.raises(UnsupportedMessage, match="ADT\\^A04") as info:
        convert_text(make(pid(), pv1(), mtype="ADT^A04"))
    assert all(t in str(info.value) for t in ("ADT^A01", "ORU^R01", "SIU^S12", "ORM^O01"))
    assert "ADT^A01" in SUPPORTED


def test_output_is_deterministic_and_idempotent_by_identifier():
    text = make(pid(), pv1())
    a, b = convert_text(text).bundle, convert_text(text).bundle
    assert a == b
    patient_entry = a["entry"][0]
    assert patient_entry["request"] == {
        "method": "PUT",
        "url": "Patient?identifier=urn%3Aoid%3A1.2.3%7CMRN1",
    }
    # the encounter points at the patient entry's fullUrl
    assert a["entry"][1]["resource"]["subject"]["reference"] == patient_entry["fullUrl"]


def test_same_patient_in_two_messages_gets_the_same_fullurl():
    one = convert(parse_message(make(pid(), pv1(), ctrl="A"))).bundle
    two = convert(parse_message(make(pid(P5="Other^Name"), pv1(), ctrl="B"))).bundle
    assert one["entry"][0]["fullUrl"] == two["entry"][0]["fullUrl"]


def test_every_resource_carries_a_synthetic_tag_and_message_source():
    meta = convert_text(make(pid(), pv1(), ctrl="CTRL7")).bundle["entry"][0]["resource"]["meta"]
    assert meta["tag"][0]["code"] == "synthetic-test-data"
    assert meta["source"] == "urn:id:message/CTRL7"


def test_assume_tz_option_is_applied():
    from hl7_fhir.convert import Options

    bundle = convert(parse_message(make(pid(), pv1())), Options(assume_tz="-05:00")).bundle
    assert bundle["entry"][1]["resource"]["period"]["start"] == "2024-01-15T08:00:00-05:00"


def test_authority_constant_is_what_the_helpers_use():
    assert AUTH == "FAC&1.2.3&ISO"
