"""Exception hierarchy. CLI maps these to exit codes: UsageError 3, everything else 2."""


class TplinkError(Exception):
    """Base for all errors raised by this package."""


class UsageError(TplinkError):
    """Caller passed something invalid (bad value, ambiguous node name, missing snapshot...).

    `query` + `candidates` let the CLI suggest close matches instead of dumping every option.
    """

    def __init__(self, message: str, query: str | None = None, candidates: list[str] | None = None, what: str = "value"):
        super().__init__(message)
        self.query, self.candidates, self.what = query, candidates or [], what


class AuthError(TplinkError):
    """Login failed: wrong password, router unreachable, or temporary lockout after failed logins."""


class RouterError(TplinkError):
    """Router rejected a request. Its error body is encrypted, so the cause can't be read."""

    def __init__(self, endpoint: str, op: str, fields: list[str], cause: str = ""):
        self.endpoint, self.op, self.fields, self.cause = endpoint, op, fields, cause
        super().__init__(
            f"router rejected endpoint={endpoint} op={op} fields={fields}. Usually an unknown endpoint/op, "
            "a missing/invalid field, or a write not allowed in the current mode (see references/api.md)")


class UnreachableError(TplinkError):
    """Router didn't accept a TCP connection quickly (wrong TPLINK_HOST, not on that network, router down)."""


class SpeedTestError(TplinkError):
    """The internet speed test (third-party server) failed; not a router problem."""

    def __init__(self, message: str, retry_after_s: int | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UnsupportedError(TplinkError):
    """The connected model/firmware doesn't offer this feature (see `tpctl capabilities`)."""
