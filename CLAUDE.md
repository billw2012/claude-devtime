# claude-devtime

This repository is the devtime Claude Code plugin (usage dashboard, alerts, token-saving tools).
If the user asks you to set it up or install it, follow INSTALL.md. Tests: `python -m unittest discover -s tests`;
technical reference: docs/reference.md.

## Releasing

Bump `version` in `.claude-plugin/plugin.json` with every pushed change users should get: `claude plugin update`
compares versions and ignores new commits under the same version.
