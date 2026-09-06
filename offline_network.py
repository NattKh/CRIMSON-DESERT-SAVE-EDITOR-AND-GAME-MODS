"""Network guard for the offline standalone editor.

The editor intentionally has no HTTP client.  Legacy community modules retain
their local-data features, but any old download action receives this local-only
error before a connection can be opened.
"""


class OfflineNetworkDisabled(RuntimeError):
    """Raised when retired online functionality is invoked."""


# Compatibility alias for old exception handlers.  It has no networking role.
URLError = OfflineNetworkDisabled


class Request:
    """Compatibility placeholder for retired urllib call sites."""

    def __init__(self, url, *args, **kwargs):
        self.full_url = url


def urlopen(*args, **kwargs):
    raise OfflineNetworkDisabled(
        "This offline build never connects to the internet. "
        "Use local game scanning or local data files instead."
    )
