# Security policy

Report suspected credential exposure or archive/path traversal vulnerabilities privately to the
maintainers. Do not open a public issue containing API keys or private paper content.

ChemEx-Lit accepts credentials only through named environment variables. Release automation must
use PyPI Trusted Publishing rather than a long-lived PyPI token.
