"""Stable public error types for the MultiFlo driver."""


class MultiFloError(Exception):
    """Base class for errors raised by this package."""


class ValidationError(MultiFloError):
    """A request is invalid and was not sent to the instrument."""


class TransportError(MultiFloError):
    """The FTDI transport failed or timed out."""


class ProtocolError(MultiFloError):
    """A packet is malformed or does not match the request.

    This is a wire-level fault. It is safe to purge and retry a read-only
    command after one, which is why it is kept distinct from
    `PreconditionError`.
    """


class PreconditionError(MultiFloError):
    """A guard refused the request before it reached the instrument.

    Missing operator confirmation, a serial that is not on the motion
    allowlist, a cassette mismatch, an absent pump, or a device that is not
    ready. Retrying one of these can never help, so it is deliberately a
    sibling of `ProtocolError` rather than a subclass: `_motion_exchange`
    catches wire faults and must not catch these.
    """


class DeviceError(MultiFloError):
    """The instrument returned a definite device-side error."""


class BusyError(MultiFloError):
    """The instrument cannot accept the requested operation while busy."""


class UnknownExecutionState(MultiFloError):
    """Communication was lost after a motion command may have started."""

