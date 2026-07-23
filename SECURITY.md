# AlphaForge Security Policy

AlphaForge is an autonomous multi-agent quantitative **research** platform. Its
public product surface is designed around read-only investigation, isolated
experiments, deterministic Evidence, and loopback-only access.

## Supported versions

| Version | Supported |
|---|:---:|
| Latest alpha release | ✅ |

## Report a vulnerability

Please do not open a public issue for a suspected vulnerability.

1. Open a private [GitHub Security Advisory](https://github.com/xxxhhbb/AlphaForge/security/advisories/new).
2. Include reproduction steps, affected revision, impact, and a minimal proof of concept.
3. Remove API keys, tokens, market-data credentials, personal data, and proprietary datasets.

We aim to acknowledge complete reports within five business days.

## Security boundaries

AlphaForge applies these boundaries to the research product path:

- the dedicated API/UI server binds to loopback and fails closed on untrusted hosts and origins;
- research models receive filtered, read-only tools rather than unrestricted shell or filesystem access;
- generated research code is mechanically reviewed and executed in a pinned, network-disabled sandbox;
- frozen dataset identities and point-in-time availability are recorded before machine evaluation;
- official metrics are recomputed by deterministic Evidence services;
- sealed evaluation data, provider secrets, promotion, risk approval, orders, and ledgers remain outside model authority;
- a research result never creates a real order or connects a live brokerage account.

## Secrets and local configuration

- Keep provider and data-vendor credentials in `agent/.env`; it is ignored by Git.
- Never paste secrets into issues, reports, screenshots, traces, or committed fixtures.
- Use restricted keys and rotate a key immediately if it is exposed.
- AlphaForge reports and public examples must use redacted or synthetic values.

## Generated research code

Do not bypass the AlphaForge review gateway to run untrusted model-generated
code. The supported research path uses a constrained workspace, approved
libraries, frozen read-only inputs, no network, resource limits, typed output
contracts, and independent evaluation. A local user who runs generated source
outside that path assumes the same risks as running any untrusted program.

## Scope

Security reports are especially valuable for:

- path traversal or arbitrary file access;
- secret, sealed-data, or cross-branch leakage;
- sandbox escape or network access from generated code;
- authority escalation into promotion, risk, execution, or ledger services;
- bypasses of point-in-time validation or Evidence identity;
- unsafe API routes or non-loopback exposure;
- replay, idempotency, or append-only ledger violations.

Please wait for a coordinated fix before public disclosure. Reporter credit is
available in release notes unless anonymity is requested.
