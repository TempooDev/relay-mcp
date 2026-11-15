# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability reporting ("Security" tab → "Report a vulnerability") on this repository. Include what you found, how to reproduce it and the version.

You can expect an acknowledgement within a few days. This is a small open source project maintained on a best-effort basis.

## Threat model

relay-mcp is a **local tool for a single user**. Its security boundary is:

- It listens on `127.0.0.1` and has **no authentication**. Exposing it beyond localhost is unsupported.
- A client connected to it can ask it to read files and send their contents to the configured models, and to create new files. The folder allow-list, protected names and no-overwrite rule limit what a misbehaving or prompt-injected client can reach, but they are not a sandbox.
- Content you delegate leaves your machine whenever the chosen tier points to a cloud provider.
- Tasks and results are stored unencrypted in a local SQLite file.

Reports about bypassing the path policy (traversal, symlink tricks, deny-list gaps), writing outside allowed folders, or exposing the server beyond localhost are in scope.
