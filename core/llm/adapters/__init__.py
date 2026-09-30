"""Provider adapters.

THE ONE RULE: provider-specific knowledge lives here and nowhere else.

``LlmRequest.provider_options`` and ``LlmResponse.raw`` may be read inside this
package and by no other. Anything outside that reaches into them has defeated
the abstraction — at which point a second provider stops being a config change
and becomes a refactor again.

Adapters translate. They do not execute tools: they return ``ToolCall`` objects
and the deterministic layer (core/safety_guard.py -> core/pending_confirmation.py
-> the dispatch in app/era_live/tool_dispatch.py) decides what may run. An
adapter importing from ``actions/`` has broken Era's safety boundary.
"""
