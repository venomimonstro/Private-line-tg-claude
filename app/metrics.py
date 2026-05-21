from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Business metrics
payments_total = Counter(
    "privateline_payments_total",
    "Total payment attempts",
    ["provider", "status"],
)
active_subscriptions = Gauge(
    "privateline_active_subscriptions",
    "Current active subscriptions",
)
vpn_key_activations = Counter(
    "privateline_vpn_key_activations_total",
    "VPN key activation attempts",
    ["result"],  # success, error
)
circuit_breaker_state = Gauge(
    "privateline_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["server_id"],
)
webhook_processing_seconds = Histogram(
    "privateline_webhook_processing_seconds",
    "Time to process payment webhooks",
    ["provider"],
)
