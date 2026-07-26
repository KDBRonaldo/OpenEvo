# OpenEvo Preview Troubleshooting

OpenEvo errors include a stable `code`, a user-safe message, whether retry is
allowed, a `repair_action`, and a `next_action`. Follow `next_action` first.
When reporting a problem, include the code, Desktop and Daemon release
identities, the operation phase, and the exported diagnostics JSON. Do not share
SSH keys, Codex credentials, full transcripts, or private research data.

## Desktop Startup Diagnostics

While startup is still in progress, open **Diagnostics** and choose **View
logs** to review local startup events. After the attempt succeeds or fails,
**Reveal in Finder** opens the log directory and **Export diagnostics** creates
the support artifact. Support requests the exported JSON rather than
screenshots, raw terminal output, or copies of local log files.

## Common Typed Errors

`ssh_connection_failed`

Run `/usr/bin/ssh <selected-alias>` from the same Mac account. Check the alias's
normal OpenSSH configuration, network route, identity/agent/Keychain, and any
native prompt, then reconnect. Do not replace the alias with manually entered
IP/user/port fields in Desktop.

`ssh_host_key_changed` or `core_ssh_authority_invalid`

Stop. Re-check the effective alias and fingerprint with the server
administrator. Do not accept or automatically repair an unexplained key change.

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

`project_not_ready`

Wait for the complete successor Project Head to become active, refresh, and
submit again. Desktop will not admit a Task against a stale or partial head.

## Unsigned App Is Blocked

Confirm that the DMG and checksum came from the intended GitHub Release. Then
open **System Settings > Privacy & Security** and choose **Open Anyway** for
OpenEvo. This is a manual exception for an unsigned, non-notarized Preview; it
does not make the app signed. After verifying the checksum, command-line
recovery may instead remove quarantine from only the installed app:

```bash
xattr -dr com.apple.quarantine "/Applications/OpenEvo Desktop.app"
```

Do not remove quarantine from a parent directory or an unverified download.

## SSH Credential Is Unavailable

This Preview delegates to system OpenSSH and never receives private-key bytes.
Ensure the selected alias can use its configured `IdentityFile`, agent,
Keychain, password, or key-passphrase flow, then choose **Reconnect**. Do not
disable host-key checking or open a remote shell to operate OpenEvo.

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

Closing Desktop does not rewrite a remote Task. After reopening, make the
configured system OpenSSH identity available, reconnect the alias, and refresh
the authoritative state. If Desktop says a mutation outcome is unknown, leave
it open while it confirms the original idempotent action. Repeated clicks do not
create a valid shortcut.

## Task Is Waiting For A Successor

Cross-Task evolution is atomic. The next Task cannot be admitted until the
previous Task's complete successor Project Head is active. A waiting/not-ready
status means OpenEvo is still preparing or committing that successor. Wait and
refresh; do not disable evolution merely to force a stale Task.

If evolution fails, the previous revision remains active but the next draft is
not silently submitted against it. Use the error's retry or repair action.

## Cancellation

**Cancel Task** requests remote cancellation. The Task may briefly show
**Cancelling** while the Daemon reconciles the runtime. Only terminal state
confirms the outcome. A failed or cancelled Task may append an infrastructure
Attempt under the same immutable admission; it does not fabricate a successful
successor.

## Disk Or Runtime Problems

Use **Check** or **Repair** in Desktop to inspect remote storage and managed
runtime state. OpenEvo does not change Docker daemon configuration or delete
project history to make space. Do not manually edit or delete OpenEvo remote
state while the Daemon may be active; a partial cleanup can make release
identity checks fail closed.
