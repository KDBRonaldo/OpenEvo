# Proxy And Network Settings

OpenEvo Desktop 0.1.10 delegates SSH routing to the Mac user's system OpenSSH
configuration. Configure SSH jump hosts or proxy commands in `~/.ssh/config`,
then select that literal alias in Desktop.

Examples:

```sshconfig
Host my-openevo-server
    HostName server.internal
    User research-user
    ProxyJump bastion.example.org
```

or, where required by institutional policy:

```sshconfig
Host my-openevo-server
    HostName server.internal
    User research-user
    ProxyCommand /usr/bin/nc -X connect -x proxy.example.org:8080 %h %p
```

System `/usr/bin/ssh <alias>` remains authoritative for these values. OpenEvo
does not copy or flatten them and does not provide a separate IP/user/port or
SSH proxy form.

## Remote HTTPS And Container Networking

The remote server still needs outbound access required by Codex Subscription,
Daemon preparation, and the managed runtime. This Preview does not expose remote
HTTP/HTTPS proxy credentials or Docker registry-mirror settings in the Desktop
v2 project/profile UI. If the environment needs them, the server administrator
must configure the supported host/runtime policy before connection.

Do not place proxy credentials in an SSH alias, Task objective, or Desktop
diagnostics. OpenEvo does not bypass institutional policy and does not modify
the Mac network stack, SSH server, Docker daemon, global shell profiles, custom
package indexes, or container registry configuration.
