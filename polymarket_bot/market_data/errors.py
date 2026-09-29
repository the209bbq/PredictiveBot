"""Errors for market-data fetches that must not be treated as a usable book."""


class BookFetchError(RuntimeError):
    """Raised when a live book/trade request fails after retries."""
