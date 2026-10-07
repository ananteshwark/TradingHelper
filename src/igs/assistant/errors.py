"""Assistant control-flow exceptions without importing the optional model SDK."""

class AssistantUnavailable(RuntimeError):
    """Off, not installed, no credentials, or over budget: nothing was sent."""


class BudgetExceeded(AssistantUnavailable):
    pass


class AssistantError(RuntimeError):
    """The API answered with an error, or declined the request."""


