"""Errors for official YouTube and TikTok publishing."""


class PublishRejected(Exception):
    """The request cannot be queued. Safe to show to the user."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PublishError(Exception):
    """A provider rejected the upload. Not retried."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class TransientPublishError(PublishError):
    """A network or 5xx failure that the queue may try again."""


class AuthExpired(PublishError):
    """The refresh token is no longer accepted. The account needs a new login."""


class QuotaExceeded(PublishError):
    """The local daily cap, or the provider's quota, is used up."""
