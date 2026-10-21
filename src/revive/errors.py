class ReviveError(Exception):
    """An error safe to report without exception text containing private paths."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
