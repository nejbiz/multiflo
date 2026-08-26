"""Stable public error types for the MultiFlo driver."""


class MultiFloError(Exception):
    """Base class for errors raised by this package."""


class ValidationError(MultiFloError):
    """A request is invalid and was not sent to the instrument."""


class TransportError(MultiFloError):
    """The FTDI transport failed or timed out."""


class ProtocolError(MultiFloError):
    """A packet is malformed or does not match the request."""


class DeviceError(MultiFloError):
    """The instrument returned a definite device-side error."""


class BusyError(MultiFloError):
    """The instrument cannot accept the requested operation while busy."""


class UnknownExecutionState(MultiFloError):
    """Communication was lost after a motion command may have started."""

