import pytest

from hl7_fhir.errors import ParseError
from hl7_fhir.parser import Encoding, parse_message, split_messages

from .conftest import make, pid, seg


def test_escaped_delimiters_are_not_split_on():
    """The classic bug: \\F\\ \\S\\ \\T\\ \\R\\ \\E\\ must survive as literal characters."""
    msg = parse_message(make(seg("NTE", {3: r"a\F\b\S\c\T\d\R\e\E\f"})))
    nte = msg.first("NTE")
    assert nte.count(3) == 1  # no repetition split
    assert len(nte.components(3)) == 1  # no component split
    assert nte.get(3, comp=1, sub=None) == "a|b^c&d~e\\f"


def test_line_break_hex_and_unknown_escapes():
    value = "one\\.br\\two \\X41\\" + "\\X4243\\ " + "\\Zlocal\\ \\H\\bold\\N\\"
    nte = parse_message(make(seg("NTE", {3: value}))).first("NTE")
    text = nte.get(3, sub=None)
    assert text.startswith("one\ntwo ABC")  # \X41\ -> A, \X4243\ -> BC
    assert "\\Zlocal\\" in text  # local escapes preserved
    assert text.endswith("bold")  # highlight markers dropped


def test_repetitions_components_and_subcomponents():
    msg = parse_message(make(seg("PID", {3: "111^^^NS&1.2.3&ISO^MR~222^^^OTHER^SS"})))
    pid = msg.first("PID")
    assert pid.count(3) == 2
    assert (pid.get(3, 1, 1), pid.get(3, 2, 1)) == ("111", "222")
    assert pid.get(3, 1, 4, 1) == "NS" and pid.get(3, 1, 4, 2) == "1.2.3"
    assert pid.get(3, 1, 4, 3) == "ISO"
    assert pid.get(3, 2, 4) == "OTHER" and pid.get(3, 2, 5) == "SS"


def test_missing_fields_and_components_are_empty_strings():
    pid = parse_message(make(seg("PID", {3: "111"}))).first("PID")
    assert pid.get(9) == "" and pid.get(3, 1, 7) == "" and pid.get(3, 5, 1) == ""
    assert pid.count(9) == 0


def test_msh_fields_are_numbered_from_the_separator():
    msg = parse_message(make())
    assert msg.msh.get(1) == "|"
    assert msg.msh.get(2, sub=None) == "^~\\&"  # not split, not unescaped
    assert msg.msh.get(3) == "APP" and msg.msh.get(9, comp=2) == "A01"
    assert msg.control_id == "T1" and msg.version == "2.5.1" and msg.type_label == "ADT^A01"


def test_custom_delimiters_from_msh2():
    msg = parse_message("MSH#*~\\^T#A#B#C#D#20240101##ADT*A01#X9#P#2.3\rPID#1##77*~88*9")
    assert msg.enc == Encoding("#", "*", "~", "\\", "^")
    assert msg.event == "A01" and msg.control_id == "X9"
    assert msg.first("PID").get(3, 1, 1) == "77"


@pytest.mark.parametrize("ending", ["\r", "\n", "\r\n"])
def test_any_line_ending_and_mllp_bytes_are_accepted(ending):
    text = (
        "\x0b" + ending.join(["MSH|^~\\&|A|B|C|D|20240101||ADT^A01|X|P|2.5", "PID|1||5"]) + "\x1c\r"
    )
    msg = parse_message(text)
    assert [s.name for s in msg.segments] == ["MSH", "PID"]


def test_split_messages_in_one_file():
    one, two = make(pid(), ctrl="A"), make(pid(), ctrl="B")
    parts = split_messages(one + "\n" + two)
    assert [parse_message(p).control_id for p in parts] == ["A", "B"]


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("hello world", "MSH"),
        ("PID|1||2", "MSH"),
        ("MSH|^", "too short"),
        ("MSH|^~|A|B", "at least four"),
        ("MSH|^^\\&|A|B", "not distinct"),
        ("MSH|^~\\&|A|B|C|D|2024||ADT^A01||P|2.5", "MSH-10"),
        ("MSH|^~\\&|A|B|C|D|2024|||X|P|2.5", "MSH-9"),
        ("MSH|^~\\&|A|B|C|D|2024||ADT^A01|X|P|2.5\rpid|1", "malformed"),
        ("MSH|^~\\&|A|B|C|D|2024||ADT^A01|X|P|2.5\rPID1||", "malformed"),
    ],
)
def test_malformed_messages_raise_clear_errors(text, fragment):
    with pytest.raises(ParseError, match=fragment):
        parse_message(text)
