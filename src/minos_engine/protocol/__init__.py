"""Protocol — Minos state retrieval and official submission contracts.

Protocol owns fetching live round state and building the immutable
``RoundProtocolSnapshot``, and it owns the official submission contract. Layer 2
returns a decision; it never holds submission credentials or side effects
(Overall spec §6). Stage 0 ships the interfaces plus a fixture-backed client.
"""
