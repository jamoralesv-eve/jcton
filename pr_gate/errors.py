class GateError(Exception):
    """The pull request could not be evaluated."""


class TicketNotInBranch(GateError):
    """The branch name does not contain a Linear issue identifier."""
