import json
import threading
from pathlib import Path

from hl7_fhir.cli import main
from hl7_fhir.mllp import MllpServer
from hl7_fhir.pipeline import Pipeline

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_convert_examples_end_to_end(tmp_path, capsys):
    code = main(
        [
            "convert",
            str(EXAMPLES / "messages"),
            str(EXAMPLES / "bad"),
            "--out",
            str(tmp_path / "out"),
            "--dead-letter",
            str(tmp_path / "dl"),
        ]
    )
    text = capsys.readouterr().out
    assert code == 1
    assert "7 of 11 message(s) converted" in text
    assert len(list((tmp_path / "out").glob("*.json"))) == 7
    assert len(list((tmp_path / "dl").glob("*.json"))) == 4
    assert text.count("FAIL") == 4 and text.count("OK") >= 7


def test_all_good_exits_zero(tmp_path, capsys):
    assert (
        main(
            [
                "convert",
                str(EXAMPLES / "messages"),
                "--out",
                str(tmp_path / "o"),
                "--dead-letter",
                str(tmp_path / "d"),
            ]
        )
        == 0
    )
    assert not (tmp_path / "d").exists()


def test_multiple_messages_in_one_file(tmp_path, capsys):
    both = "".join(
        (EXAMPLES / "messages" / n).read_text()
        for n in ("adt_a01_admit.hl7", "adt_a03_discharge.hl7")
    )
    f = tmp_path / "two.hl7"
    f.write_text(both)
    assert (
        main(
            ["convert", str(f), "--out", str(tmp_path / "o"), "--dead-letter", str(tmp_path / "d")]
        )
        == 0
    )
    assert "2 of 2" in capsys.readouterr().out


def test_missing_path_is_a_clear_error(tmp_path, capsys):
    assert main(["convert", str(tmp_path / "nope.hl7"), "--out", str(tmp_path)]) != 0
    assert "nope.hl7" in capsys.readouterr().err + capsys.readouterr().out


def test_types_lists_supported_messages(capsys):
    assert main(["types"]) == 0
    assert "ORU^R01" in capsys.readouterr().out


def test_send_prints_the_ack(tmp_path, capsys):
    pipe = Pipeline(tmp_path / "o", tmp_path / "d")
    server = MllpServer(("127.0.0.1", 0), pipe.handle_mllp, idle_timeout=5)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        rc = main(
            [
                "send",
                str(EXAMPLES / "messages" / "adt_a01_admit.hl7"),
                "--port",
                str(server.server_address[1]),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 0 and "MSA|AA|" in capsys.readouterr().out


def test_checked_in_example_bundles_match_the_converter():
    """examples/fhir/*.json must not drift from what the code produces."""
    from hl7_fhir.convert import convert
    from hl7_fhir.parser import parse_message

    for hl7 in sorted((EXAMPLES / "messages").glob("*.hl7")):
        expected = json.loads((EXAMPLES / "fhir" / f"{hl7.stem}.json").read_text())
        assert convert(parse_message(hl7.read_text())).bundle == expected, hl7.name
