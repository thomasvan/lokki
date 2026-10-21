"""Container healthcheck: is the relay answering /healthz? Standard library only (the image has no curl).

`/healthz` says the relay process serves; it does not look at the cliproxy behind it (docs/relay/live-8381.md). The
URL is HEALTHCHECK_URL (compose sets it from the relay's port), default the live relay's loopback address.
Exit 0 healthy, 1 not."""
import os
import sys
import urllib.request

URL = os.environ.get("HEALTHCHECK_URL", "http://127.0.0.1:8381/healthz")
try:
    with urllib.request.urlopen(URL, timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:  # noqa: BLE001 - any failure to answer is "not healthy"
    sys.exit(1)
