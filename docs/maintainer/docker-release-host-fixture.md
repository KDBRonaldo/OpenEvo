# Docker Release-Host Fixture

`scripts/e2e/docker_release_host_fixture.py` is maintainer/E2E infrastructure,
not a product CLI. It creates one bounded Linux x86_64 container on the local
Docker Engine with:

- the release-profile Ubuntu 24.04 `linux/amd64` image pinned by the immutable
  manifest reference
  `docker.io/library/ubuntu@sha256:52df9b1ee71626e0088f7d400d5c6b5f7bb916f8f0c82b474289a4ece6cf3faf`;
- SSH published only on `127.0.0.1`;
- a maintainer-supplied public key and password authentication disabled;
- a non-root `openevo` login matching the invoking user's UID/GID;
- a Docker CLI using `/var/run/docker.sock`, verified through both CLI server
  identity and Docker `_ping`;
- one writable data root bind-mounted from a Docker-daemon-visible source to a
  path visible inside the fixture.

The Docker endpoint must be the local `unix:///var/run/docker.sock`. Use a
new dedicated canonical data directory for every fixture run; the fixture never
deletes or writes fixture metadata into it. Before a new container is created,
the directory must be empty. A non-empty root is accepted only when the same
named fixture container still exists and its schema-v2 labels carry the same
random provenance identity and exact configuration. If that container no
longer exists, old labels cannot authorize another container; use a new empty
root.
Docker socket access grants daemon-level host control, so this fixture belongs
only on a disposable or otherwise controlled maintainer E2E host.
When invoked as root, the container user defaults to `1000:1000`; only a newly
created final data-root directory is assigned to that identity. Existing data
roots are never re-owned.

```bash
python scripts/e2e/docker_release_host_fixture.py \
  --timeout-seconds 300 create \
  --public-key "$HOME/.ssh/id_ed25519.pub" \
  --data-root /absolute/path/to/openevo-e2e-data

python scripts/e2e/docker_release_host_fixture.py check
python scripts/e2e/docker_release_host_fixture.py destroy
```

When this command itself runs inside a user container connected to the host
Docker socket, pass the translated host source explicitly:

```bash
python scripts/e2e/docker_release_host_fixture.py \
  --timeout-seconds 300 create \
  --public-key "$HOME/.ssh/id_ed25519.pub" \
  --data-root /EvoLab/openevo-e2e-data \
  --docker-data-root /data2/EvoLab/openevo-e2e-data
```

The created fixture exposes the first path internally. OpenEvo Daemon then
discovers the second path from Docker self-inspect evidence; ordinary Desktop
users never configure either value.

The fixture first verifies the localhost-published SSH port. When the manager
itself runs in a sibling Docker container and therefore cannot reach the host
loopback namespace, it verifies and reports the fixture's unique Docker bridge
IPv4 address on port 22 instead. The port remains published only on host
loopback.

Each command emits one bounded JSON record. `create` is idempotent for the same
live fixture and matching provenance. A conflicting container is left
untouched. A failed new create gets a separate bounded cleanup attempt; destroy
is idempotent and removes only a correctly labelled fixture container.

The `docker_user_container_v1` fixture accepts the closed first-release set
containing Docker Engine server version `29.3.0`, API version `1.54`, server OS
`linux`, and architecture `amd64`. Any other value fails before fixture
mutation. Ready evidence records both the observed server identity and the
closed supported sets. It also records the immutable image reference, observed
image content ID, exact Ubuntu `24.04`/`noble` guest identity, and platform.
The data-root admission is recorded as either `fresh_empty` or
`same_fixture_provenance`; the random provenance value itself is not emitted.

The fixture intentionally does not pass `--hostname`. Docker's default
12-character container-ID hostname is a first-release profile invariant:
inspect must report `Config.Hostname == Id[:12]`. This value is checked again
and emitted in ready evidence so Daemon self-identification tests exercise the
same assumption as the release profile.
