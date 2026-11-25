# hl7v2-to-fhir

Converts HL7 v2 messages to validated FHIR R4 transaction Bundles. Messages come from files or
a small MLLP listener; bundles go to disk and optionally to a FHIR server such as HAPI.
**Messages that cannot be converted are never dropped:** they go to a dead-letter queue with the
original bytes and a clear reason.

> **Synthetic data only.** Every example is invented (fake names, MRNs and 555 phone numbers).
> Do not run real patient data through a demo setup like this.

[![ci](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest -q

# convert the bundled examples (7 good, 4 deliberately bad)
hl7-fhir convert examples/messages examples/bad --out out --dead-letter dead_letter
```

Output:

```
OK    adt_a01_admit.hl7 ...
FAIL  bad_missing_pid.hl7 ...  [convert/AE] PID segment is required ...
7 of 11 message(s) converted
```

Exit code is 1 if any message failed, so it works in scripts and CI.

### MLLP listener

```bash
hl7-fhir listen --port 2575 --post http://localhost:8080/fhir   # terminal 1
hl7-fhir send examples/messages/oru_r01_lab.hl7 --port 2575      # terminal 2 → prints the ACK
```

The listener replies `AA` on success, `AR` for messages it can never convert (unparseable,
unsupported type) and `AE` for conversion, validation or server errors. It logs one JSON line
per message.

### With a local HAPI server (Docker)

```bash
docker compose up
hl7-fhir send examples/messages/adt_a01_admit.hl7
curl "http://localhost:8080/fhir/Patient?identifier=100234"
```

HAPI takes a minute or so to start. The converter waits up to 180 s for HAPI's `/metadata`
(`listen --wait 180`) before it starts listening, so the first `send` may need that long. Both
ports are bound to `127.0.0.1` only.

> **Status:** the Docker files have not been run end to end yet (no Docker where this was
> built). The `--post` path *is* tested against a local mock FHIR server, including 4xx,
> 5xx-retry and unreachable cases. On Linux you may need to `chmod 777 out dead_letter` for
> the container user.

## What is supported

ADT^A01/A03/A08, ORU^R01, SIU^S12–S15, ORM^O01 → Patient, Encounter, DiagnosticReport,
Observation, Appointment, ServiceRequest. See [docs/mapping.md](docs/mapping.md) for the
field-by-field table, design decisions and what is deliberately not mapped.

Before/after pairs live in `examples/messages/*.hl7` and `examples/fhir/*.json`
(a test fails if they drift from the converter). `oru_r01_tricky.hl7` exercises escaped
delimiters, a repeated OBX-5, `<5`-style comparators and two OBR groups.

## Design

```
raw text ─ parse ─ convert ─ validate ─ write ─ (POST) ─ ACK
              └──────── any failure ────────► dead_letter/<stamp>_<stage>_<id>.hl7 + .json
```

Each stage raises a typed error that carries its stage and ACK code. Even an unexpected bug is
dead-lettered (stage `internal`) rather than crashing the listener.

## Limitations (read before relying on it)

- Not a full FHIR validator; `validate.py` checks the mistakes this converter could make.
  Full profile validation happens when you POST to HAPI.
- A deliberately small slice of HL7 v2: six message types, no Z-segments, no batch (FHS/BHS).
- No authentication or TLS on the MLLP listener or the HAPI demo. Local use only.
- Dead-lettered messages are stored but there is no replay command yet.
- Timestamps with no offset are assumed to be `--assume-tz` (default UTC).
- Tested against messages written for this project, not against a real interface engine's output.

## Development

```bash
ruff check . && ruff format --check . && python -m pytest -q
python scripts/regenerate_examples.py   # after changing a mapping
```

MIT licensed.
