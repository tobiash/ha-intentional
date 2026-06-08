# Security policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.6.x   | :white_check_mark: |
| < 0.6   | :x:                |

## Reporting a vulnerability

Please report security issues by emailing `tobiash@users.noreply.github.com`
or opening a private security advisory on GitHub:
https://github.com/tobiash/ha-intentional/security/advisories/new

Do not open a public issue for security vulnerabilities.

## Scope

Rule files are parsed as YAML and structured `observe:` trees. Jinja templates
are evaluated only for scalar values under `intent` and `effect.data`; target
names, field names, operator names, and service names are not templated. Rule
evaluation must not execute arbitrary Python. If you find a way to inject code
execution through a rule file or API payload, that's a security bug. Please
report it privately.
