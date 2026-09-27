"""Error codes and the delivery error type shared by the planner and CLI."""

INPUT_NOT_FOUND = "INPUT_NOT_FOUND"
SYMLINK_NOT_ALLOWED = "SYMLINK_NOT_ALLOWED"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
EMPTY_SELECTION = "EMPTY_SELECTION"
SENSITIVE_PATH = "SENSITIVE_PATH"
INVALID_ROOT = "INVALID_ROOT"
IO_ERROR = "IO_ERROR"
INPUT_CHANGED = "INPUT_CHANGED"
DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
OUTPUT_NOT_ALLOWED = "OUTPUT_NOT_ALLOWED"
OUTPUT_EXISTS = "OUTPUT_EXISTS"
VERIFY_FAILED = "VERIFY_FAILED"


class DeliveryError(Exception):
    """Raised with a stable machine-readable code and a human message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
