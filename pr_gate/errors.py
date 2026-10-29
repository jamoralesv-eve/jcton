class GateError(Exception):
    """The branch could not be evaluated."""


class TicketNotInBranch(GateError):
    """The branch name does not contain a Linear issue identifier."""
