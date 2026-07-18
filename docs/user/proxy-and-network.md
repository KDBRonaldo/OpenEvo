# Proxy And Network Settings

The Preview's server profile supports remote HTTP proxy, HTTPS proxy, and
bypass-list settings. Configure them in **Remote workspace > Network proxy**
before connecting or activating a project.

## Supported Values

- **HTTP proxy** and **HTTPS proxy** must be an `http://` or `https://` origin
  with a host and optional port.
- Do not include a user name, password, path, query, or fragment in a proxy URL.
- **Bypass proxy for** is a comma-separated host list. Desktop preserves the
  required loopback exclusions used by local Daemon services.

Example:

```text
HTTP proxy:  http://proxy.example.org:8080
HTTPS proxy: http://proxy.example.org:8080
Bypass:      localhost, 127.0.0.1, research.internal
```

These settings describe networking on the remote Linux server. They apply to
supported OpenEvo-managed preparation and services; they do not reconfigure the
Mac, the SSH server, Docker daemon policy, or a pre-existing interactive shell.

## Preview Limitations

The first Preview does not provide proxy credential fields, SSH jump-host
configuration, custom package indexes, Hugging Face endpoints, or container
registry mirror controls in the user interface. A proxy URL containing user
information is rejected instead of sending those credentials.

OpenEvo does not bypass institutional network policy. The remote server still
needs access to the Codex subscription service during a run. If Docker itself
needs a proxy or registry configuration, the server administrator must
configure that outside OpenEvo before activation.

After changing a profile's proxy settings, save it and reconnect. A failed
operation reports the phase and a typed next action; use that action instead of
repeatedly changing unrelated settings.
