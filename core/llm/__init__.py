"""Provider-neutral LLM layer for Era One.

Nothing in here talks to a provider SDK directly except the future
``adapters/`` package. ``toolspec`` is the first module: it owns the ONE
canonical description of Era's tool surface and emits each provider's dialect
from it, so the 42 declarations never have to be maintained twice.
"""
