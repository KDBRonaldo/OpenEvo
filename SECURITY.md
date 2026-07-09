# Security Policy

OpenEvo is pre-release. Please do not publish exploitable security details
publicly before maintainers have had a chance to respond.

## Reporting

Report suspected vulnerabilities through a private GitHub security advisory for
the repository, or contact the maintainers privately if advisories are not
available.

Include:

- affected commit or release artifact;
- reproduction steps;
- expected impact;
- logs or payloads with secrets removed;
- whether the issue affects Desktop, Core Backend, remote deployment, runtime
  containers, artifact handling, or release packaging.

## Secret Handling

OpenEvo Desktop should store secret references rather than raw credentials in
project config. Core Backend and sidecar logs must not expose API keys, SSH
private keys, access tokens, or subscription auth files. If you find a path that
logs or packages secrets, treat it as security-sensitive.
