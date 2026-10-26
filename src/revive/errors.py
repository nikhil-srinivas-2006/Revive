class ReviveError(Exception):
    """An error safe to report without exception text containing private paths."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def recovery_action(code):
    if code == "RAM_ADMISSION":
        return "Waiting for memory without consuming further attempts. Check status resources; close memory-heavy applications. The monitor must be running to retry automatically."
    if code == "RAM_LIMIT":
        return "Retry after more memory is available; use smaller inputs or disable optional AI stages if needed."
    if code == "WORKER_TIMEOUT":
        return "Review processing time and enabled stages before retrying; the worker exceeded its deadline."
    if code in {
        "MODEL_MISSING",
        "MODEL_HASH_MISMATCH",
        "MODEL_INCOMPATIBLE",
        "BACKEND_UNSUPPORTED",
    }:
        return "Check doctor and models status; explicitly set up the dependencies and checkpoint for each enabled stage."
    if code == "DISK_LOW":
        return "Free space on the output/application drive, then retry."
    if code in {
        "SOURCE_CORRUPT",
        "FORMAT_UNSUPPORTED",
        "BIT_DEPTH_UNSUPPORTED",
        "IMAGE_LIMIT",
    }:
        return "Inspect the source format, precision and limits; provide a supported copy without changing the original."
    if code in {"PUBLISH_CONFLICT", "OUTPUT_INVALID"}:
        return "Preserve existing derivatives and inspect the manifest/output before retrying."
    if code == "INTERRUPTED":
        return "The job remains retryable; start the monitor to resume processing."
    return "Inspect the incident/log; stop the monitor before using retry JOB_ID for a failed job."
