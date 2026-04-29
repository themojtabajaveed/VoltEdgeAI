"""VoltEdgeAI Event Intelligence Pipeline (v1).

Ingests, parses, classifies, and emits corporate-event signals (earnings,
guidance, dividends, board meetings) so DAWN/HYDRA can route on catalyst
*meaning* rather than subject-line keywords.

Operates as a separate service (`voltedge-events.service`) writing to
`data/earnings_signals_YYYY-MM-DD.json` and `data/event_intel_shadow_router_YYYY-MM-DD.json`.
v1 is shadow-only: no live router integration. See plan + design doc.
"""
