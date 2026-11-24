"""Structural checks on the FHIR this tool produces. NOT a full FHIR validator: it checks the
required elements, code sets, date formats and reference integrity for the resource types
emitted here. Use HAPI's $validate (or fhir.resources) for complete schema validation."""

from __future__ import annotations

import re

from .errors import ValidationError

DATE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")
DATETIME = re.compile(
    r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01])"
    r"(T([01]\d|2[0-3]):[0-5]\d:([0-5]\d|60)(\.\d+)?(Z|[+-]((0\d|1[0-3]):[0-5]\d|14:00)))?)?)?$"
)

ENUMS = {
    ("Patient", "gender"): {"male", "female", "other", "unknown"},
    ("Encounter", "status"): {
        "planned",
        "arrived",
        "triaged",
        "in-progress",
        "onleave",
        "finished",
        "cancelled",
        "entered-in-error",
        "unknown",
    },
    ("Observation", "status"): {
        "registered",
        "preliminary",
        "final",
        "amended",
        "corrected",
        "cancelled",
        "entered-in-error",
        "unknown",
    },
    ("DiagnosticReport", "status"): {
        "registered",
        "partial",
        "preliminary",
        "final",
        "amended",
        "corrected",
        "appended",
        "cancelled",
        "entered-in-error",
        "unknown",
    },
    ("Appointment", "status"): {
        "proposed",
        "pending",
        "booked",
        "arrived",
        "fulfilled",
        "cancelled",
        "noshow",
        "entered-in-error",
        "checked-in",
        "waitlist",
    },
    ("ServiceRequest", "status"): {
        "draft",
        "active",
        "on-hold",
        "revoked",
        "completed",
        "entered-in-error",
        "unknown",
    },
}
DATE_FIELDS = {
    "Patient": ["birthDate"],
    "Encounter": [],
    "Observation": [],
    "DiagnosticReport": [],
    "Appointment": [],
    "ServiceRequest": [],
}
DATETIME_FIELDS = {
    "Patient": ["deceasedDateTime"],
    "Encounter": [],
    "Observation": ["effectiveDateTime", "valueDateTime"],
    "DiagnosticReport": ["effectiveDateTime", "issued"],
    "Appointment": ["start", "end"],
    "ServiceRequest": ["authoredOn"],
}
VALUE_KEYS = ("valueQuantity", "valueString", "valueCodeableConcept", "valueDateTime")


def _refs(node, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            found.append(ref)
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _refs(item, found)


def _check_resource(res: dict, problems: list[str], where: str) -> None:
    rtype = res.get("resourceType")
    for (t, key), allowed in ENUMS.items():
        if t == rtype and key in res and res[key] not in allowed:
            problems.append(f"{where}: {key}={res[key]!r} is not one of {sorted(allowed)}")
    for key in DATE_FIELDS.get(rtype, []):
        if key in res and not (isinstance(res[key], str) and DATE.match(res[key])):
            problems.append(f"{where}: {key}={res[key]!r} is not a valid FHIR date")
    for key in DATETIME_FIELDS.get(rtype, []):
        if key in res and not (isinstance(res[key], str) and DATETIME.match(res[key])):
            problems.append(f"{where}: {key}={res[key]!r} is not a valid FHIR dateTime")

    def need(key: str) -> None:
        if not res.get(key):
            problems.append(f"{where}: {rtype}.{key} is required")

    if rtype == "Patient":
        need("identifier")
        for ident in res.get("identifier", []):
            if not ident.get("value"):
                problems.append(f"{where}: identifier without a value")
    elif rtype == "Encounter":
        need("status")
        if not res.get("class", {}).get("code"):
            problems.append(f"{where}: Encounter.class.code is required")
        need("subject")
        for key in ("start", "end"):
            val = res.get("period", {}).get(key)
            if val and not DATETIME.match(val):
                problems.append(f"{where}: period.{key}={val!r} is not a valid dateTime")
    elif rtype == "Observation":
        need("status")
        need("code")
        need("subject")
        present = [k for k in VALUE_KEYS if k in res]
        if len(present) > 1:
            problems.append(f"{where}: Observation has more than one value[x]: {present}")
        if "valueQuantity" in res and not isinstance(
            res["valueQuantity"].get("value"), (int, float)
        ):
            problems.append(f"{where}: valueQuantity.value must be a number")
    elif rtype == "DiagnosticReport":
        need("status")
        need("code")
        need("subject")
    elif rtype == "Appointment":
        need("status")
        if not res.get("participant"):
            problems.append(f"{where}: Appointment.participant is required")
        for p in res.get("participant", []):
            if not p.get("status"):
                problems.append(f"{where}: Appointment.participant.status is required")
        has_times = "start" in res and "end" in res
        if not has_times and res.get("status") not in ("proposed", "cancelled", "waitlist"):
            problems.append(
                f"{where}: Appointment needs start and end unless status is "
                "proposed, cancelled or waitlist"
            )
    elif rtype == "ServiceRequest":
        need("status")
        need("intent")
        need("subject")
    else:
        problems.append(f"{where}: unexpected resource type {rtype!r}")


def validate_bundle(bundle: dict) -> list[str]:
    problems: list[str] = []
    if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
        problems.append("Bundle: must be a transaction Bundle")
    entries = bundle.get("entry") or []
    if not entries:
        problems.append("Bundle: has no entries")
    full_urls: set[str] = set()
    request_urls: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"entry[{i}]"
        full_url = entry.get("fullUrl", "")
        if not full_url:
            problems.append(f"{where}: fullUrl is required")
        elif full_url in full_urls:
            problems.append(f"{where}: duplicate fullUrl {full_url}")
        full_urls.add(full_url)
        req = entry.get("request", {})
        if not req.get("method") or not req.get("url"):
            problems.append(f"{where}: request.method and request.url are required")
        elif req["method"] not in ("PUT", "POST"):
            problems.append(f"{where}: request.method {req['method']!r} is not PUT or POST")
        elif req["url"] in request_urls:
            problems.append(
                f"{where}: two entries use the same conditional URL {req['url']} "
                "(a transaction cannot update one resource twice)"
            )
        request_urls.add(req.get("url", ""))
        _check_resource(entry.get("resource", {}), problems, where)
    refs: list[str] = []
    _refs(entries, refs)
    for ref in refs:
        if ref.startswith("urn:uuid:") and ref not in full_urls:
            problems.append(f"reference {ref} does not match any entry in the bundle")
    return problems


def validate_or_raise(bundle: dict) -> None:
    problems = validate_bundle(bundle)
    if problems:
        shown = "; ".join(problems[:5]) + (
            f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        )
        raise ValidationError(shown)
