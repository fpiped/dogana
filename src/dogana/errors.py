"""Protocol refusals and storage failures."""


class DoganaError(Exception):
    """Base class for delivery ledger errors."""


class InvalidContract(DoganaError, ValueError):
    """A declaration contradicts the protocol."""


class SerializationError(InvalidContract):
    """A wire value has an unsupported representation."""


class RejectedDelivery(DoganaError, ValueError):
    """A delivery cannot establish its claimed evidence."""


class PlanningError(DoganaError, ValueError):
    """No admitted extraction can establish the requested evidence."""


class StoreError(DoganaError):
    """A storage operation failed without a protocol decision."""


class InconsistentPublication(StoreError):
    """Published rows and their persisted evidence disagree."""
