"""HL7 v2 -> FHIR R4. Each supported message becomes a transaction Bundle of conditional
updates (PUT Resource?identifier=...), so replaying a message updates rather than duplicates.
The exception is the Patient in non-ADT messages: it is a conditional *create*, because a
lab/scheduling/order feed often sends a thin PID and a PUT would wipe the demographics the
ADT feed stored.
Resource ids are not set; entries reference each other through urn:uuid fullUrls derived
deterministically from identifiers (uuid5), so output is reproducible."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote

from .datatypes import (
    display_name,
    parse_address,
    parse_ce,
    parse_cx,
    parse_decimal,
    parse_ei,
    parse_name,
    parse_telecom,
    system_uri,
    ts_to_date,
    ts_to_fhir,
)
from .errors import ConversionError, UnsupportedMessage
from .parser import Message, Segment

NAMESPACE = uuid.UUID("6f1c8c3e-3b6a-4d6e-9d8a-1f0a5b7c2d10")
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
SYNTHETIC_TAG = {"system": "urn:id:hl7v2-to-fhir", "code": "synthetic-test-data"}


@dataclass
class Options:
    assume_tz: str = "Z"  # applied to timestamps that carry no UTC offset


@dataclass
class Conversion:
    bundle: dict
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.bundle["entry"]:
            rt = e["resource"]["resourceType"]
            out[rt] = out.get(rt, 0) + 1
        return out


class Builder:
    """Collects entries and hands out deterministic fullUrls."""

    def __init__(self, msg: Message, opts: Options):
        self.msg, self.opts = msg, opts
        self.entries: list[dict] = []
        self.warnings: list[str] = []

    def authority(self) -> str:
        return self.msg.msh.get(4, comp=1) or self.msg.msh.get(3, comp=1) or "unknown"

    def ts(self, text: str) -> str | None:
        return ts_to_fhir(text, self.opts.assume_tz)

    def instant(self, text: str, where: str) -> str | None:
        """FHIR `instant` needs date, time and offset; a date-only HL7 value is an error."""
        value = self.ts(text)
        if value and "T" not in value:
            raise ConversionError(f"{where} needs a time of day, got {text!r}")
        return value

    def add(self, resource: dict, identifier: dict, create_only: bool = False) -> str:
        rtype = resource["resourceType"]
        key = f"{identifier['system']}|{identifier['value']}"
        full_url = "urn:uuid:" + str(uuid.uuid5(NAMESPACE, f"{rtype}|{key}"))
        resource["meta"] = {
            "source": f"urn:id:message/{self.msg.control_id}",
            "tag": [dict(SYNTHETIC_TAG)],
        }
        query = (
            f"identifier={quote(identifier['system'], safe='')}"
            f"%7C{quote(identifier['value'], safe='')}"
        )
        if create_only:  # create if missing, otherwise leave the stored resource untouched
            request = {"method": "POST", "url": rtype, "ifNoneExist": query}
        else:
            request = {"method": "PUT", "url": f"{rtype}?{query}"}
        self.entries.append({"fullUrl": full_url, "resource": resource, "request": request})
        return full_url

    def bundle(self) -> dict:
        return {"resourceType": "Bundle", "type": "transaction", "entry": self.entries}


# ---- Patient ---------------------------------------------------------------------------------

GENDER = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "other"}


def build_patient(b: Builder, update: bool = False) -> str:
    """Patient from PID. Only ADT (update=True) replaces a stored Patient; other message types
    create it if it doesn't exist yet and otherwise just reference it."""
    pid = b.msg.first("PID")
    if pid is None:
        raise ConversionError("PID segment is required but missing")
    identifiers = []
    for rep in range(1, pid.count(3) + 1):
        ident = parse_cx(pid, 3, rep, b.authority())
        if ident:
            identifiers.append(ident)
    if not identifiers:
        raise ConversionError(
            "PID-3 (patient identifier list) is empty; cannot identify the patient"
        )
    patient: dict = {"resourceType": "Patient", "identifier": identifiers}
    names = [n for r in range(1, pid.count(5) + 1) if (n := parse_name(pid, 5, r))]
    if names:
        patient["name"] = names
    if pid.get(7):
        patient["birthDate"] = ts_to_date(pid.get(7))
    gender = pid.get(8).upper()
    if gender:
        if gender not in GENDER:
            b.warnings.append(f"PID-8 value {gender!r} is not a known administrative sex; omitted")
        else:
            patient["gender"] = GENDER[gender]
    addresses = [a for r in range(1, pid.count(11) + 1) if (a := parse_address(pid, 11, r))]
    if addresses:
        patient["address"] = addresses
    telecom = [
        t for n in (13, 14) for r in range(1, pid.count(n) + 1) if (t := parse_telecom(pid, n, r))
    ]
    if telecom:
        patient["telecom"] = telecom
    if pid.get(29):
        patient["deceasedDateTime"] = b.ts(pid.get(29))
    elif pid.get(30).upper() == "Y":
        patient["deceasedBoolean"] = True
    elif pid.get(30).upper() == "N":
        patient["deceasedBoolean"] = False
    return b.add(patient, identifiers[0], create_only=not update)


# ---- ADT -> Patient + Encounter --------------------------------------------------------------

ENCOUNTER_CLASS = {
    "I": ("IMP", "inpatient encounter"),
    "O": ("AMB", "ambulatory"),
    "E": ("EMER", "emergency"),
    "P": ("PRENC", "pre-admission"),
}


def convert_adt(b: Builder) -> None:
    msg = b.msg
    patient_url = build_patient(b, update=True)
    pv1 = msg.first("PV1")
    if pv1 is None:
        raise ConversionError("PV1 segment is required for ADT messages but missing")
    klass = pv1.get(2).upper()
    if klass not in ENCOUNTER_CLASS:
        raise ConversionError(
            f"PV1-2 patient class {klass!r} is missing or unsupported "
            f"(supported: {', '.join(sorted(ENCOUNTER_CLASS))})"
        )
    code, display = ENCOUNTER_CLASS[klass]
    start = pv1.get(44) or (
        msg.first("EVN").get(6) if msg.first("EVN") and msg.event == "A01" else ""
    )
    end = pv1.get(45) or (
        msg.first("EVN").get(6) if msg.first("EVN") and msg.event == "A03" else ""
    )
    status = "finished" if (msg.event == "A03" or end) else "in-progress"
    identifier = parse_cx(pv1, 19, 1, b.authority())
    if identifier is None:
        mrn = patient_identity_value(b)
        identifier = {
            "system": f"urn:id:{b.authority()}/visit",
            "value": f"{mrn}-{start or msg.control_id}",
        }
        b.warnings.append(
            "PV1-19 (visit number) missing; derived a visit identifier "
            f"{identifier['value']!r} from the patient id and admit time"
        )
    encounter: dict = {
        "resourceType": "Encounter",
        "identifier": [identifier],
        "status": status,
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": code,
            "display": display,
        },
        "subject": {"reference": patient_url},
    }
    period = {}
    if start:
        period["start"] = b.ts(start)
    if end:
        period["end"] = b.ts(end)
    if period:
        encounter["period"] = period
    poc = [pv1.get(3, 1, c) for c in (1, 2, 3)]
    if any(poc):
        encounter["location"] = [{"location": {"display": "/".join(p for p in poc if p)}}]
    if pv1.get(7, 1, 2):
        encounter["participant"] = [
            {
                "type": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/"
                                "v3-ParticipationType",
                                "code": "ATND",
                            }
                        ]
                    }
                ],
                "individual": {"display": display_name(pv1, 7)},
            }
        ]
    if pv1.get(36):
        encounter["hospitalization"] = {
            "dischargeDisposition": {
                "coding": [
                    {"system": "http://terminology.hl7.org/CodeSystem/v2-0112", "code": pv1.get(36)}
                ]
            }
        }
    b.add(encounter, identifier)


def patient_identity_value(b: Builder) -> str:
    return b.entries[0]["resource"]["identifier"][0]["value"] if b.entries else "unknown"


# ---- ORU -> Patient + DiagnosticReport + Observation ---------------------------------------

OBX_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "X": "cancelled",
    "D": "entered-in-error",
    "W": "entered-in-error",
    "R": "registered",
    "I": "registered",
    "S": "preliminary",
}
OBR_STATUS = {
    "F": "final",
    "P": "preliminary",
    "C": "corrected",
    "X": "cancelled",
    "A": "partial",
    "R": "registered",
    "O": "registered",
    "I": "registered",
    "S": "registered",
    "Y": "registered",
    "Z": "registered",
}
COMPARATOR = re.compile(r"^\s*(<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")
RANGE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*$")
TEXT_TYPES = {"ST", "TX", "FT"}
CODED_TYPES = {"CE", "CWE", "CNE"}
DATE_TYPES = {"DT", "TS", "DTM"}


def quantity(value: int | float, unit_seg: Segment | None, comparator: str | None = None) -> dict:
    q: dict = {"value": value}
    if comparator:
        q["comparator"] = comparator
    if unit_seg is not None:
        unit = unit_seg.get(6, 1, 1)
        if unit:
            q["unit"] = unit
            if unit_seg.get(6, 1, 3).upper() == "UCUM":
                q["system"] = "http://unitsofmeasure.org"
                q["code"] = unit
    return q


def observation_value(b: Builder, obx: Segment, rep: int) -> dict:
    vtype = obx.get(2).upper()
    raw = obx.get(5, rep, 1, None)
    if not any(obx.get(5, rep, c, None) for c in range(1, len(obx.components(5, rep)) + 1)):
        return {}  # genuinely empty; a value in a later component (SN "^1^:^128") is not
    if vtype == "":
        raise ConversionError(
            f"OBX-{obx.get(1) or '?'}: OBX-2 (value type) is required when OBX-5 has a value"
        )
    if vtype == "NM":
        m = COMPARATOR.match(raw)
        if m:
            return {"valueQuantity": quantity(parse_decimal(m.group(2)), obx, m.group(1))}
        return {"valueQuantity": quantity(parse_decimal(raw.strip()), obx)}
    if vtype == "SN":
        comparator, num1, sep, num2 = (obx.get(5, rep, c) for c in (1, 2, 3, 4))
        if num1 and not num2 and comparator in ("", "=", "<", ">", "<=", ">="):
            return {
                "valueQuantity": quantity(
                    parse_decimal(num1), obx, comparator if comparator not in ("", "=") else None
                )
            }
        return {"valueString": "".join(p for p in (comparator, num1, sep, num2) if p)}
    if vtype in TEXT_TYPES:
        return {"valueString": raw}
    if vtype in CODED_TYPES:
        concept = parse_ce(obx, 5, rep)
        return {"valueCodeableConcept": concept} if concept else {}
    if vtype in DATE_TYPES:
        return {"valueDateTime": b.ts(raw)}
    raise ConversionError(
        f"OBX-{obx.get(1) or '?'}: unsupported value type {vtype!r} "
        f"(supported: NM, SN, ST, TX, FT, CE, CWE, CNE, DT, TS, DTM)"
    )


def reference_range(obx: Segment) -> list[dict]:
    text = obx.get(7)
    if not text:
        return []
    m, unit_seg = RANGE.match(text), obx
    if m:
        return [
            {
                "low": quantity(parse_decimal(m.group(1)), unit_seg),
                "high": quantity(parse_decimal(m.group(2)), unit_seg),
            }
        ]
    c = COMPARATOR.match(text)
    if c and c.group(1) in ("<", "<="):
        return [{"high": quantity(parse_decimal(c.group(2)), unit_seg)}]
    if c:
        return [{"low": quantity(parse_decimal(c.group(2)), unit_seg)}]
    return [{"text": text}]


def convert_oru(b: Builder) -> None:
    msg = b.msg
    patient_url = build_patient(b)
    groups: list[tuple[Segment, list[Segment]]] = []  # (OBR, [OBX/NTE ...])
    for seg in msg.segments:
        if seg.name == "OBR":
            groups.append((seg, []))
        elif seg.name in ("OBX", "NTE") and groups:
            groups[-1][1].append(seg)
    if not groups:
        raise ConversionError("ORU^R01 needs at least one OBR segment")
    for index, (obr, children) in enumerate(groups, start=1):
        set_id = obr.get(1) or str(index)
        filler = parse_ei(obr, 3, b.authority(), "FILL")
        placer = parse_ei(obr, 2, b.authority(), "PLAC")
        report_id = filler or placer
        if report_id is None:
            report_id = {
                "system": f"urn:id:{b.authority()}/report",
                "value": f"{msg.control_id}-{set_id}",
            }
            b.warnings.append(
                f"OBR-{set_id}: no placer/filler order number; derived report "
                f"identifier {report_id['value']!r} from the message control id"
            )
        code = parse_ce(obr, 4)
        if code is None:
            raise ConversionError(f"OBR-{set_id}: OBR-4 (universal service identifier) is required")
        key = report_id["value"]
        result_refs, notes_for_report = [], []
        last_obs: dict | None = None
        for child in children:
            if child.name == "NTE":
                comment = child.get(3, 1, 1, None)
                if not comment:
                    continue
                if last_obs is not None:
                    last_obs.setdefault("note", []).append({"text": comment})
                else:
                    notes_for_report.append(comment)
                continue
            obs_code = parse_ce(child, 3)
            if obs_code is None:
                raise ConversionError(
                    f"OBX-{child.get(1) or '?'}: OBX-3 (observation identifier) is required"
                )
            reps = max(1, child.count(5))
            status = OBX_STATUS.get(child.get(11).upper())
            if status is None:
                status = "unknown"
                b.warnings.append(
                    f"OBX-{child.get(1)}: result status {child.get(11)!r} not "
                    "mapped; used 'unknown'"
                )
            for rep in range(1, reps + 1):
                suffix = f"#{rep}" if reps > 1 else ""
                ident = {
                    "system": f"urn:id:{b.authority()}/obx",
                    "value": f"{key}/{child.get(1) or len(b.entries)}{suffix}",
                }
                obs: dict = {
                    "resourceType": "Observation",
                    "identifier": [ident],
                    "status": status,
                    "code": obs_code,
                    "subject": {"reference": patient_url},
                }
                effective = child.get(14) or obr.get(7)
                if effective:
                    obs["effectiveDateTime"] = b.ts(effective)
                obs.update(observation_value(b, child, rep))
                flags = [f for r in range(1, child.count(8) + 1) if (f := child.get(8, r))]
                if flags:
                    obs["interpretation"] = [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/v2-0078",
                                    "code": f,
                                }
                            ]
                        }
                        for f in flags
                    ]
                ranges = reference_range(child)
                if ranges:
                    obs["referenceRange"] = ranges
                result_refs.append({"reference": b.add(obs, ident)})
                last_obs = b.entries[-1]["resource"]
        report: dict = {
            "resourceType": "DiagnosticReport",
            "identifier": [report_id],
            "status": OBR_STATUS.get(obr.get(25).upper(), "unknown"),
            "code": code,
            "subject": {"reference": patient_url},
        }
        if obr.get(25) and obr.get(25).upper() not in OBR_STATUS:
            b.warnings.append(f"OBR-{set_id}: result status {obr.get(25)!r} not mapped")
        if obr.get(24):
            report["category"] = [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": obr.get(24),
                        }
                    ]
                }
            ]
        if obr.get(7):
            report["effectiveDateTime"] = b.ts(obr.get(7))
        if obr.get(22):
            issued = b.ts(obr.get(22))
            if issued and "T" in issued:
                report["issued"] = issued
            else:  # issued is a FHIR instant; a bare date would be rejected by the server
                b.warnings.append(
                    f"OBR-{set_id}: OBR-22 {obr.get(22)!r} has no time; issued omitted"
                )
        if result_refs:
            report["result"] = result_refs
        if notes_for_report:
            report["conclusion"] = "\n".join(notes_for_report)
        b.add(report, report_id)


# ---- SIU -> Patient + Appointment -----------------------------------------------------------

APPOINTMENT_STATUS = {
    "booked": "booked",
    "pending": "pending",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "noshow": "noshow",
    "no show": "noshow",
    "complete": "fulfilled",
    "completed": "fulfilled",
    "arrived": "arrived",
    "started": "arrived",
    "waitlist": "waitlist",
    "deleted": "entered-in-error",
}
SIU_DEFAULT = {"S12": "booked", "S13": "booked", "S14": "booked", "S15": "cancelled"}


def convert_siu(b: Builder) -> None:
    msg = b.msg
    sch = msg.first("SCH")
    if sch is None:
        raise ConversionError("SCH segment is required for SIU messages but missing")
    patient_url = build_patient(b)
    ident = parse_ei(sch, 2, b.authority(), "FILL") or parse_ei(sch, 1, b.authority(), "PLAC")
    if ident is None:
        ident = {"system": f"urn:id:{b.authority()}/appointment", "value": msg.control_id}
        b.warnings.append(
            "SCH-1/SCH-2 missing; derived the appointment identifier from the message control id"
        )
    raw_status = (sch.get(25, 1, 1) or sch.get(25, 1, 2)).strip().lower()
    status = APPOINTMENT_STATUS.get(raw_status) if raw_status else SIU_DEFAULT[msg.event]
    if status is None:
        status = SIU_DEFAULT[msg.event]
        b.warnings.append(f"SCH-25 value {raw_status!r} not mapped; used {status!r}")
    start = sch.get(11, 1, 4) or ""
    end = sch.get(11, 1, 5) or ""
    appointment: dict = {"resourceType": "Appointment", "identifier": [ident], "status": status}
    if start:
        appointment["start"] = b.instant(start, "SCH-11.4 (appointment start)")
    duration, unit = sch.get(9), sch.get(10, 1, 1).lower()
    if start and not end and duration:
        if unit.startswith(("min", "m")) and duration.isdigit():
            minutes = int(duration)
            start_dt = datetime.fromisoformat(b.ts(start).replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(minutes=minutes)
            appointment["end"] = end_dt.isoformat().replace("+00:00", "Z")
            appointment["minutesDuration"] = minutes
        else:
            b.warnings.append(f"SCH-9/10 duration {duration!r} {unit!r} not understood; no end set")
    elif end:
        appointment["end"] = b.instant(end, "SCH-11.5 (appointment end)")
    reason = parse_ce(sch, 7)
    if reason:
        appointment["reasonCode"] = [reason]
    kind = parse_ce(sch, 8)
    if kind:
        appointment["appointmentType"] = kind
    services = [
        c
        for r in range(1, 1 + max((s.count(3) for s in msg.all("AIS")), default=0))
        for s in msg.all("AIS")
        if (c := parse_ce(s, 3, r))
    ]
    if services:
        appointment["serviceType"] = services
    participants = [{"actor": {"reference": patient_url}, "status": "accepted"}]
    for seg in msg.all("AIP"):
        who = display_name(seg, 3)
        if who:
            participants.append({"actor": {"display": who}, "status": "accepted"})
    for seg in msg.all("AIL"):
        where = "/".join(p for p in (seg.get(3, 1, 1), seg.get(3, 1, 2), seg.get(3, 1, 3)) if p)
        if where:
            participants.append({"actor": {"display": where}, "status": "accepted"})
    appointment["participant"] = participants
    b.add(appointment, ident)


# ---- ORM -> Patient + ServiceRequest ----------------------------------------------------------

ORC_CONTROL_STATUS = {
    "NW": "active",
    "OK": "active",
    "RP": "active",
    "SC": "active",
    "CA": "revoked",
    "OC": "revoked",
    "DC": "revoked",
    "HD": "on-hold",
}
ORDER_STATUS = {
    "CM": "completed",
    "IP": "active",
    "SC": "active",
    "HD": "on-hold",
    "CA": "revoked",
    "DC": "revoked",
    "ER": "entered-in-error",
    "A": "active",
    "RP": "active",
}
PRIORITY = {"S": "stat", "A": "asap", "R": "routine", "U": "urgent"}


def convert_orm(b: Builder) -> None:
    msg = b.msg
    patient_url = build_patient(b)
    orc: Segment | None = None
    count = 0
    for seg in msg.segments:
        if seg.name == "ORC":
            orc = seg
            continue
        if seg.name != "OBR":
            continue
        count += 1
        set_id = seg.get(1) or str(count)
        placer = (parse_ei(orc, 2, b.authority(), "PLAC") if orc else None) or parse_ei(
            seg, 2, b.authority(), "PLAC"
        )
        filler = (parse_ei(orc, 3, b.authority(), "FILL") if orc else None) or parse_ei(
            seg, 3, b.authority(), "FILL"
        )
        identifiers = [i for i in (placer, filler) if i]
        if not identifiers:
            fallback = {
                "system": f"urn:id:{b.authority()}/order",
                "value": f"{msg.control_id}-{set_id}",
            }
            identifiers.append(fallback)
            b.warnings.append(
                f"OBR-{set_id}: no placer/filler order number; derived "
                f"{fallback['value']!r} from the message control id"
            )
        code = parse_ce(seg, 4)
        if code is None:
            raise ConversionError(f"OBR-{set_id}: OBR-4 (universal service identifier) is required")
        status = None
        if orc:
            status = ORDER_STATUS.get(orc.get(5).upper()) if orc.get(5) else None
            status = status or ORC_CONTROL_STATUS.get(orc.get(1).upper())
        if status is None:
            status = "unknown"
            b.warnings.append(
                f"OBR-{set_id}: order status/control "
                f"{(orc.get(5) or orc.get(1)) if orc else ''!r} not mapped; "
                "used 'unknown'"
            )
        request: dict = {
            "resourceType": "ServiceRequest",
            "identifier": identifiers,
            "status": status,
            "intent": "order",
            "code": code,
            "subject": {"reference": patient_url},
        }
        priority = PRIORITY.get(seg.get(27, 1, 6).upper()) or (
            PRIORITY.get(orc.get(7, 1, 6).upper()) if orc else None
        )
        if priority:
            request["priority"] = priority
        if orc and orc.get(9):
            request["authoredOn"] = b.ts(orc.get(9))
        if orc and orc.get(12, 1, 2):
            request["requester"] = {"display": display_name(orc, 12)}
        reason = parse_ce(seg, 31)
        if reason:
            request["reasonCode"] = [reason]
        b.add(request, identifiers[0])
    if count == 0:
        raise ConversionError("ORM^O01 needs at least one OBR segment")


# ---- dispatch ---------------------------------------------------------------------------------

HANDLERS = {
    ("ADT", "A01"): convert_adt,
    ("ADT", "A03"): convert_adt,
    ("ADT", "A08"): convert_adt,
    ("ORU", "R01"): convert_oru,
    ("SIU", "S12"): convert_siu,
    ("SIU", "S13"): convert_siu,
    ("SIU", "S14"): convert_siu,
    ("SIU", "S15"): convert_siu,
    ("ORM", "O01"): convert_orm,
}
SUPPORTED = sorted(f"{t}^{e}" for t, e in HANDLERS)


def convert(msg: Message, opts: Options | None = None) -> Conversion:
    handler = HANDLERS.get((msg.type_code, msg.event))
    if handler is None:
        raise UnsupportedMessage(
            f"message type {msg.type_label!r} is not supported (supported: {', '.join(SUPPORTED)})"
        )
    builder = Builder(msg, opts or Options())
    handler(builder)
    return Conversion(builder.bundle(), builder.warnings)


__all__ = ["Conversion", "Options", "convert", "SUPPORTED", "system_uri"]
