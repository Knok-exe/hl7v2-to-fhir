"""Regenerate examples/fhir/<name>.json from examples/messages/<name>.hl7.

python scripts/regenerate_examples.py
"""

import json
from pathlib import Path

from hl7_fhir.convert import convert
from hl7_fhir.parser import parse_message

root = Path(__file__).resolve().parent.parent / "examples"
for path in sorted((root / "messages").glob("*.hl7")):
    bundle = convert(parse_message(path.read_text(encoding="utf-8"))).bundle
    target = root / "fhir" / f"{path.stem}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path.name} -> {target.relative_to(root.parent)}")
