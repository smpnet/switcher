# Security policy

## Reporting vulnerabilities

Email security reports to **smpnet74@gmail.com**. Please do not open public issues for unpatched vulnerabilities. We aim to acknowledge reports within 48 hours and ship a fix within 14 days for high-severity issues.

## Supported versions

Security fixes target the latest tagged release. The `main` branch may contain unreleased changes. Older tags are not patched — upgrade to the latest release if you're affected.

## Scope

**In-scope:** any switcher behavior that could clobber user data, expose credentials held in managed config directories (auth tokens, API keys), escalate privileges, or cause symlinks to point at unintended targets.

**Out-of-scope:** bugs in the AI tools whose configs switcher manages (Claude Code, GitHub Copilot CLI, OpenAI Codex CLI). Please report those upstream to the respective tool maintainers.
