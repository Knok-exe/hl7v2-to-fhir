"""hl7-fhir command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .convert import SUPPORTED
from .mllp import MllpServer, send_message
from .parser import split_messages
from .pipeline import Pipeline

TZ = re.compile(r"^(Z|[+-]((0\d|1[0-3]):[0-5]\d|14:00))$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in getattr(record, "fields", {}).items() if v is not None})
        return json.dumps(entry)


def _tz(text: str) -> str:
    if not TZ.match(text):
        raise argparse.ArgumentTypeError("use Z or an offset like -05:00")
    return text


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default="out", help="where FHIR bundles are written")
    p.add_argument("--dead-letter", default="dead_letter", help="where failed messages go")
    p.add_argument("--post", metavar="URL", help="also POST each bundle to this FHIR base URL")
    p.add_argument(
        "--assume-tz",
        type=_tz,
        default="Z",
        help="timezone for HL7 timestamps with no offset (default Z)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl7-fhir", description=__doc__)
    p.add_argument("--version", action="version", version=f"hl7-fhir {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("convert", help="convert .hl7 files (or folders of them)")
    c.add_argument("paths", nargs="+")
    _common(c)
    ls = sub.add_parser("listen", help="run an MLLP listener")
    ls.add_argument("--host", default="127.0.0.1")
    ls.add_argument("--port", type=int, default=2575)
    _common(ls)
    s = sub.add_parser("send", help="send a file to an MLLP listener and print the ACK")
    s.add_argument("file")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=2575)
    sub.add_parser("types", help="list supported message types")
    return p


def _files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for item in paths:
        path = Path(item)
        found.extend(sorted(path.glob("*.hl7")) if path.is_dir() else [path])
    return found


def _cmd_convert(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args.out, args.dead_letter, args.post, args.assume_tz)
    failures = total = 0
    for path in _files(args.paths):
        if not path.exists():
            print(f"{path}: file not found", file=sys.stderr)
            failures += 1
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for raw in split_messages(text) or [text]:
            total += 1
            o = pipeline.process(raw)
            if o.ok:
                parts = ", ".join(f"{n} {t}" for t, n in o.counts.items())
                posted = " and posted" if o.posted else ""
                print(
                    f"OK    {path.name}  {o.type_label} {o.control_id}  ->  "
                    f"{o.bundle_path} ({parts}){posted}"
                )
                for w in o.warnings:
                    print(f"      warning: {w}")
            else:
                failures += 1
                print(
                    f"FAIL  {path.name}  {o.type_label or '?'} {o.control_id or '?'}  "
                    f"[{o.stage}] {o.error}\n      dead-lettered to {o.dead_letter_path}"
                )
    print(f"{total - failures} of {total} message(s) converted")
    return 1 if failures else 0


def _cmd_listen(args: argparse.Namespace) -> int:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.getLogger("hl7_fhir").addHandler(handler)
    logging.getLogger("hl7_fhir").setLevel(logging.INFO)
    pipeline = Pipeline(args.out, args.dead_letter, args.post, args.assume_tz)
    with MllpServer((args.host, args.port), pipeline.handle_mllp) as server:
        host, port = server.server_address[:2]
        print(
            f"MLLP listener on {host}:{port}  ->  {args.out}"
            f"{'  +  ' + args.post if args.post else ''}  (Ctrl+C to stop)",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("stopped")
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8-sig")
    for raw in split_messages(text) or [text]:
        print(send_message(args.host, args.port, raw).replace("\r", "\n").strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "types":
        print("\n".join(SUPPORTED))
        return 0
    return {"convert": _cmd_convert, "listen": _cmd_listen, "send": _cmd_send}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
