# Security policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.12.x  | :white_check_mark: |
| 0.11.x  | :white_check_mark: |
| < 0.11  | :x:                |

## Reporting a vulnerability

Please report security issues by emailing `tobiash@users.noreply.github.com`
or opening a private security advisory on GitHub:
https://github.com/tobiash/ha-intentional/security/advisories/new

Do not open a public issue for security vulnerabilities.

## Scope

Rule files are parsed as YAML and structured `observe:` trees. Jinja templates
are evaluated only for bounded scalar values under `intent`, `effect.data`, and
Alert annotations; labels, target names, field names, operator names, service
names, and Receiver configuration are not templated. Rule evaluation must not
execute arbitrary Python.

Alerting Receiver destinations use fixed schemas and restricted Home Assistant
notification capabilities. Mobile actions use actor-bound, operation-bound,
single-use HMAC capabilities; raw tokens and the HMAC secret must never appear
in storage exports, APIs, entities, diagnostics, or logs. Non-admin Alert
responses must not reveal Receiver configuration, rendered Notification
messages, delivery error detail, or secret-bearing action data.

Code execution through a Rule/API payload, Receiver allowlist bypass, mobile
capability replay or rebinding, secret/token disclosure, or unauthorized Alert
suppression is a security bug. Please report it privately.
