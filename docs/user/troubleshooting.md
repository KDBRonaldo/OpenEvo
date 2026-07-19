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

Desktop could not prepare the managed runtime. Choose **Repair** or **Retry** in
Desktop. If the report identifies missing Docker host access or insufficient
host storage, ask the server administrator to resolve that host prerequisite,
then retry in Desktop.

`project_activation_codex_cli_unavailable`

This Preview cannot install Codex on an arbitrary clean host. Ask the server
administrator to provide a host with the supported Codex CLI for the selected
remote account, then choose **Retry** in Desktop.

`project_activation_codex_subscription_auth_unavailable`

Desktop could not verify a Codex subscription for the selected remote account.
Ask the server administrator to prepare that account for the Preview, then
choose **Retry** in Desktop.

`project_activation_runtime_executable_unavailable`

The host does not provide the Docker user-container capability required by this
Preview. Ask the server administrator to provide a supported host, then choose
**Retry** in Desktop.

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

## SSH Credential Is Unavailable

This Preview uses the macOS SSH agent and never receives private-key bytes.
Ensure the configured Mac credential is available through the normal macOS
credential flow, then choose **Reconnect** in Desktop. Do not disable host-key
checking and do not open a remote shell to operate OpenEvo.

## Codex Readiness Is Unavailable

Desktop checks Codex installation and subscription readiness for the configured
remote account. If Codex works in another environment but activation fails,
review the account and host shown in Desktop, ask the server administrator to
prepare the matching account, and choose **Retry**. Desktop does not accept a
token pasted into the project form.

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

Use **Check** or **Repair** in Desktop to inspect remote storage and managed
runtime state. OpenEvo does not change Docker daemon configuration or delete
project history to make space. Do not manually edit or delete OpenEvo remote
state while the Daemon may be active; a partial cleanup can make release
identity checks fail closed.
