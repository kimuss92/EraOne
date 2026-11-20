"""Read-only subscription/session probe. --live makes two small Claude requests."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from core.codemode import configured_profile
from core.codemode_cli import ClaudeSession
from core.codemode_context import SYSTEM


async def probe(live=False):
    profile, model = configured_profile()
    identity = str(uuid.uuid4())
    client = ClaudeSession(
        profile, identity, Path("runtime/codemode-probes") / identity, model=model
    )
    await client.authenticate()
    report = {"subscription_authenticated": True, "live_generation": "not_run"}
    if live:
        chunks = []

        async def receive(text):
            chunks.append(text)

        nonce = "era" + uuid.uuid4().hex[:8]
        first = await client.generate(
            SYSTEM + f"\nRemember {nonce}. Reply with just that value. No candidate.",
            receive,
            wall_s=60,
        )
        chunks.clear()
        second = await client.generate(
            SYSTEM
            + "\nRepeat the exact value I asked you to remember in the previous turn. No candidate.",
            receive,
            wall_s=60,
        )
        assert nonce in first["reply"] and nonce in second["reply"]
        assert first.get("candidate") is None and second.get("candidate") is None
        report.update(
            live_generation="passed", exact_session_resume="passed", streaming=bool(chunks)
        )
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    asyncio.run(probe(parser.parse_args().live))
