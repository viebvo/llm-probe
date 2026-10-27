# Security

LLM Probe is a local tool: by default its web UI listens on `127.0.0.1` only. See the section "Security notes" of the README for what
is protected and what is not (no TLS, HTTP basic auth in clear text, SSH host keys are not verified).

If you find a vulnerability, please do **not** open a public issue. Use GitHub's private reporting ("Security" tab of the repository, then "Report a vulnerability"). A short description and how to reproduce it is enough.
