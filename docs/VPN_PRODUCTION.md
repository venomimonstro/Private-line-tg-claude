# Private Line VPN production architecture

## Objective

A paid Telegram user must receive an import-ready VLESS + TCP + REALITY +
XTLS-Vision profile which has already passed the same data-plane path from the
control-plane host. API/DB state alone is never treated as proof of VPN health.

## Supported production stack

- Marzban: v0.8.4, exact digest pinned per CPU architecture.
- Xray server: v26.6.27, exact container digest pinned.
- Transport: exactly one VLESS inbound, TCP, REALITY, port 443.
- Flow: xtls-rprx-vision.
- REALITY target: one TLS 1.3 + HTTP/2 host, default dl.google.com:443.
- Management API: Marzban TCP/2096, exposed by UFW only to the control-plane IP.
- Node administration: stored encrypted SSH credentials; no interactive production password prompts.
- Client profile source of truth: UUID from Marzban DB + public host from /api/hosts + Reality key/SNI/shortId from the verified static inbound.

Xray 24.x and 25.x+ have a documented REALITY compatibility break. Current
third-party clients also move independently. Private Line therefore pins its
owned server/probe/Windows/Chrome Xray compatibility baseline to 26.6.27 and
explicitly sets minClientVer=0.0.0 for third-party client interoperability.

## Golden node invariant

scripts/converge_vpn_node.sh is the only implementation allowed to mutate the VPN node runtime configuration.

It must preserve existing Reality credentials and user UUIDs, ensure one
VLESS/TCP/REALITY inbound on 443, validate the exact candidate with the pinned
Xray core before applying it, keep the management API firewall-restricted,
restart only on actual runtime drift, and verify the live process afterward.

Legacy node updater and Reality migration entrypoints are wrappers around this
single converger and must never re-implement node mutation.

## Admission gate

A node becomes active/selectable only after golden convergence, reconciliation,
a brand-new random Marzban user created without an Xray restart, real HTTPS
egress through that exact fresh credential, a persistent synthetic E2E check,
and, when present, a paid-key E2E check.

A DB row, HTTP 200, open TCP/443, running Xray process, or syntactically valid
VLESS URL is never sufficient by itself. Failure quarantines the node for new
assignments while preserving already-issued customer credentials.

## Runtime health

The control-plane systemd timer runs a persistent synthetic E2E every five
minutes. It does not rotate credentials or restart Xray. Failed nodes are
removed from new-user admission and the operator is alerted.

## Subscription redundancy

Tariff max_keys is also the maximum number of distinct healthy VPN nodes to
provision for one subscription. When two healthy nodes exist and the tariff
allows two keys, activation creates a primary and a backup profile. Renewal
preserves valid profiles and fills missing backups without deleting the primary.

The public subscription endpoint returns every active profile, so compatible
clients can select another route when one node is unavailable.

## 99.99% SLO

99.99% corresponds to about 4.4 minutes of unavailable service in a 30.44-day
month. One VPS, one provider, one public IP and one route cannot honestly meet
this target because infrastructure failure remains a single point of failure.

For a real four-nines target deploy at least two dedicated VPN nodes on
independent providers/ASNs or regions, keep both continuously admitted by the
real E2E gate, and give eligible users both profiles.

## Release rule

The only supported production entrypoint is:

    cd /opt/private-line && bash update.sh --force-nodes

A release is not successful until shell/Python/unit preflight and the real
fresh-user, synthetic and paid-key E2E checks have all passed.