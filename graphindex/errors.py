"""Structured error codes for query planning.

Every error surfaces to the user as a JSON object on stderr::

    {"code": "<Code>", "message": "<human readable detail>"}

and the process exits with status 2. No partial plan is ever emitted.
"""


class PlannerError(Exception):
    """Base class for all errors carrying a stable machine-readable code."""

    code = "Error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class IoError(PlannerError):
    """A required file is missing, unreadable, or not valid UTF-8."""

    code = "IoError"


class ParseError(PlannerError):
    """GraphQL or JSON source has a syntax error."""

    code = "ParseError"


class MappingError(PlannerError):
    """The schema is missing required mapping directives or they conflict."""

    code = "MappingError"


class InvalidRequest(PlannerError):
    """The request cannot be resolved (e.g. multiple anonymous candidates)."""

    code = "InvalidRequest"


class InvalidOperation(PlannerError):
    """The requested (or inferred) operation name is not present/valid."""

    code = "InvalidOperation"


class UnsupportedOperation(PlannerError):
    """The operation kind is supported by the schema but not by this tool."""

    code = "UnsupportedOperation"


class UnknownField(PlannerError):
    """A selected field or argument does not exist on the current entity."""

    code = "UnknownField"


class UnknownEntity(PlannerError):
    """A link points at an entity that is not mapped in the schema."""

    code = "UnknownEntity"


class InvalidJoin(PlannerError):
    """A @link target field is not the target entity's primary key."""

    code = "InvalidJoin"


class InvalidQuery(PlannerError):
    """The query has an empty or otherwise unsupported selection."""

    code = "InvalidQuery"


class VariablesError(PlannerError):
    """A variable is missing, null for a non-null type, or has the wrong type."""

    code = "VariablesError"
