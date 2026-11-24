# Mapping reference

Every message becomes one FHIR R4 **transaction Bundle**. Each resource is a conditional
`PUT Resource?identifier=system|value`, so re-sending a message updates instead of duplicating.
Bundle `fullUrl`s are `urn:uuid` values derived deterministically (uuid5) from the identifier,
so the same patient gets the same id in every message. Every resource is tagged
`synthetic-test-data` and carries `meta.source = urn:id:message/<MSH-10>`.

## Message types

| HL7 v2 | Produces | Notes |
|---|---|---|
| ADT^A01 admit | Patient, Encounter (`in-progress`) | |
| ADT^A03 discharge | Patient, Encounter (`finished`) | end = PV1-45, else EVN-6 |
| ADT^A08 update | Patient, Encounter | `finished` only if PV1-45 is present |
| ORU^R01 result | Patient, DiagnosticReport, Observation(s) | one report per OBR |
| SIU^S12–S15 | Patient, Appointment | S15 defaults to `cancelled` |
| ORM^O01 order | Patient, ServiceRequest(s) | one per OBR |

Anything else is rejected with `UnsupportedMessage` (ACK `AR`) and dead-lettered.

## Segment mapping

| HL7 v2 | FHIR | Notes |
|---|---|---|
| PID-3 (CX) | Patient.identifier | authority → `system` (ISO → `urn:oid:`, else `urn:id:<ns>`); falls back to MSH-4 |
| PID-5 (XPN) | Patient.name | name type `L`→official, `N`→nickname, `M`→maiden |
| PID-7 | birthDate | partial precision kept |
| PID-8 | gender | M/F/O/U; A,N → other; anything else omitted + warning |
| PID-11 (XAD) | address | type H→home, O→work |
| PID-13 (XTN) | telecom | PH/CP/Internet; use PRN→home, WPN→work, ORN/EMR |
| PID-29 | deceasedDateTime | |
| PV1-2 | Encounter.class | I→IMP, O→AMB, E→EMER, P→PRENC; others are an error |
| PV1-3 | location | `unit/room/bed` |
| PV1-7 | participant | attending |
| PV1-19 | identifier | visit number; derived from MRN + admit time with a warning if absent |
| PV1-36 | hospitalization.dischargeDisposition | |
| PV1-44 / PV1-45 | period.start / period.end | |
| OBR-4 | DiagnosticReport.code / ServiceRequest.code | required |
| OBR-7, OBR-22 | effective / issued | |
| OBR-25 | DiagnosticReport.status | F final, P preliminary, C corrected, X cancelled |
| OBX-2 / OBX-5 | Observation.value[x] | NM, SN, ST, TX, FT, CE, CWE, CNE, DT, TS, DTM |
| OBX-6 | valueQuantity unit | `UCUM` as third component also sets `system`/`code` |
| OBX-7 | referenceRange | `a-b`, `<a`, `>a`, else text |
| OBX-8 | interpretation | each repetition kept (v2-0078) |
| OBX-11 | status | F, P, C, X, W; unmapped → `unknown` + warning |
| NTE | note / conclusion | after OBX → Observation.note; after OBR → report conclusion |
| SCH-1/2 | Appointment.identifier | derived from MSH-10 with a warning if absent |
| SCH-11 | start / end | or duration in minutes from SCH-9/10 |
| SCH-25 | status | |
| AIS-3 | serviceType | |
| AIP-3, AIL-3 | participant | |
| ORC-1/5 | ServiceRequest.status | |
| OBR-27.6 | priority | |
| ORC-9 / ORC-12 | authoredOn / requester | |
| OBR-31 | reasonCode (text) | |

## Design decisions

- **Nothing is silently dropped.** If data cannot be mapped, the converter either raises (with
  the field name) or adds a warning. A message that fails goes to the dead-letter queue as the
  original bytes plus a `.json` file with stage, error and ACK code.
- **Repeated OBX-5** (e.g. several organisms) becomes one Observation per repetition.
- **Escapes** (`\F\ \S\ \T\ \R\ \E\ \.br\ \Xhh\`) are decoded after splitting, so an escaped
  delimiter never splits a field. MSH-1 and MSH-2 are never split or unescaped.
- **Timestamps without an offset** use `--assume-tz` (default `Z`); the assumption is yours to make.
- **Free text in a coded field** becomes `CodeableConcept.text`, not a fake coding.
- **Comparators** (`<5`, `>^1.9`) become `valueQuantity.comparator`.
- **ACK codes:** parse errors and unsupported types → `AR`; conversion, validation and
  post failures → `AE`; success → `AA`.

## Deliberately not mapped

Marital status, race/ethnicity, DG1 diagnoses, insurance (IN1), and most of PV1. Adding
them is straightforward, but each needs a vocabulary decision rather than a guess.

## The validator is not a full FHIR validator

`validate.py` checks structure the converter can get wrong: transaction shape, unique
fullUrls, request methods, required fields and enums per resource, date formats,
one `value[x]` per Observation, Appointment start/end, and reference integrity. For full
profile validation, run the HAPI validator (or POST to a HAPI server, which `--post` does).
