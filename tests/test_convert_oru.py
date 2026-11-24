import pytest

from hl7_fhir.errors import ConversionError
from hl7_fhir.validate import validate_bundle

from .conftest import convert_text, make, pid, resources, seg

OBR = seg(
    "OBR",
    {
        1: "1",
        2: "P1^EHR",
        3: "F1^LAB",
        4: "51990-0^Basic metabolic panel^LN",
        7: "20240116060000",
        22: "20240116064000",
        24: "LAB",
        25: "F",
    },
)


def oru(*obx: str, obr: str = OBR, extra: tuple = ()):
    return convert_text(make(pid(), obr, *obx, *extra, mtype="ORU^R01"))


def obx(
    n: int,
    vtype: str,
    code: str,
    value: str,
    units: str = "",
    rng: str = "",
    flag: str = "",
    status: str = "F",
) -> str:
    return seg(
        "OBX", {1: str(n), 2: vtype, 3: code, 5: value, 6: units, 7: rng, 8: flag, 11: status}
    )


def test_numeric_result_with_units_range_and_flag():
    conv = oru(obx(1, "NM", "2823-3^Potassium^LN", "5.4", "mmol/L^^UCUM", "3.5-5.1", "H"))
    o = resources(conv, "Observation")[0]
    assert o["valueQuantity"] == {
        "value": 5.4,
        "unit": "mmol/L",
        "system": "http://unitsofmeasure.org",
        "code": "mmol/L",
    }
    assert o["referenceRange"][0]["low"]["value"] == 3.5
    assert o["referenceRange"][0]["high"]["value"] == 5.1
    assert o["interpretation"][0]["coding"][0] == {
        "system": "http://terminology.hl7.org/CodeSystem/v2-0078",
        "code": "H",
    }
    assert o["status"] == "final" and o["code"]["coding"][0]["system"] == "http://loinc.org"
    assert validate_bundle(conv.bundle) == []


def test_integer_values_stay_integers_and_units_without_ucum_get_no_code():
    o = resources(oru(obx(1, "NM", "X^Y^L", "141", "mmol/L")), "Observation")[0]
    assert o["valueQuantity"] == {"value": 141, "unit": "mmol/L"}


def test_diagnostic_report_links_every_observation():
    conv = oru(obx(1, "NM", "A^a^L", "1"), obx(2, "NM", "B^b^L", "2"), obx(3, "ST", "C^c^L", "x"))
    report = resources(conv, "DiagnosticReport")[0]
    obs_urls = [
        e["fullUrl"] for e in conv.bundle["entry"] if e["resource"]["resourceType"] == "Observation"
    ]
    assert [r["reference"] for r in report["result"]] == obs_urls
    assert report["status"] == "final" and report["category"][0]["coding"][0]["code"] == "LAB"
    assert report["effectiveDateTime"] == "2024-01-16T06:00:00Z"
    assert report["issued"] == "2024-01-16T06:40:00Z"


def test_repeated_obx5_becomes_one_observation_per_repetition():
    conv = oru(
        obx(
            1,
            "CWE",
            "630-4^Bacteria^LN",
            "112283007^Escherichia coli^SCT~3092008^Staphylococcus aureus^SCT",
        )
    )
    obs = resources(conv, "Observation")
    assert len(obs) == 2
    assert [o["valueCodeableConcept"]["coding"][0]["code"] for o in obs] == ["112283007", "3092008"]
    assert len({o["identifier"][0]["value"] for o in obs}) == 2  # distinct identifiers
    assert len(resources(conv, "DiagnosticReport")[0]["result"]) == 2
    assert validate_bundle(conv.bundle) == []


def test_repeated_obx_segments_with_the_same_code_all_survive():
    conv = oru(obx(1, "NM", "2823-3^K^LN", "4.0"), obx(2, "NM", "2823-3^K^LN", "4.2"))
    assert [o["valueQuantity"]["value"] for o in resources(conv, "Observation")] == [4.0, 4.2]
    assert validate_bundle(conv.bundle) == []


def test_escaped_delimiters_and_line_breaks_in_text_values():
    value = r"Growth \F\ count >100,000\.br\Review \T\ repeat \S\ \R\ \E\ end"
    o = resources(oru(obx(1, "TX", "18725-2^Comment^LN", value)), "Observation")[0]
    assert o["valueString"] == "Growth | count >100,000\nReview & repeat ^ ~ \\ end"


def test_escaped_delimiter_in_a_patient_name():
    conv = convert_text(
        make(pid(P5=r"Smith\S\Jones^Pat\F\Q"), OBR, obx(1, "NM", "A^a^L", "1"), mtype="ORU^R01")
    )
    assert resources(conv, "Patient")[0]["name"][0] == {"family": "Smith^Jones", "given": ["Pat|Q"]}


@pytest.mark.parametrize(
    "raw, comparator, value",
    [("<5", "<", 5), (">100", ">", 100), ("<=0.5", "<=", 0.5), (">=7", ">=", 7)],
)
def test_numeric_with_comparator(raw, comparator, value):
    q = resources(oru(obx(1, "NM", "A^a^L", raw, "mg/L^^UCUM")), "Observation")[0]["valueQuantity"]
    assert q["comparator"] == comparator and q["value"] == value


def test_structured_numeric_sn_value():
    o = resources(oru(obx(1, "SN", "A^a^L", ">^1.9", "mg/dL^^UCUM")), "Observation")[0]
    assert o["valueQuantity"]["comparator"] == ">" and o["valueQuantity"]["value"] == 1.9
    ratio = resources(oru(obx(1, "SN", "A^a^L", "^1^:^128")), "Observation")[0]
    assert ratio["valueString"] == "1:128"


def test_coded_date_and_string_values():
    conv = oru(
        obx(1, "CE", "A^a^L", "POS^Positive^L"),
        obx(2, "DT", "B^b^L", "20240110"),
        obx(3, "ST", "C^c^L", "Yellow"),
    )
    o1, o2, o3 = resources(conv, "Observation")
    assert o1["valueCodeableConcept"]["text"] == "Positive"
    assert o2["valueDateTime"] == "2024-01-10"
    assert o3["valueString"] == "Yellow"


def test_non_numeric_value_in_an_nm_field_is_a_clear_error():
    with pytest.raises(ConversionError, match="not a number"):
        oru(obx(1, "NM", "A^a^L", "pending"))


def test_unsupported_value_type_and_missing_type():
    with pytest.raises(ConversionError, match="unsupported value type 'ED'"):
        oru(obx(1, "ED", "A^a^L", "blob"))
    with pytest.raises(ConversionError, match="OBX-2"):
        oru(obx(1, "", "A^a^L", "1"))


@pytest.mark.parametrize(
    "hl7, fhir",
    [
        ("F", "final"),
        ("P", "preliminary"),
        ("C", "corrected"),
        ("X", "cancelled"),
        ("W", "entered-in-error"),
    ],
)
def test_result_status_mapping(hl7, fhir):
    assert (
        resources(oru(obx(1, "NM", "A^a^L", "1", status=hl7)), "Observation")[0]["status"] == fhir
    )


def test_unmapped_status_becomes_unknown_with_a_warning():
    conv = oru(obx(1, "NM", "A^a^L", "1", status="Q"))
    assert resources(conv, "Observation")[0]["status"] == "unknown"
    assert any("not mapped" in w for w in conv.warnings)


def test_reference_range_variants():
    lt = resources(oru(obx(1, "NM", "A^a^L", "1", "mg/L", "<5")), "Observation")[0]
    gt = resources(oru(obx(1, "NM", "A^a^L", "1", "mg/L", ">10")), "Observation")[0]
    text = resources(oru(obx(1, "NM", "A^a^L", "1", "mg/L", "negative")), "Observation")[0]
    assert (
        list(lt["referenceRange"][0]) == ["high"] and lt["referenceRange"][0]["high"]["value"] == 5
    )
    assert list(gt["referenceRange"][0]) == ["low"]
    assert text["referenceRange"] == [{"text": "negative"}]


def test_multiple_abnormal_flags_are_all_kept():
    o = resources(oru(obx(1, "NM", "A^a^L", "1", flag="H~A")), "Observation")[0]
    assert [i["coding"][0]["code"] for i in o["interpretation"]] == ["H", "A"]


def test_nte_after_obx_annotates_the_observation_and_after_obr_the_report():
    conv = convert_text(
        make(
            pid(),
            OBR,
            seg("NTE", {1: "1", 3: "Report comment"}),
            obx(1, "NM", "A^a^L", "1"),
            seg("NTE", {1: "2", 3: "Hemolyzed"}),
            mtype="ORU^R01",
        )
    )
    assert resources(conv, "Observation")[0]["note"] == [{"text": "Hemolyzed"}]
    assert resources(conv, "DiagnosticReport")[0]["conclusion"] == "Report comment"


def test_two_obr_groups_become_two_reports_with_their_own_results():
    second = seg("OBR", {1: "2", 3: "F2^LAB", 4: "5778-6^Color^LN", 25: "P"})
    conv = convert_text(
        make(
            pid(),
            OBR,
            obx(1, "NM", "A^a^L", "1"),
            second,
            obx(1, "ST", "B^b^L", "Yellow"),
            mtype="ORU^R01",
        )
    )
    reports = resources(conv, "DiagnosticReport")
    assert [r["status"] for r in reports] == ["final", "preliminary"]
    assert [len(r["result"]) for r in reports] == [1, 1]
    assert validate_bundle(conv.bundle) == []  # OBX-1 repeats across OBRs without colliding


def test_obx_effective_time_falls_back_to_the_obr_time():
    o = resources(oru(obx(1, "NM", "A^a^L", "1")), "Observation")[0]
    assert o["effectiveDateTime"] == "2024-01-16T06:00:00Z"


def test_report_without_order_numbers_derives_an_identifier_and_warns():
    bare = seg("OBR", {1: "1", 4: "51990-0^BMP^LN", 25: "F"})
    conv = oru(obx(1, "NM", "A^a^L", "1"), obr=bare)
    assert resources(conv, "DiagnosticReport")[0]["identifier"][0]["value"] == "T1-1"
    assert any("derived report" in w for w in conv.warnings)


def test_missing_obr_or_codes_are_clear_errors():
    with pytest.raises(ConversionError, match="OBR"):
        convert_text(make(pid(), obx(1, "NM", "A^a^L", "1"), mtype="ORU^R01"))
    with pytest.raises(ConversionError, match="OBR-4"):
        oru(obx(1, "NM", "A^a^L", "1"), obr=seg("OBR", {1: "1", 3: "F1^LAB"}))
    with pytest.raises(ConversionError, match="OBX-3"):
        oru(obx(1, "NM", "", "1"))


def test_obr_without_obx_gives_a_report_with_no_results():
    conv = oru()
    report = resources(conv, "DiagnosticReport")[0]
    assert "result" not in report and validate_bundle(conv.bundle) == []
