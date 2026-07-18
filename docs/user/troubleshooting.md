# OpenEvo Preview Troubleshooting

OpenEvo errors include a stable `code`, a user-safe message, whether retry is
allowed, a `repair_action`, and a `next_action`. Follow `next_action` first.
When reporting a problem, include the code, Desktop and Daemon release
identities, the operation phase, and any displayed logs reference. Do not share
SSH keys, Codex credentials, full transcripts, or private research data.

## Common Typed Errors

`ssh_connection_failed`

Check the host, port, user, network, and that the correct key is loaded in the
Mac SSH agent. Then reconnect.

`host_key_verification_failed` or `core_ssh_authority_invalid`

Stop. Re-check the fingerprint with the server administrator. Do not accept an
unexplained key change.

`core_python_runtime_unavailable`

The remote architecture has no supported automatic Python path. Use a
release-supported server.

`core_python_runtime_provision_failed`

Check the remote proxy, HTTPS/TLS access, home-directory space, and inode
capacity, then retry activation.

`core_bootstrap_install_failed`

The isolated Daemon generation could not be installed. Check remote space and
network access, then use the Desktop retry action.

`managed_runtime_prepare_failed`

Confirm that Docker Engine is usable by the SSH user and has enough storage,
then retry.

`project_activation_codex_cli_unavailable`

Install the release-supported Codex CLI on the remote server so
`codex --version` works for the SSH user.

`project_activation_codex_subscription_auth_unavailable`

Sign in to Codex as the same remote SSH user and confirm `codex login status`,
then retry activation.

`project_activation_runtime_executable_unavailable`

Install or restore Docker Engine access for the SSH user.

`project_activation_runtime_image_unavailable`

Use the OpenEvo repair/retry action to prepare the packaged science runtime. Do
not upload an image manually.

`release_execution_mode_unsupported`

Select Codex subscription transcript mode. Self-Deployed is not in this
Preview.

`core_project_not_ready`

Wait for activation or the successor revision to finish, refresh, and submit
again. Do not run against the prior revision.

## Unsigned App Is Blocked

Confirm that the DMG and checksum came from the intended GitHub Release. Then
open **System Settings > Privacy & Security** and choose **Open Anyway** for
OpenEvo. This is a manual exception for an unsigned, non-notarized Preview; it
does not make the app signed.

## SSH Agent Has No Usable Key

Desktop does not prompt for a password or key file. In Terminal on the Mac:

```bash
ssh-add -l
```

If the required identity is absent, add it to the agent and reconnect. Confirm
that the same identity can access the configured remote user. Do not disable
host-key checking to work around a connection problem.

## Codex Works In One Shell But Activation Fails

OpenEvo checks `codex` and the subscription status as the configured SSH user.
On the server, verify:

```bash
codex --version
codex login status
```

The login must be a Codex subscription and its authentication state must be
readable from that user's normal Codex home. Desktop does not perform the login
and does not accept a token as a substitute.

## Connection Or Preparation Was Interrupted

Choose **Reconnect**. Desktop re-reads authoritative remote state and resumes or
retries only the relevant operation. If **Cancel operation** was used, wait
until cancellation settles before starting another connection or activation.

Closing Desktop does not cancel a remote science session. After reopening, make
the SSH identity available to the agent, reconnect, and let the timeline replay.
If Desktop says a mutation outcome is unknown, leave it open while it confirms
the original idempotent action. Repeated clicks do not create a valid shortcut.

## Session Is Waiting For A Revision

Cross-session evolution is atomic. The next session cannot start until the
previous session's complete successor revision is active. A
`required_revision_uncommitted` status means OpenEvo is still committing that
revision. Wait and refresh; do not disable evolution merely to force a stale
run.

If evolution fails, the previous revision remains active but the next draft is
not silently submitted against it. Use the error's retry or repair action.

## Cancellation

**Cancel session** requests a remote cancellation. The session may briefly show
**Cancelling** while the Daemon reconciles the runtime. Only the terminal
**Cancelled** state confirms the outcome. A cancelled or failed session does
not report a successful successor revision.

## Disk Or Runtime Problems

Check free space in the remote user's home and in container storage. OpenEvo
does not change Docker daemon configuration or delete project history to make
space. Do not manually replace files under remote `~/.openevo` while the Daemon
may be active; a partial manual cleanup can make release identity checks fail
closed.
