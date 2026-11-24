import copy

import pytest

from hl7_fhir.errors import ValidationError
from hl7_fhir.validate import validate_bundle, validate_or_raise

from .conftest import convert_text, make, pid, pv1


@pytest.fixture
def good():
    return convert_text(make(pid(), pv1())).bundle


def broken(bundle, mutate):
    clone = copy.deepcopy(bundle)
    mutate(clone)
    return validate_bundle(clone)


def test_converted_output_has_no_problems(good):
    assert validate_bundle(good) == []
    validate_or_raise(good)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda b: b.update(type="collection"), "transaction"),
        (lambda b: b["entry"][0]["resource"].pop("resourceType"), "resource type"),
        (lambda b: b["entry"][1]["resource"].pop("status"), "status"),
        (lambda b: b["entry"][1]["resource"].update(status="bogus"), "status"),
        (lambda b: b["entry"][1]["resource"].pop("class"), "class"),
        (lambda b: b["entry"][1]["resource"].pop("subject"), "subject"),
        (lambda b: b["entry"][0]["resource"].update(birthDate="1980-13-45"), "birthDate"),
        (lambda b: b["entry"][0]["resource"].update(gender="robot"), "gender"),
        (
            lambda b: b["entry"][1]["resource"]["subject"].update(reference="urn:uuid:nope"),
            "reference",
        ),
        (lambda b: b["entry"][1].update(fullUrl=b["entry"][0]["fullUrl"]), "duplicate"),
        (lambda b: b["entry"][0]["request"].update(method="PATCH"), "method"),
        (lambda b: b["entry"][0].pop("request"), "request"),
    ],
)
def test_validator_catches_problems(good, mutate, fragment):
    problems = broken(good, mutate)
    assert problems and any(fragment.lower() in p.lower() for p in problems), problems


def test_validate_or_raise_lists_the_problems(good):
    good["type"] = "batch"
    with pytest.raises(ValidationError, match="transaction"):
        validate_or_raise(good)


def test_observation_with_two_values_is_rejected():
    from .test_convert_oru import obx, oru

    conv = oru(obx(1, "NM", "A^a^L", "1"))
    o = next(
        e["resource"]
        for e in conv.bundle["entry"]
        if e["resource"]["resourceType"] == "Observation"
    )
    o["valueString"] = "also a string"
    assert any("value" in p for p in validate_bundle(conv.bundle))
