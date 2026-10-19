"""Prepare disposable native filesystem fixtures despite brief Windows locks."""

import os
import time


def rename_fixture(source, destination):
    deadline = time.monotonic() + 2
    while True:
        try:
            return source.rename(destination)
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in {5, 32}
                or time.monotonic() >= deadline
                or not source.exists()
                or destination.exists()
            ):
                raise
            # Only a failed, unchanged fixture preparation is retried. The actual
            # index operation/assertions still execute once; no failures are skipped.
            time.sleep(0.05)
