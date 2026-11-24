"""Error types. Each maps to a pipeline stage and an ACK code."""


class Hl7FhirError(Exception):
    stage = "error"
    ack_code = "AE"


class ParseError(Hl7FhirError):
    """The text is not a well-formed HL7 v2 message (ACK code AR: rejected)."""

    stage = "parse"
    ack_code = "AR"


class UnsupportedMessage(Hl7FhirError):
    """Well-formed HL7 but a message type this converter does not handle (AR)."""

    stage = "convert"
    ack_code = "AR"


class ConversionError(Hl7FhirError):
    """A supported message that cannot be mapped (missing PID, bad date, ...) (AE)."""

    stage = "convert"
    ack_code = "AE"


class ValidationError(Hl7FhirError):
    """The produced FHIR failed the structural checks (AE)."""

    stage = "validate"
    ack_code = "AE"


class PostError(Hl7FhirError):
    """The FHIR server rejected or could not be reached (AE)."""

    stage = "post"
    ack_code = "AE"
