# Security Policy

## Reporting a vulnerability

Please do not post tokens, credentials, or exploitable details in public issues. Contact the repository maintainer privately through GitHub to report a security issue. Include impact, reproduction steps, and a suggested mitigation where possible.

## Secret handling

- Keep `.env` out of version control.
- Rotate leaked Telegram bot tokens immediately through BotFather.
- Do not log bot tokens or environment variable values.
- Keep Python and system packages updated and review dependency changes before deploying.
