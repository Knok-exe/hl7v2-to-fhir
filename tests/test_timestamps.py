import pytest

from hl7_fhir.datatypes import ts_to_date, ts_to_fhir
from hl7_fhir.errors import ConversionError


@pytest.mark.parametrize(
    "hl7, fhir",
    [
        ("2024", "2024"),
        ("202401", "2024-01"),
        ("20240115", "2024-01-15"),
        ("2024011509", "2024-01-15T09:00:00Z"),
        ("202401150930", "2024-01-15T09:30:00Z"),
        ("20240115093045", "2024-01-15T09:30:45Z"),
        ("20240115093045.25", "2024-01-15T09:30:45.25Z"),
        ("20240115093045-0500", "2024-01-15T09:30:45-05:00"),
        ("20240115093045+0000", "2024-01-15T09:30:45Z"),
        ("20240115093045+0530", "2024-01-15T09:30:45+05:30"),
    ],
)
def test_valid_timestamps(hl7, fhir):
    assert ts_to_fhir(hl7) == fhir


def test_assumed_timezone_only_applies_without_an_offset():
    assert ts_to_fhir("20240115093000", "-05:00") == "2024-01-15T09:30:00-05:00"
    assert ts_to_fhir("20240115093000+0100", "-05:00") == "2024-01-15T09:30:00+01:00"
    assert ts_to_fhir("20240115", "-05:00") == "2024-01-15"  # dates carry no timezone


def test_empty_is_none_and_date_truncation():
    assert ts_to_fhir("") is None and ts_to_date("") is None
    assert ts_to_date("19800102153000") == "1980-01-02"
    assert ts_to_date("1980") == "1980"


@pytest.mark.parametrize(
    "bad",
    ["abc", "2024-01-15", "20241301", "20240230", "2024011525", "202401150961", "202401150930xx"],
)
def test_invalid_timestamps_raise(bad):
    with pytest.raises(ConversionError):
        ts_to_fhir(bad)
