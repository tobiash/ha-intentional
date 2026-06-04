# Security policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please report security issues by emailing `tobiash@users.noreply.github.com`
or opening a private security advisory on GitHub:
https://github.com/tobiash/ha-intentional/security/advisories/new

Do not open a public issue for security vulnerabilities.

## Scope

The `when:` expression parser and evaluator are designed to be safe against
injection attacks. They parse to an AST and evaluate that, never using
`eval()` or `exec()` on user input. If you find a way to inject Python
execution through a rule file, that's a security bug — please report it.
