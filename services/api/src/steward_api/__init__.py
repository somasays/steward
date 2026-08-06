"""steward-api: FastAPI gateway service (SPEC.md §8; issue #4).

Route handlers do no business logic (GUARDRAILS.md smell list, I4): they
parse requests, delegate to a `RunStore`, and shape the HTTP response. The
in-memory `RunStore` here is an M0 skeleton implementation; issue #5 swaps
in a queue-backed store behind the same `Protocol` with no handler change.
"""
