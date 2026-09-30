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
INVALID_KEY = "INVALID_KEY"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
LOCAL_STORE_CONFLICT = "LOCAL_STORE_CONFLICT"
STATE_INVALID = "STATE_INVALID"
BUSY = "BUSY"
CONFIG_INVALID = "CONFIG_INVALID"
BUCKET_NOT_PRIVATE = "BUCKET_NOT_PRIVATE"
INVALID_OBJECT_KEY = "INVALID_OBJECT_KEY"
REMOTE_CONFLICT = "REMOTE_CONFLICT"
REMOTE_INTEGRITY = "REMOTE_INTEGRITY"
REMOTE_AUTH = "REMOTE_AUTH"
REMOTE_ERROR = "REMOTE_ERROR"
REMOTE_UNKNOWN = "REMOTE_UNKNOWN"
REMOTE_DELETE_UNCONFIRMED = "REMOTE_DELETE_UNCONFIRMED"
LINK_EXPIRED = "LINK_EXPIRED"
TASK_REVOKED = "TASK_REVOKED"
DELIVERY_NOT_READY = "DELIVERY_NOT_READY"
RECIPIENT_INVALID = "RECIPIENT_INVALID"
CONTACT_NOT_FOUND = "CONTACT_NOT_FOUND"
CONTACTS_INVALID = "CONTACTS_INVALID"
SMTP_CONNECT = "SMTP_CONNECT"
SMTP_TLS = "SMTP_TLS"
SMTP_AUTH = "SMTP_AUTH"
SMTP_REJECTED = "SMTP_REJECTED"
SMTP_PREPARE = "SMTP_PREPARE"
SMTP_UNKNOWN = "SMTP_UNKNOWN"


class DeliveryError(Exception):
    """Raised with a stable machine-readable code and a human message."""

    def __init__(self, code: str, message: str, *, diagnostics=None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = safe_diagnostics(diagnostics)


# Only controlled vocabulary crosses provider/CLI/MCP boundaries. Never copy
# exception strings, request objects, response bodies or arbitrary class names.
_DIAGNOSTIC_VALUES = {
    "reason": {"connect_timeout", "read_timeout", "timeout", "connection_error",
               "tls_error", "stream_interrupted", "transport_error", "http_server_error"},
    "stage": {"upload", "bucket_check", "stat", "list", "delete", "download", "download_stream", "request"},
    "exception_type": {"ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError",
                       "SSLError", "ChunkedEncodingError", "ContentDecodingError", "unknown"},
}


def safe_diagnostics(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, allowed in _DIAGNOSTIC_VALUES.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    for key in ("elapsed_seconds", "timeout_seconds", "http_status"):
        item = value.get(key)
        if type(item) in (int, float) and 0 <= item <= 1_000_000:
            result[key] = item
    return result
