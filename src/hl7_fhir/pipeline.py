"""Parse -> convert -> validate -> write -> (post). Every failure is dead-lettered with the
stage and reason; nothing is dropped silently."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from .convert import Conversion, Options, convert
from .errors import Hl7FhirError, ParseError, PostError
from .mllp import build_ack
from .parser import Message, parse_message
from .validate import validate_or_raise

log = logging.getLogger("hl7_fhir")


@dataclass
class Outcome:
    ok: bool
    control_id: str = ""
    type_label: str = ""
    stage: str = ""
    error: str = ""
    ack_code: str = "AA"
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    bundle_path: Path | None = None
    dead_letter_path: Path | None = None
    posted: bool = False


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "unknown"


def post_bundle(
    url: str, bundle: dict, retries: int = 3, backoff: float = 1.0, timeout: float = 30.0
) -> None:
    """POST a transaction Bundle to a FHIR server. Retries connection errors and 5xx."""
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url,
                json=bundle,
                timeout=timeout,
                headers={
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/fhir+json",
                },
            )
        except requests.exceptions.RequestException as exc:
            last = f"could not reach {url}: {exc.__class__.__name__}"
        else:
            if resp.status_code in (200, 201):
                return
            last = f"server returned HTTP {resp.status_code}: {_outcome_text(resp)}"
            if resp.status_code < 500:
                raise PostError(last)  # a 4xx will not get better by retrying
        if attempt < retries:
            time.sleep(backoff * (2**attempt))
    raise PostError(last)


def wait_for_server(
    url: str, timeout: float, interval: float = 3.0, sleep=time.sleep, clock=time.monotonic
) -> bool:
    """Poll `<url>/metadata` until the FHIR server answers 200 or `timeout` seconds pass.

    Used at listener start-up: a fresh HAPI container needs a minute or so before it accepts
    requests, longer than the POST retry backoff covers."""
    deadline = clock() + timeout
    target = url.rstrip("/") + "/metadata"
    while True:
        try:
            if requests.get(target, timeout=5).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        if clock() >= deadline:
            return False
        sleep(interval)


def _outcome_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        issue = body["issue"][0]
        return issue.get("diagnostics") or issue.get("details", {}).get("text") or "no detail"
    except (ValueError, KeyError, IndexError, TypeError):
        return (resp.text or "no detail")[:200]


class Pipeline:
    def __init__(
        self,
        out_dir: str | Path = "out",
        dead_letter_dir: str | Path = "dead_letter",
        post_url: str | None = None,
        assume_tz: str = "Z",
        post_retries: int = 3,
        post_backoff: float = 1.0,
    ):
        self.out_dir, self.dead_dir = Path(out_dir), Path(dead_letter_dir)
        self.post_url, self.opts = post_url, Options(assume_tz=assume_tz)
        self.post_retries, self.post_backoff = post_retries, post_backoff

    def process(self, raw: str) -> Outcome:
        msg: Message | None = None
        conversion: Conversion | None = None
        path: Path | None = None
        try:
            msg = parse_message(raw)
            conversion = convert(msg, self.opts)
            validate_or_raise(conversion.bundle)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / f"{safe_name(msg.control_id)}_{msg.type_code}-{msg.event}.json"
            path.write_text(
                json.dumps(conversion.bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            outcome = Outcome(
                True,
                msg.control_id,
                msg.type_label,
                warnings=conversion.warnings,
                counts=conversion.counts,
                bundle_path=path,
            )
            if self.post_url:
                post_bundle(self.post_url, conversion.bundle, self.post_retries, self.post_backoff)
                outcome.posted = True
            self._log(outcome)
            return outcome
        except Exception as exc:  # noqa: BLE001 - nothing may be dropped, even on a bug
            if isinstance(exc, Hl7FhirError):
                stage, ack = exc.stage, exc.ack_code
                detail = str(exc)
            else:
                stage, ack = "internal", "AE"
                detail = f"unexpected {exc.__class__.__name__}: {exc} (this is a converter bug)"
            outcome = Outcome(
                False,
                msg.control_id if msg else _guess_control_id(raw),
                msg.type_label if msg else "",
                stage,
                detail,
                ack,
                conversion.warnings if conversion else [],
            )
            outcome.bundle_path = path  # set only if the failure came after the file was written
            outcome.dead_letter_path = self._dead_letter(raw, outcome)
            self._log(outcome)
            return outcome

    def handle_mllp(self, raw: str) -> str:
        """Process one framed message and return the ACK text."""
        outcome = self.process(raw)
        return build_ack(
            raw, outcome.ack_code, "" if outcome.ok else f"{outcome.stage}: {outcome.error}"
        )

    def _dead_letter(self, raw: str, outcome: Outcome) -> Path:
        self.dead_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        base = self.dead_dir / f"{stamp}_{outcome.stage}_{safe_name(outcome.control_id)}"
        base.with_suffix(".hl7").write_bytes(raw.encode("utf-8"))  # byte-exact: keep \r, no CRLF
        meta = {
            "stage": outcome.stage,
            "error": outcome.error,
            "control_id": outcome.control_id,
            "message_type": outcome.type_label,
            "ack_code": outcome.ack_code,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warnings": outcome.warnings,
        }
        base.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return base.with_suffix(".hl7")

    @staticmethod
    def _log(outcome: Outcome) -> None:
        log.info(
            "message processed",
            extra={
                "fields": {
                    "event": "converted" if outcome.ok else "dead_lettered",
                    "control_id": outcome.control_id,
                    "message_type": outcome.type_label,
                    "stage": outcome.stage or None,
                    "error": outcome.error or None,
                    "resources": outcome.counts or None,
                    "warnings": len(outcome.warnings),
                    "posted": outcome.posted,
                }
            },
        )


def _guess_control_id(raw: str) -> str:
    first = raw.replace("\x0b", "").replace("\n", "\r").split("\r", 1)[0]
    parts = first.split(first[3]) if first.startswith("MSH") and len(first) > 3 else []
    return parts[9] if len(parts) > 9 else ""


__all__ = ["Outcome", "ParseError", "Pipeline", "post_bundle", "wait_for_server"]
