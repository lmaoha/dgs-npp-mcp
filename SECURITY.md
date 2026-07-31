# Security policy

## Local-only design

The broker listens only on `127.0.0.1`. It has no remote authentication layer
and must not be exposed through port forwarding, a reverse proxy, or a network
relay.

The MCP client can ask the bridge to read or modify files that the current
Windows account and the configured Notepad++ integration can access. Configure
the MCP client's workspace and approval rules accordingly.

## Protected information

This project does not grant permission to access, decrypt, disclose, or upload
protected information. Use it only for files you are authorized to access and
only with AI/model providers approved by the data owner and your organization.
Search results, context snippets, tool responses, and edits may be sent to the
configured MCP client or model provider.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository:
https://github.com/lmaoha/dgs-npp-mcp/security/advisories/new

Do not include real protected documents, credentials, or proprietary source in
a vulnerability report. Use a minimal synthetic reproduction.
