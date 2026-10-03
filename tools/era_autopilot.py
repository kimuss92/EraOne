"""Inspect an Autopilot decision without executing it or contacting any model."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.autopilot import classify  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(classify(" ".join(sys.argv[1:])).as_dict(), indent=2, ensure_ascii=False))
