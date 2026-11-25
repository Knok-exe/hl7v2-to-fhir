"""HL7 v2 data type helpers: timestamps, CX, XPN, XAD, XTN, CE/CWE, units."""

from __future__ import annotations

import re
from datetime import date

from .errors import ConversionError
from .parser import Segment

_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.(\d{1,4}))?([+-]\d{4})?$")


def ts_to_fhir(text: str, assume_tz: str = "Z") -> str | None:
    """HL7 TS/DTM -> FHIR date or dateTime. FHIR requires a timezone once a time is present,
    so `assume_tz` is applied when the message has none. Missing minutes/seconds are zero-padded."""
    text = text.strip()
    if not text:
        return None
    m = _TS.match(text)
    if not m:
        raise ConversionError(f"cannot parse HL7 timestamp {text!r}")
    year, month, day, hour, minute, second, frac, tz = m.groups()
    try:
        date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        raise ConversionError(f"invalid calendar date in timestamp {text!r}") from None
    if month is None:
        return year
    if day is None:
        return f"{year}-{month}"
    if hour is None:
        return f"{year}-{month}-{day}"
    if int(hour) > 23 or int(minute or 0) > 59 or int(second or 0) > 60:
        raise ConversionError(f"invalid time in timestamp {text!r}")
    out = f"{year}-{month}-{day}T{hour}:{minute or '00'}:{second or '00'}"
    if frac:
        out += "." + frac
    if tz:
        tz = tz[:3] + ":" + tz[3:]
    else:
        tz = assume_tz
    return out + ("Z" if tz in ("+00:00", "-00:00") else tz)


def ts_to_date(text: str) -> str | None:
    """Truncate a timestamp to a FHIR `date` (for birthDate and similar)."""
    value = ts_to_fhir(text, "Z")
    return value[:10] if value else None


# ---- identifiers (CX) -----------------------------------------------------------------------


def system_uri(namespace: str = "", oid: str = "", id_type: str = "", fallback: str = "") -> str:
    """URI for an assigning authority (HD: namespace & universal id & universal id type)."""
    if oid and id_type.upper() == "ISO":
        return f"urn:oid:{oid}"
    if namespace:
        return f"urn:id:{namespace}"
    if oid:
        return f"urn:id:{oid}"
    return f"urn:id:{fallback or 'unknown'}"


def parse_cx(
    seg: Segment, field_no: int, rep: int = 1, fallback_authority: str = ""
) -> dict | None:
    value = seg.get(field_no, rep, 1)
    if not value:
        return None
    system = system_uri(
        seg.get(field_no, rep, 4, 1),
        seg.get(field_no, rep, 4, 2),
        seg.get(field_no, rep, 4, 3),
        fallback_authority,
    )
    ident: dict = {"system": system, "value": value}
    id_type = seg.get(field_no, rep, 5)
    if id_type:
        ident["type"] = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": id_type}]
        }
    return ident


def parse_ei(
    seg: Segment, field_no: int, fallback_authority: str = "", type_code: str | None = None
) -> dict | None:
    """EI: entity id ^ namespace ^ universal id ^ universal id type."""
    value = seg.get(field_no, 1, 1)
    if not value:
        return None
    ident: dict = {
        "system": system_uri(
            seg.get(field_no, 1, 2),
            seg.get(field_no, 1, 3),
            seg.get(field_no, 1, 4),
            fallback_authority,
        ),
        "value": value,
    }
    if type_code:
        ident["type"] = {
            "coding": [
                {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": type_code}
            ]
        }
    return ident


# ---- names, addresses, telecom ---------------------------------------------------------------

NAME_USE = {"L": "official", "D": "usual", "N": "nickname", "M": "maiden", "A": "anonymous"}
ADDRESS_USE = {"H": "home", "O": "work", "B": "billing"}


def parse_name(seg: Segment, field_no: int, rep: int) -> dict | None:
    family = seg.get(field_no, rep, 1, 1)
    given = [g for g in (seg.get(field_no, rep, 2), seg.get(field_no, rep, 3)) if g]
    if not family and not given:
        return None
    name: dict = {}
    if family:
        name["family"] = family
    if given:
        name["given"] = given
    suffix, prefix = seg.get(field_no, rep, 4), seg.get(field_no, rep, 5)
    if suffix:
        name["suffix"] = [suffix]
    if prefix:
        name["prefix"] = [prefix]
    use = NAME_USE.get(seg.get(field_no, rep, 7))
    if use:
        name["use"] = use
    return name


def display_name(seg: Segment, field_no: int, rep: int = 1, first_comp: int = 2) -> str:
    """'Given Family' for XCN-style fields (id ^ family ^ given ...)."""
    family = seg.get(field_no, rep, first_comp)
    given = seg.get(field_no, rep, first_comp + 1)
    return " ".join(p for p in (given, family) if p)


def parse_address(seg: Segment, field_no: int, rep: int) -> dict | None:
    lines = [x for x in (seg.get(field_no, rep, 1), seg.get(field_no, rep, 2)) if x]
    address: dict = {}
    if lines:
        address["line"] = lines
    for key, comp in (("city", 3), ("state", 4), ("postalCode", 5), ("country", 6)):
        value = seg.get(field_no, rep, comp)
        if value:
            address[key] = value
    if not address:
        return None
    use = ADDRESS_USE.get(seg.get(field_no, rep, 7))
    if use:
        address["use"] = use
    return address


def parse_telecom(seg: Segment, field_no: int, rep: int) -> dict | None:
    email = seg.get(field_no, rep, 4)
    equipment = seg.get(field_no, rep, 3).upper()
    if email:
        point: dict = {"system": "email", "value": email}
    else:
        number = seg.get(field_no, rep, 1) or (
            seg.get(field_no, rep, 6) + seg.get(field_no, rep, 7)
        )
        if not number:
            return None
        point = {"system": "fax" if equipment == "FX" else "phone", "value": number}
        if equipment == "CP":
            point["use"] = "mobile"
    use = {"PRN": "home", "WPN": "work"}.get(seg.get(field_no, rep, 2).upper())
    if use and "use" not in point:
        point["use"] = use
    return point


# ---- coded values (CE / CWE / CNE) ------------------------------------------------------------

CODE_SYSTEMS = {
    "LN": "http://loinc.org",
    "LOINC": "http://loinc.org",
    "SCT": "http://snomed.info/sct",
    "SNM": "http://snomed.info/sct",
    "SNOMEDCT": "http://snomed.info/sct",
    "SNOMED": "http://snomed.info/sct",
    "I10": "http://hl7.org/fhir/sid/icd-10",
    "ICD10": "http://hl7.org/fhir/sid/icd-10",
    "I10C": "http://hl7.org/fhir/sid/icd-10-cm",
    "ICD10CM": "http://hl7.org/fhir/sid/icd-10-cm",
    "I9": "http://hl7.org/fhir/sid/icd-9-cm",
    "I9C": "http://hl7.org/fhir/sid/icd-9-cm",
    "RXNORM": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "RXN": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "CVX": "http://hl7.org/fhir/sid/cvx",
    "UCUM": "http://unitsofmeasure.org",
    "CPT": "http://www.ama-assn.org/go/cpt",
    "CPT4": "http://www.ama-assn.org/go/cpt",
}


def coding_system(name: str) -> str:
    return CODE_SYSTEMS.get(name.upper(), f"urn:id:{name}") if name else "urn:id:local"


def parse_ce(seg: Segment, field_no: int, rep: int = 1) -> dict | None:
    """CodeableConcept from CE/CWE: primary (1-3) and alternate (4-6) codings plus text."""
    codings = []
    for code_c, text_c, sys_c in ((1, 2, 3), (4, 5, 6)):
        code = seg.get(field_no, rep, code_c)
        if (
            code
            and code_c == 1
            and not seg.get(field_no, rep, 2)
            and not seg.get(field_no, rep, 3)
            and re.search(r"\s", code)
        ):
            return {"text": code}  # free text sent in a coded field, not a code
        if code:
            coding = {"system": coding_system(seg.get(field_no, rep, sys_c)), "code": code}
            display = seg.get(field_no, rep, text_c)
            if display:
                coding["display"] = display
            codings.append(coding)
    text = seg.get(field_no, rep, 2) or seg.get(field_no, rep, 9)
    if not codings and not text:
        return None
    concept: dict = {}
    if codings:
        concept["coding"] = codings
    if text:
        concept["text"] = text
    return concept


_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def parse_decimal(text: str) -> int | float:
    """HL7 NM: optional sign, digits, optional decimal point. Python's float() is looser (it
    takes NaN, inf, 1e5 and 1_000), so the format is checked first."""
    text = text.strip()
    if not _DECIMAL.match(text):
        raise ConversionError(f"{text!r} is not a number")
    return int(text) if "." not in text else float(text)
