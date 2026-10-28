# Security policy

## Reporting a vulnerability

Please report vulnerabilities by email to
**[security@bibliotekarien.se](mailto:security@bibliotekarien.se)**, not in a
public issue. Swedish or English is fine.

Helpful to include:

- what you found and where (endpoint, file, version or commit),
- how to reproduce it,
- what an attacker could do with it.

Please do not access data that is not yours, and do not run denial-of-service
tests against the public service. The same contact address is published at
`/.well-known/security.txt` on our services (RFC 9116).

## Supported versions

Only the latest commit on `main` is supported; that is what we deploy.

## What we check automatically

Every pull request and every commit on `main` runs, besides the tests
(`.github/workflows/tests.yml`):

- a secret scan of the **entire** git history (gitleaks),
- a scan of `uv.lock` for known vulnerabilities (osv-scanner),
- and, on `main`, a CycloneDX SBOM of the dependencies, kept as a run artifact.

See `.github/workflows/security-checks.yml`. Dependabot proposes dependency
updates weekly.
