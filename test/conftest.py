"""Keep HTTP retries instant while the suite is running."""

import os

os.environ.setdefault("MPT_HTTP_RETRY_BACKOFF", "0")
os.environ.setdefault("MPT_PUBLISH_WORKER", "0")
os.environ.setdefault("MPT_PUBLISH_RETRY_BACKOFF", "0")
