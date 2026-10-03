"""Error type carrying a stable machine-readable code."""


class PlanError(Exception):
    """An error that maps to a JSON object on stderr and exit code 2."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
