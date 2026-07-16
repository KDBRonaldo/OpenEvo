use std::collections::{HashMap, VecDeque};
use std::ffi::CString;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::ops::Deref;
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, TryLockError};
use std::thread;
use std::time::{Duration, Instant};

use hmac::{Hmac, Mac};
use rand::rngs::OsRng;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;
use tempfile::TempDir;

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("OpenEvo Desktop native sidecar FD execution supports only Linux and macOS");

const BUNDLED_SIDECAR_BINARY: &str = "openevo-desktop-sidecar";
const NATIVE_SIDECAR_PROTOCOL: &str = "openevo-native-sidecar-v1";
const DESKTOP_LOCAL_API_NAME: &str = "openevo-desktop-local-api";
const DESKTOP_LOCAL_API_OPENAPI_SHA256: &str =
    "60cd51f9ab1e7b1140747b9cc5d3760fad32204e4e5c399b608bb5d406172777";
const RENDERER_READY_MARKER: &str = "OPENEVO_DESKTOP_RENDERER_READY_V1";
const SIDECAR_PROCESS_MARKER: &str = "OPENEVO_DESKTOP_SIDECAR_PROCESS_V1";
const LEGACY_DESKTOP_SHELL_ROUTE: &str = "/openevo-api/desktop/shell";
const NATIVE_SESSION_PROBE_ROUTE: &str = "/openevo-native/session";
const NATIVE_WORKSPACE_IMPORT_ROUTE: &str = "/openevo-native/workspace-imports";
const NATIVE_WORKSPACE_CANCEL_ROUTE: &str = "/openevo-native/workspace-imports/cancel";
const NATIVE_WORKSPACE_DISCARD_ROUTE: &str = "/openevo-native/workspace-imports/discard";
const NATIVE_SESSION_HEADER: &str = "X-OpenEvo-Desktop-Session";
const NATIVE_HANDOFF_HEADER: &str = "X-OpenEvo-Native-Handoff";
const NATIVE_LISTENER_FD_ENV: &str = "OPENEVO_NATIVE_LISTENER_FD";
const NATIVE_EXECUTABLE_FD_ENV: &str = "OPENEVO_NATIVE_EXECUTABLE_FD";
const NATIVE_EXECUTABLE_PATH_ENV: &str = "OPENEVO_NATIVE_EXECUTABLE_PATH";
const PYINSTALLER_PRIVATE_ENV_PREFIX: &[u8] = b"_PYI_";
const PYINSTALLER_RESET_ENVIRONMENT: &str = "PYINSTALLER_RESET_ENVIRONMENT";
const INHERITED_LISTENER_FD: libc::c_int = 3;
const INHERITED_EXECUTABLE_FD: libc::c_int = 4;
const INSTANCE_ID_BYTES: usize = 16;
const READINESS_KEY_BYTES: usize = 32;
const SESSION_TOKEN_BYTES: usize = 32;
const HANDOFF_TOKEN_BYTES: usize = 32;
const WORKSPACE_CANCELLATION_TOKEN_BYTES: usize = 32;
const NATIVE_INSTANCE_FRAME_MAX_BYTES: usize = 512;
const SIDECAR_STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const SIDECAR_HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(100);
const SIDECAR_HEALTH_CONNECT_TIMEOUT: Duration = Duration::from_millis(250);
const SIDECAR_HEALTH_RESPONSE_MAX_BYTES: usize = 4096;
const NATIVE_WORKSPACE_RESPONSE_MAX_BYTES: usize = 8192;
const NATIVE_WORKSPACE_IMPORT_TIMEOUT: Duration = Duration::from_secs(300);
const NATIVE_WORKSPACE_CANCEL_TIMEOUT: Duration = Duration::from_secs(1);
const NATIVE_WORKSPACE_CANCEL_GRACE: Duration = Duration::from_secs(3);
const NATIVE_WORKSPACE_IO_POLL: Duration = Duration::from_millis(50);
const MAX_PENDING_WORKSPACE_IMPORTS: usize = 64;
const MAX_CANCELLED_PICKER_ACTIONS: usize = 64;
const RUN_RETRY_RECOVERY_MAX_BYTES: usize = 1024 * 1024;
const RUN_RETRY_RECOVERY_FILE_NAME: &str = ".7f3d8b24c1a94762";
const RUN_RETRY_RECOVERY_TEMP_PREFIX: &str = ".7f3d8b24c1a94762.tmp.";
const RUN_RETRY_RECOVERY_LOCK_FILE_NAME: &str = ".c41d73e981bf4a56";
const RUN_RETRY_RECOVERY_LOCK_TIMEOUT: Duration = Duration::from_secs(3);
const RUN_RETRY_RECOVERY_LOCK_POLL_INTERVAL: Duration = Duration::from_millis(10);
const SIDECAR_TERM_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_KILL_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_STOP_POLL_INTERVAL: Duration = Duration::from_millis(20);
const SIDECAR_MONITOR_POLL_INTERVAL: Duration = Duration::from_millis(100);
const SIDECAR_STATE_LOCK_TIMEOUT: Duration = Duration::from_secs(3);
const SIDECAR_EXIT_EMERGENCY_TERM_GRACE: Duration = Duration::from_millis(250);
#[cfg(any(test, target_os = "macos"))]
const MACOS_PROCESS_GROUP_LIST_RETRIES: usize = 4;
#[cfg(any(test, target_os = "macos"))]
const MACOS_PROCESS_GROUP_MAX_PIDS: usize = 1_048_576;
const RELEASE_FORBIDDEN_SIDECAR_ENV: [&str; 8] = [
    "OPENEVO_DESKTOP_SIDECAR_COMMAND",
    "OPENEVO_DESKTOP_SIDECAR_PROGRAM",
    "OPENEVO_DESKTOP_SIDECAR_ARGS_JSON",
    "OPENEVO_DESKTOP_SIDECAR_WORKDIR",
    "OPENEVO_DESKTOP_BACKEND_BASE_URL",
    NATIVE_LISTENER_FD_ENV,
    NATIVE_EXECUTABLE_FD_ENV,
    NATIVE_EXECUTABLE_PATH_ENV,
];

type HostResult<T> = Result<T, NativeHostError>;
type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Debug, serde::Serialize)]
struct NativeHostError {
    code: String,
    message: String,
}

impl NativeHostError {
    fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.to_string(),
            message: message.to_string(),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LaunchPolicy {
    #[cfg_attr(debug_assertions, allow(dead_code))]
    Release,
    #[cfg(debug_assertions)]
    Debug,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PackagedSourceOwnerPolicy {
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    MatchLoadedExecutable(u32),
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    RootOrEffectiveUser(u32),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TrustedDirectoryPolicy {
    #[cfg_attr(target_os = "macos", allow(dead_code))]
    Strict,
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    MacOsBundle,
}

#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_READ_DATA: u64 = 1 << 1;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_WRITE_DATA: u64 = 1 << 2;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_EXECUTE: u64 = 1 << 3;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_DELETE: u64 = 1 << 4;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_APPEND_DATA: u64 = 1 << 5;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_DELETE_CHILD: u64 = 1 << 6;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_READ_ATTRIBUTES: u64 = 1 << 7;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_WRITE_ATTRIBUTES: u64 = 1 << 8;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_READ_EXTATTRIBUTES: u64 = 1 << 9;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_WRITE_EXTATTRIBUTES: u64 = 1 << 10;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_READ_SECURITY: u64 = 1 << 11;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_WRITE_SECURITY: u64 = 1 << 12;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_CHANGE_OWNER: u64 = 1 << 13;
#[cfg(any(test, target_os = "macos"))]
const ACL_PERMISSION_SYNCHRONIZE: u64 = 1 << 20;
#[cfg(any(test, target_os = "macos"))]
const ACL_READ_ONLY_PERMISSIONS: u64 = ACL_PERMISSION_READ_DATA
    | ACL_PERMISSION_EXECUTE
    | ACL_PERMISSION_READ_ATTRIBUTES
    | ACL_PERMISSION_READ_EXTATTRIBUTES
    | ACL_PERMISSION_READ_SECURITY
    | ACL_PERMISSION_SYNCHRONIZE;
#[cfg(any(test, target_os = "macos"))]
const ACL_MUTATING_PERMISSIONS: u64 = ACL_PERMISSION_WRITE_DATA
    | ACL_PERMISSION_DELETE
    | ACL_PERMISSION_APPEND_DATA
    | ACL_PERMISSION_DELETE_CHILD
    | ACL_PERMISSION_WRITE_ATTRIBUTES
    | ACL_PERMISSION_WRITE_EXTATTRIBUTES
    | ACL_PERMISSION_WRITE_SECURITY
    | ACL_PERMISSION_CHANGE_OWNER;

#[cfg(any(test, target_os = "macos"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExtendedAclTag {
    Allow,
    Deny,
    Unknown,
}

#[cfg(any(test, target_os = "macos"))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ExtendedAclEntry {
    tag: ExtendedAclTag,
    permissions: u64,
}

#[cfg(any(test, target_os = "macos"))]
fn validate_extended_acl_entries(entries: &[ExtendedAclEntry]) -> HostResult<()> {
    if entries.iter().any(|entry| {
        entry.tag == ExtendedAclTag::Unknown
            || (entry.tag == ExtendedAclTag::Allow
                && (entry.permissions & ACL_MUTATING_PERMISSIONS != 0
                    || entry.permissions & !(ACL_READ_ONLY_PERMISSIONS | ACL_MUTATING_PERMISSIONS)
                        != 0))
    }) {
        return Err(packaged_path_error());
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn validate_anchored_extended_acl(_file: &File) -> HostResult<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn validate_anchored_extended_acl(file: &File) -> HostResult<()> {
    use std::ffi::c_void;
    use std::ptr;

    type Acl = *mut c_void;
    type AclEntry = *mut c_void;

    const ACL_TYPE_EXTENDED: libc::c_int = 0x0000_0100;
    const ACL_FIRST_ENTRY: libc::c_int = 0;
    const ACL_NEXT_ENTRY: libc::c_int = -1;
    const ACL_EXTENDED_ALLOW: libc::c_int = 1;
    const ACL_EXTENDED_DENY: libc::c_int = 2;

    unsafe extern "C" {
        fn acl_get_fd_np(fd: libc::c_int, acl_type: libc::c_int) -> Acl;
        fn acl_get_entry(acl: Acl, entry_id: libc::c_int, entry: *mut AclEntry) -> libc::c_int;
        fn acl_get_tag_type(entry: AclEntry, tag: *mut libc::c_int) -> libc::c_int;
        fn acl_get_permset_mask_np(entry: AclEntry, mask: *mut u64) -> libc::c_int;
        fn acl_free(object: *mut c_void) -> libc::c_int;
    }

    struct AclGuard(Acl);

    impl Drop for AclGuard {
        fn drop(&mut self) {
            unsafe {
                acl_free(self.0);
            }
        }
    }

    let acl = unsafe { acl_get_fd_np(file.as_raw_fd(), ACL_TYPE_EXTENDED) };
    if acl.is_null() {
        return Err(packaged_path_error());
    }
    let _guard = AclGuard(acl);
    let mut entries = Vec::new();
    let mut entry_id = ACL_FIRST_ENTRY;
    loop {
        let mut entry: AclEntry = ptr::null_mut();
        match unsafe { acl_get_entry(acl, entry_id, &mut entry) } {
            0 => break,
            1 => {}
            _ => return Err(packaged_path_error()),
        }
        let mut raw_tag = 0;
        let mut permissions = 0_u64;
        if unsafe { acl_get_tag_type(entry, &mut raw_tag) } == -1
            || unsafe { acl_get_permset_mask_np(entry, &mut permissions) } == -1
        {
            return Err(packaged_path_error());
        }
        let tag = match raw_tag {
            ACL_EXTENDED_ALLOW => ExtendedAclTag::Allow,
            ACL_EXTENDED_DENY => ExtendedAclTag::Deny,
            _ => ExtendedAclTag::Unknown,
        };
        entries.push(ExtendedAclEntry { tag, permissions });
        entry_id = ACL_NEXT_ENTRY;
    }
    validate_extended_acl_entries(&entries)
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct FileIdentity {
    device: u64,
    inode: u64,
    mode: u32,
    links: u64,
    owner: u32,
    size: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

#[cfg(target_os = "linux")]
const FILE_TYPE_MASK: u32 = libc::S_IFMT;
#[cfg(target_os = "macos")]
const FILE_TYPE_MASK: u32 = libc::S_IFMT as u32;
#[cfg(target_os = "linux")]
const DIRECTORY_FILE_TYPE: u32 = libc::S_IFDIR;
#[cfg(target_os = "macos")]
const DIRECTORY_FILE_TYPE: u32 = libc::S_IFDIR as u32;
#[cfg(target_os = "linux")]
const REGULAR_FILE_TYPE: u32 = libc::S_IFREG;
#[cfg(target_os = "macos")]
const REGULAR_FILE_TYPE: u32 = libc::S_IFREG as u32;
#[cfg(target_os = "linux")]
const SYMLINK_FILE_TYPE: u32 = libc::S_IFLNK;
#[cfg(target_os = "macos")]
const SYMLINK_FILE_TYPE: u32 = libc::S_IFLNK as u32;
#[cfg(target_os = "linux")]
const STICKY_MODE_BIT: u32 = libc::S_ISVTX;
#[cfg(target_os = "macos")]
const STICKY_MODE_BIT: u32 = libc::S_ISVTX as u32;

#[cfg(any(test, target_os = "macos"))]
fn same_identity_after_optional_unlink(actual: &FileIdentity, expected: &FileIdentity) -> bool {
    if actual.links == expected.links {
        return actual == expected;
    }
    if expected.links != 1 || actual.links != 0 {
        return false;
    }
    let mut normalized = actual.clone();
    normalized.links = expected.links;
    // unlink(2) changes ctime even though the open inode and its bytes are unchanged.
    normalized.changed_seconds = expected.changed_seconds;
    normalized.changed_nanoseconds = expected.changed_nanoseconds;
    &normalized == expected
}

#[derive(Debug)]
struct VerifiedExecutableFile {
    file: File,
    identity: FileIdentity,
    digest: [u8; 32],
}

#[derive(Debug)]
struct PrivateLaunchDirectory {
    temp_dir: TempDir,
    directory: File,
    identity: FileIdentity,
    executable_identity: Option<FileIdentity>,
}

impl PrivateLaunchDirectory {
    fn create() -> HostResult<Self> {
        let temp_dir = tempfile::Builder::new()
            .prefix("openevo-sidecar-")
            .disable_cleanup(true)
            .tempdir()
            .map_err(|_| private_sidecar_error())?;
        fs::set_permissions(temp_dir.path(), fs::Permissions::from_mode(0o700))
            .map_err(|_| private_sidecar_error())?;
        validate_private_launch_dir(temp_dir.path())?;
        let directory = open_directory(temp_dir.path()).map_err(|_| private_sidecar_error())?;
        let identity = file_identity(&directory).map_err(|_| private_sidecar_error())?;
        Ok(Self {
            temp_dir,
            directory,
            identity,
            executable_identity: None,
        })
    }

    fn path(&self) -> &Path {
        self.temp_dir.path()
    }

    fn validate(&self) -> HostResult<()> {
        validate_private_launch_dir(self.path())?;
        let fd_identity = file_identity(&self.directory).map_err(|_| private_sidecar_error())?;
        let path_identity = fs::symlink_metadata(self.path())
            .map(|metadata| file_identity_from_metadata(&metadata))
            .map_err(|_| private_sidecar_error())?;
        if fd_identity != path_identity
            || fd_identity.device != self.identity.device
            || fd_identity.inode != self.identity.inode
        {
            return Err(private_sidecar_error());
        }
        Ok(())
    }
}

impl Drop for PrivateLaunchDirectory {
    fn drop(&mut self) {
        if self.validate().is_ok() {
            #[cfg(target_os = "macos")]
            {
                let name = CString::new(BUNDLED_SIDECAR_BINARY).expect("constant has no NUL");
                if source_identity_at_optional(&self.directory, &name)
                    .ok()
                    .flatten()
                    .as_ref()
                    == self.executable_identity.as_ref()
                {
                    let _ = unsafe { libc::unlinkat(self.directory.as_raw_fd(), name.as_ptr(), 0) };
                }
            }
            let _ = fs::remove_dir(self.path());
        }
    }
}

impl VerifiedExecutableFile {
    fn validate(&self) -> HostResult<()> {
        let identity = file_identity(&self.file).map_err(|_| private_sidecar_error())?;
        let access_mode = unsafe { libc::fcntl(self.file.as_raw_fd(), libc::F_GETFL) };
        if identity != self.identity
            || identity.links > 1
            || identity.mode & 0o777 != 0o500
            || access_mode == -1
            || access_mode & libc::O_ACCMODE != libc::O_RDONLY
            || hash_file_at(&self.file, identity.size).map_err(|_| private_sidecar_error())?
                != self.digest
        {
            return Err(private_sidecar_error());
        }
        Ok(())
    }
}

#[derive(Debug)]
struct SidecarLaunchSpec {
    program: PathBuf,
    args: Vec<String>,
    current_dir: Option<PathBuf>,
    remove_env: &'static [&'static str],
    private_launch_dir: Option<PrivateLaunchDirectory>,
    verified_executable: Option<VerifiedExecutableFile>,
}

struct PreparedCommand {
    command: Command,
    _listener_guard: TcpListener,
    _executable_guard: Option<File>,
    _private_directory_guard: Option<File>,
    _parent_liveness_reader: File,
    parent_liveness_writer: Option<File>,
}

impl PreparedCommand {
    fn spawn(&mut self) -> std::io::Result<Child> {
        self.command.spawn()
    }

    fn take_parent_liveness_writer(&mut self) -> HostResult<File> {
        self.parent_liveness_writer
            .take()
            .ok_or_else(sidecar_state_error)
    }
}

struct AllocatedSidecarListener {
    listener: TcpListener,
    port: u16,
}

struct NativeInstanceCredential {
    instance_id: [u8; INSTANCE_ID_BYTES],
    readiness_key: [u8; READINESS_KEY_BYTES],
    session_token: [u8; SESSION_TOKEN_BYTES],
    handoff_token: [u8; HANDOFF_TOKEN_BYTES],
}

struct EncodedSecret(String);

impl EncodedSecret {
    fn new(bytes: &[u8]) -> Self {
        Self(encode_hex(bytes))
    }

    fn expose(&self) -> &str {
        &self.0
    }
}

impl Drop for EncodedSecret {
    fn drop(&mut self) {
        unsafe {
            self.0.as_bytes_mut().fill(0);
        }
    }
}

#[derive(Serialize)]
struct NativeInstanceFrame<'a> {
    protocol: &'static str,
    instance_id: &'a str,
    readiness_key: &'a str,
    session_token: &'a str,
    handoff_token: &'a str,
}

impl NativeInstanceCredential {
    fn generate() -> HostResult<Self> {
        let mut instance_id = [0_u8; INSTANCE_ID_BYTES];
        let mut readiness_key = [0_u8; READINESS_KEY_BYTES];
        let mut session_token = [0_u8; SESSION_TOKEN_BYTES];
        let mut handoff_token = [0_u8; HANDOFF_TOKEN_BYTES];
        OsRng
            .try_fill_bytes(&mut instance_id)
            .map_err(|_| instance_credential_error())?;
        OsRng
            .try_fill_bytes(&mut readiness_key)
            .map_err(|_| instance_credential_error())?;
        OsRng
            .try_fill_bytes(&mut session_token)
            .map_err(|_| instance_credential_error())?;
        OsRng
            .try_fill_bytes(&mut handoff_token)
            .map_err(|_| instance_credential_error())?;
        Ok(Self {
            instance_id,
            readiness_key,
            session_token,
            handoff_token,
        })
    }

    fn instance_id_hex(&self) -> String {
        encode_hex(&self.instance_id)
    }

    fn write_to_child(&self, child: &mut Child) -> HostResult<()> {
        let mut stdin = child.stdin.take().ok_or_else(instance_channel_error)?;
        let instance_id = self.instance_id_hex();
        let readiness_key = EncodedSecret::new(&self.readiness_key);
        let session_token = EncodedSecret::new(&self.session_token);
        let handoff_token = EncodedSecret::new(&self.handoff_token);
        let frame = NativeInstanceFrame {
            protocol: NATIVE_SIDECAR_PROTOCOL,
            instance_id: &instance_id,
            readiness_key: readiness_key.expose(),
            session_token: session_token.expose(),
            handoff_token: handoff_token.expose(),
        };
        let mut encoded = serde_json::to_vec(&frame).map_err(|_| instance_channel_error())?;
        encoded.push(b'\n');
        if encoded.len() > NATIVE_INSTANCE_FRAME_MAX_BYTES {
            encoded.fill(0);
            return Err(instance_channel_error());
        }
        let write_result = stdin.write_all(&encoded).and_then(|_| stdin.flush());
        encoded.fill(0);
        write_result.map_err(|_| instance_channel_error())
    }

    fn take_session_credential(&mut self) -> SessionCredential {
        SessionCredential(std::mem::replace(
            &mut self.session_token,
            [0_u8; SESSION_TOKEN_BYTES],
        ))
    }

    fn take_handoff_credential(&mut self) -> HandoffCredential {
        HandoffCredential(std::mem::replace(
            &mut self.handoff_token,
            [0_u8; HANDOFF_TOKEN_BYTES],
        ))
    }
}

impl Drop for NativeInstanceCredential {
    fn drop(&mut self) {
        self.instance_id.fill(0);
        self.readiness_key.fill(0);
        self.session_token.fill(0);
        self.handoff_token.fill(0);
    }
}

struct SessionCredential([u8; SESSION_TOKEN_BYTES]);
struct ReadinessCredential([u8; READINESS_KEY_BYTES]);
struct HandoffCredential([u8; HANDOFF_TOKEN_BYTES]);

impl SessionCredential {
    fn expose(&self) -> String {
        encode_hex(&self.0)
    }
}

impl Drop for SessionCredential {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

impl Drop for ReadinessCredential {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

impl HandoffCredential {
    fn expose(&self) -> EncodedSecret {
        EncodedSecret::new(&self.0)
    }
}

impl Drop for HandoffCredential {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum FeatureFlagV1 {
    RemoteProfiles,
    ProjectValidation,
    OperationEvents,
    RunObservability,
    ArtifactInspection,
    ServiceControl,
    Diagnostics,
    Maintenance,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct NegotiatedContractV1 {
    major: u8,
    openapi_sha256: String,
    provider_kind: String,
    feature_flags: Vec<FeatureFlagV1>,
}

#[derive(Clone, Serialize)]
struct DesktopBootstrapContextV1 {
    schema_version: &'static str,
    endpoint: String,
    session_token: String,
    negotiated_contract: NegotiatedContractV1,
}

#[derive(Clone, Debug, Serialize)]
struct HostStatus {
    state: String,
}

#[derive(Serialize)]
struct NativeWorkspaceImportRequest<'a> {
    schema_version: &'static str,
    kind: &'static str,
    action_id: &'a str,
    selected_path: &'a str,
    selected_device: u64,
    selected_inode: u64,
    cancellation_token: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    project_id: Option<&'a str>,
}

#[derive(Serialize)]
struct NativeWorkspaceCancelRequestV1<'a> {
    schema_version: &'static str,
    action_id: &'a str,
    cancellation_token: &'a str,
}

struct NativeWorkspaceSelection<'a> {
    kind: &'a str,
    action_id: &'a str,
    selected_path: &'a Path,
    selected_device: u64,
    selected_inode: u64,
    project_id: Option<&'a str>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct NativeWorkspaceImportRefV1 {
    import_id: String,
    content_sha256: String,
    byte_size: u64,
    entry_count: u64,
    extracted_byte_size: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct NativeProjectSourceV1 {
    kind: String,
    display_name: String,
    import_ref: NativeWorkspaceImportRefV1,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeWorkspaceImportResponseV1 {
    schema_version: String,
    source: NativeProjectSourceV1,
    lease_token: String,
}

#[derive(Serialize)]
struct NativeWorkspaceDiscardRequestV1<'a> {
    schema_version: &'static str,
    action_id: &'a str,
    import_ref: &'a NativeWorkspaceImportRefV1,
    lease_token: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    project_id: Option<&'a str>,
}

#[derive(Clone, Eq, PartialEq)]
struct PendingNativeWorkspaceImport {
    sidecar_instance: [u8; INSTANCE_ID_BYTES],
    action_id: String,
    project_id: Option<String>,
    source: NativeProjectSourceV1,
    lease_token: String,
}

struct NativePickerOperation {
    sidecar_instance: [u8; INSTANCE_ID_BYTES],
    action_id: String,
    cancellation_token: [u8; WORKSPACE_CANCELLATION_TOKEN_BYTES],
    cancelled: AtomicBool,
}

impl NativePickerOperation {
    fn generate(sidecar_instance: [u8; INSTANCE_ID_BYTES], action_id: String) -> HostResult<Self> {
        let mut cancellation_token = [0_u8; WORKSPACE_CANCELLATION_TOKEN_BYTES];
        OsRng
            .try_fill_bytes(&mut cancellation_token)
            .map_err(|_| workspace_import_error())?;
        Ok(Self {
            sidecar_instance,
            action_id,
            cancellation_token,
            cancelled: AtomicBool::new(false),
        })
    }

    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }

    fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }

    fn encoded_cancellation_token(&self) -> EncodedSecret {
        EncodedSecret::new(&self.cancellation_token)
    }
}

impl Drop for NativePickerOperation {
    fn drop(&mut self) {
        self.cancellation_token.fill(0);
    }
}

#[derive(Clone, Debug)]
struct LifecycleStatus {
    state: String,
    port: Option<u16>,
    pid: Option<u32>,
    url: Option<String>,
}

struct SidecarBootstrapState {
    session_credential: SessionCredential,
    readiness_credential: ReadinessCredential,
    handoff_credential: HandoffCredential,
    negotiated_contract: NegotiatedContractV1,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ManagedLifecycle {
    Starting,
    Running,
    CleanupPending,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum GroupSignalAuthority {
    Anchored,
    Finalizing,
}

struct ManagedSidecar {
    status: LifecycleStatus,
    bootstrap: Option<SidecarBootstrapState>,
    lifecycle: ManagedLifecycle,
    startup_epoch: u64,
    instance_id: [u8; INSTANCE_ID_BYTES],
    monitor_started: bool,
    spawn_pending: bool,
    child: Option<Child>,
    process_group: i32,
    session_id: i32,
    birth_identity: Option<String>,
    group_signal_authority: GroupSignalAuthority,
    process_cleanup_confirmed: bool,
    _private_launch_dir: Option<PrivateLaunchDirectory>,
    verified_executable: Option<VerifiedExecutableFile>,
    _listener: TcpListener,
}

struct SpawnedHandoffProcess {
    child: Child,
    process_group: i32,
    session_id: i32,
    birth_identity: Option<String>,
    group_signal_authority: GroupSignalAuthority,
}

enum SpawnHandoffOutcome {
    Pending,
    Spawning,
    Spawned(SpawnedHandoffProcess),
    Failed,
    Transferred,
}

struct SpawnHandoff {
    startup_epoch: u64,
    outcome: Mutex<SpawnHandoffOutcome>,
}

impl ManagedSidecar {
    fn mark_cleanup_pending(&mut self) {
        self.lifecycle = ManagedLifecycle::CleanupPending;
        self.status.state = "cleanup_pending".to_string();
        self.status.url = None;
    }

    fn child_mut(&mut self) -> HostResult<&mut Child> {
        self.child.as_mut().ok_or_else(sidecar_state_error)
    }

    fn host_status(&self) -> HostStatus {
        HostStatus {
            state: self.status.state.clone(),
        }
    }

    fn bootstrap_context(&self) -> HostResult<DesktopBootstrapContextV1> {
        let bootstrap = self.bootstrap.as_ref().ok_or_else(sidecar_state_error)?;
        let port = self.status.port.ok_or_else(sidecar_state_error)?;
        Ok(DesktopBootstrapContextV1 {
            schema_version: "1",
            endpoint: format!("http://127.0.0.1:{port}"),
            session_token: bootstrap.session_credential.expose(),
            negotiated_contract: bootstrap.negotiated_contract.clone(),
        })
    }
}

struct DesktopHostStateInner {
    sidecar: Mutex<Option<ManagedSidecar>>,
    run_retry_recovery: Mutex<()>,
    spawn_handoff: Mutex<Option<Arc<SpawnHandoff>>>,
    parent_liveness: Mutex<Option<File>>,
    startup_in_progress: AtomicBool,
    cancellation_epoch: AtomicU64,
    launch_state: AtomicU64,
    shutdown_requested: AtomicBool,
    active_picker: Mutex<Option<Arc<NativePickerOperation>>>,
    cancelled_picker_actions: Mutex<VecDeque<String>>,
    pending_workspace_imports: Mutex<HashMap<String, PendingNativeWorkspaceImport>>,
}

#[derive(Clone)]
struct DesktopHostState(Arc<DesktopHostStateInner>);

impl Deref for DesktopHostState {
    type Target = DesktopHostStateInner;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl Default for DesktopHostState {
    fn default() -> Self {
        Self(Arc::new(DesktopHostStateInner {
            sidecar: Mutex::new(None),
            run_retry_recovery: Mutex::new(()),
            spawn_handoff: Mutex::new(None),
            parent_liveness: Mutex::new(None),
            startup_in_progress: AtomicBool::new(false),
            cancellation_epoch: AtomicU64::new(0),
            launch_state: AtomicU64::new(encode_launch_state(0, LaunchPhase::Idle)),
            shutdown_requested: AtomicBool::new(false),
            active_picker: Mutex::new(None),
            cancelled_picker_actions: Mutex::new(VecDeque::new()),
            pending_workspace_imports: Mutex::new(HashMap::new()),
        }))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u64)]
enum LaunchPhase {
    Idle = 0,
    Reserved = 1,
    Spawning = 2,
    Published = 3,
    Cancelled = 4,
}

const LAUNCH_PHASE_BITS: u32 = 3;
const LAUNCH_PHASE_MASK: u64 = (1 << LAUNCH_PHASE_BITS) - 1;

fn encode_launch_state(epoch: u64, phase: LaunchPhase) -> u64 {
    (epoch << LAUNCH_PHASE_BITS) | phase as u64
}

fn decode_launch_state(value: u64) -> (u64, LaunchPhase) {
    let phase = match value & LAUNCH_PHASE_MASK {
        0 => LaunchPhase::Idle,
        1 => LaunchPhase::Reserved,
        2 => LaunchPhase::Spawning,
        3 => LaunchPhase::Published,
        _ => LaunchPhase::Cancelled,
    };
    (value >> LAUNCH_PHASE_BITS, phase)
}

struct StartupClaim<'a> {
    in_progress: &'a AtomicBool,
}

impl<'a> StartupClaim<'a> {
    fn acquire(state: &'a DesktopHostState) -> HostResult<(Self, u64)> {
        if state.shutdown_requested.load(Ordering::Acquire) {
            return Err(NativeHostError::new(
                "sidecar_host_shutting_down",
                "OpenEvo Desktop is shutting down its native sidecar host.",
            ));
        }
        let (epoch, _) = decode_launch_state(state.launch_state.load(Ordering::Acquire));
        state
            .startup_in_progress
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| sidecar_start_in_progress_error())?;
        let (confirmed_epoch, _) = decode_launch_state(state.launch_state.load(Ordering::Acquire));
        if confirmed_epoch != epoch || state.shutdown_requested.load(Ordering::Acquire) {
            state.startup_in_progress.store(false, Ordering::Release);
            return Err(sidecar_start_cancelled_error());
        }
        Ok((
            Self {
                in_progress: &state.startup_in_progress,
            },
            epoch,
        ))
    }
}

impl Drop for StartupClaim<'_> {
    fn drop(&mut self) {
        self.in_progress.store(false, Ordering::Release);
    }
}

struct PickerClaim {
    state: DesktopHostState,
    operation: Arc<NativePickerOperation>,
}

impl PickerClaim {
    fn acquire(
        state: &DesktopHostState,
        action_id: String,
        sidecar_instance: [u8; INSTANCE_ID_BYTES],
    ) -> HostResult<Self> {
        if !is_valid_native_text(&action_id) || action_id.len() < 16 {
            return Err(workspace_selection_error());
        }
        let mut active = state
            .active_picker
            .lock()
            .map_err(|_| workspace_import_error())?;
        if active.is_some() {
            return Err(NativeHostError::new(
                "workspace_selection_in_progress",
                "OpenEvo Desktop is already selecting a research folder.",
            ));
        }
        let mut cancelled = state
            .cancelled_picker_actions
            .lock()
            .map_err(|_| workspace_import_error())?;
        if let Some(index) = cancelled.iter().position(|pending| pending == &action_id) {
            cancelled.remove(index);
            return Err(workspace_selection_cancelled_error());
        }
        let operation = Arc::new(NativePickerOperation::generate(
            sidecar_instance,
            action_id,
        )?);
        *active = Some(operation.clone());
        Ok(Self {
            state: state.clone(),
            operation,
        })
    }
}

impl Drop for PickerClaim {
    fn drop(&mut self) {
        if let Ok(mut active) = self.state.active_picker.lock() {
            if active
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, &self.operation))
            {
                *active = None;
            }
        }
    }
}

fn cancel_active_picker(
    state: &DesktopHostState,
    action_id: &str,
) -> HostResult<Option<Arc<NativePickerOperation>>> {
    if !is_valid_native_text(action_id) || action_id.len() < 16 {
        return Err(workspace_selection_error());
    }
    let mut active = state
        .active_picker
        .lock()
        .map_err(|_| workspace_import_error())?;
    if active
        .as_ref()
        .is_some_and(|operation| operation.action_id == action_id)
    {
        let operation = active
            .take()
            .expect("active picker disappeared while locked");
        operation.cancel();
        return Ok(Some(operation));
    }
    let mut cancelled = state
        .cancelled_picker_actions
        .lock()
        .map_err(|_| workspace_import_error())?;
    if !cancelled.iter().any(|pending| pending == action_id) {
        if cancelled.len() == MAX_CANCELLED_PICKER_ACTIONS {
            cancelled.pop_front();
        }
        cancelled.push_back(action_id.to_string());
    }
    Ok(None)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeHealthResponse {
    service: String,
    status: String,
    protocol: String,
    instance_id: String,
    instance_proof: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VersionInfoV1 {
    schema_version: String,
    api_name: String,
    preferred_major: u8,
    supported_majors: Vec<u8>,
    openapi_sha256: String,
    build_version: String,
    source_commit: String,
    build_channel: String,
    provider_kind: String,
    feature_flags: Vec<FeatureFlagV1>,
}

trait ProcessControl {
    fn leader_exited(&self, child: &Child) -> std::io::Result<bool>;
    fn reap_leader(&self, child: &mut Child) -> std::io::Result<Option<ExitStatus>>;
    fn signal_group(&self, process_group: i32, signal: libc::c_int) -> std::io::Result<()>;
    fn group_has_members_except_leader(
        &self,
        process_group: i32,
        leader: i32,
    ) -> std::io::Result<bool>;
    fn sleep(&self, duration: Duration);
}

#[derive(Clone, Copy)]
struct OsProcessControl;

impl ProcessControl for OsProcessControl {
    fn leader_exited(&self, child: &Child) -> std::io::Result<bool> {
        leader_exited_without_reaping(child)
    }

    fn reap_leader(&self, child: &mut Child) -> std::io::Result<Option<ExitStatus>> {
        child.try_wait()
    }

    fn signal_group(&self, process_group: i32, signal: libc::c_int) -> std::io::Result<()> {
        if process_group <= 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "invalid process group",
            ));
        }
        let result = unsafe { libc::kill(-process_group, signal) };
        if result == 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(())
        } else {
            Err(error)
        }
    }

    fn group_has_members_except_leader(
        &self,
        process_group: i32,
        leader: i32,
    ) -> std::io::Result<bool> {
        group_has_members_except_leader(process_group, leader)
    }

    fn sleep(&self, duration: Duration) {
        thread::sleep(duration);
    }
}

fn leader_exited_without_reaping(child: &Child) -> std::io::Result<bool> {
    loop {
        let mut info = std::mem::MaybeUninit::<libc::siginfo_t>::zeroed();
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                child.id(),
                info.as_mut_ptr(),
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result == 0 {
            return Ok(unsafe { info.assume_init().si_pid() } != 0);
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EINTR) {
            return Err(error);
        }
    }
}

#[cfg(target_os = "linux")]
fn group_has_members_except_leader(process_group: i32, leader: i32) -> std::io::Result<bool> {
    for entry in fs::read_dir("/proc")? {
        let entry = entry?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<i32>().ok())
        else {
            continue;
        };
        if pid == leader {
            continue;
        }
        if let Some(true) =
            inspect_linux_proc_stat(fs::read_to_string(entry.path().join("stat")), process_group)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(any(test, target_os = "linux"))]
fn inspect_linux_proc_stat(
    stat: std::io::Result<String>,
    process_group: i32,
) -> std::io::Result<Option<bool>> {
    match stat {
        Ok(stat) => linux_proc_stat_is_live_group_member(&stat, process_group).map(Some),
        Err(error) if linux_proc_member_disappeared(&error) => Ok(None),
        Err(error) => Err(error),
    }
}

#[cfg(any(test, target_os = "linux"))]
fn linux_proc_member_disappeared(error: &std::io::Error) -> bool {
    error.kind() == std::io::ErrorKind::NotFound || error.raw_os_error() == Some(libc::ESRCH)
}

#[cfg(any(test, target_os = "linux"))]
fn linux_proc_stat_is_live_group_member(stat: &str, process_group: i32) -> std::io::Result<bool> {
    let after_name = stat
        .rsplit_once(')')
        .map(|(_, suffix)| suffix)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc stat"))?;
    let mut fields = after_name.split_whitespace();
    let state = fields.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "missing proc state")
    })?;
    if state.len() != 1 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid proc state",
        ));
    }
    fields.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "missing proc parent")
    })?;
    let group = fields
        .next()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing proc group"))?
        .parse::<i32>()
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc group"))?;
    Ok(group == process_group && state != "Z")
}

#[cfg(any(test, target_os = "linux"))]
fn linux_proc_stat_start_ticks(stat: &str) -> std::io::Result<u64> {
    let after_name = stat
        .rsplit_once(')')
        .map(|(_, suffix)| suffix)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc stat"))?;
    let start_ticks = after_name
        .split_whitespace()
        .nth(19)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing proc start"))?
        .parse::<u64>()
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc start"))?;
    if start_ticks == 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid proc start",
        ));
    }
    Ok(start_ticks)
}

#[cfg(target_os = "linux")]
fn sidecar_process_birth_identity(pid: i32) -> std::io::Result<(i32, i32, String)> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let after_name = stat
        .rsplit_once(')')
        .map(|(_, suffix)| suffix)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc stat"))?;
    let process_group = after_name
        .split_whitespace()
        .nth(2)
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing proc group"))?
        .parse::<i32>()
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid proc group"))?;
    let start_ticks = linux_proc_stat_start_ticks(&stat)?;
    let session_id = unsafe { libc::getsid(pid) };
    if session_id == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((process_group, session_id, format!("linux:{start_ticks}")))
}

#[cfg(target_os = "macos")]
fn sidecar_process_birth_identity(pid: i32) -> std::io::Result<(i32, i32, String)> {
    use std::ffi::c_void;

    let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::uninit();
    let expected_size = std::mem::size_of::<libc::proc_bsdinfo>();
    let buffer_size = libc::c_int::try_from(expected_size)
        .map_err(|_| std::io::Error::other("process identity buffer is too large"))?;
    let result = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            info.as_mut_ptr().cast::<c_void>(),
            buffer_size,
        )
    };
    if result != buffer_size {
        return Err(std::io::Error::last_os_error());
    }
    let info = unsafe { info.assume_init() };
    if info.pbi_pid != pid as u32
        || info.pbi_pgid == 0
        || info.pbi_start_tvsec == 0
        || info.pbi_start_tvusec >= 1_000_000
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid process identity",
        ));
    }
    let session_id = unsafe { libc::getsid(pid) };
    if session_id == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((
        info.pbi_pgid as i32,
        session_id,
        format!("darwin:{}:{}", info.pbi_start_tvsec, info.pbi_start_tvusec),
    ))
}

#[cfg(any(test, target_os = "macos"))]
fn macos_group_has_members_except_leader_with<C, F>(
    leader: i32,
    mut count_pids: C,
    mut fill_pids: F,
) -> std::io::Result<bool>
where
    C: FnMut() -> std::io::Result<usize>,
    F: FnMut(&mut [libc::pid_t]) -> std::io::Result<usize>,
{
    let required = count_pids()?;
    let mut capacity = required
        .checked_add(16)
        .filter(|value| *value <= MACOS_PROCESS_GROUP_MAX_PIDS)
        .ok_or_else(|| std::io::Error::other("process group listing is too large"))?;
    for _ in 0..MACOS_PROCESS_GROUP_LIST_RETRIES {
        let mut pids = vec![0 as libc::pid_t; capacity];
        let used = fill_pids(&mut pids)?;
        if used > capacity {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "process group listing exceeded its buffer",
            ));
        }
        if used < capacity {
            return Ok(pids[..used].iter().any(|pid| *pid > 0 && *pid != leader));
        }
        capacity = capacity
            .checked_mul(2)
            .filter(|value| *value <= MACOS_PROCESS_GROUP_MAX_PIDS)
            .ok_or_else(|| std::io::Error::other("process group listing is too large"))?;
    }
    Err(std::io::Error::other(
        "process group listing remained truncated",
    ))
}

#[cfg(any(test, target_os = "macos"))]
fn macos_proc_listpgrppids_call(call: impl FnOnce() -> libc::c_int) -> std::io::Result<usize> {
    unsafe {
        set_current_errno(0);
    }
    let raw_count = call();
    let error_number = unsafe { current_errno() };
    if raw_count < 0 || (raw_count == 0 && error_number != 0) {
        return Err(std::io::Error::from_raw_os_error(if error_number == 0 {
            libc::EIO
        } else {
            error_number
        }));
    }
    usize::try_from(raw_count)
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid PID count"))
}

#[cfg(target_os = "macos")]
fn group_has_members_except_leader(process_group: i32, leader: i32) -> std::io::Result<bool> {
    use std::ffi::c_void;
    use std::ptr;

    macos_group_has_members_except_leader_with(
        leader,
        || {
            macos_proc_listpgrppids_call(|| unsafe {
                libc::proc_listpgrppids(process_group, ptr::null_mut(), 0)
            })
        },
        |pids| {
            let byte_capacity = pids
                .len()
                .checked_mul(std::mem::size_of::<libc::pid_t>())
                .and_then(|size| libc::c_int::try_from(size).ok())
                .ok_or_else(|| std::io::Error::other("process group listing is too large"))?;
            macos_proc_listpgrppids_call(|| unsafe {
                libc::proc_listpgrppids(
                    process_group,
                    pids.as_mut_ptr().cast::<c_void>(),
                    byte_capacity,
                )
            })
        },
    )
}

fn allocate_sidecar_listener() -> HostResult<AllocatedSidecarListener> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| {
        NativeHostError::new(
            "sidecar_port_allocation_failed",
            "OpenEvo Desktop could not allocate a local sidecar listener.",
        )
    })?;
    let port = listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|_| {
            NativeHostError::new(
                "sidecar_port_allocation_failed",
                "OpenEvo Desktop could not inspect its local sidecar listener.",
            )
        })?;
    Ok(AllocatedSidecarListener { listener, port })
}

fn stopped_host_status() -> HostStatus {
    HostStatus {
        state: "stopped".to_string(),
    }
}

fn bundled_sidecar_path() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()?
        .parent()
        .map(|directory| directory.join(BUNDLED_SIDECAR_BINARY))
}

#[cfg(not(debug_assertions))]
fn active_launch_policy() -> LaunchPolicy {
    LaunchPolicy::Release
}

#[cfg(debug_assertions)]
fn active_launch_policy() -> LaunchPolicy {
    LaunchPolicy::Debug
}

fn sidecar_launch_spec(
    policy: LaunchPolicy,
    bundled_path: Option<&Path>,
    port: u16,
) -> HostResult<SidecarLaunchSpec> {
    match policy {
        LaunchPolicy::Release => release_sidecar_launch_spec(bundled_path, port),
        #[cfg(debug_assertions)]
        LaunchPolicy::Debug => debug_sidecar_launch_spec(bundled_path, port),
    }
}

fn release_sidecar_launch_spec(
    bundled_path: Option<&Path>,
    _port: u16,
) -> HostResult<SidecarLaunchSpec> {
    let source = bundled_path.ok_or_else(bundled_sidecar_missing_error)?;
    let (verified_executable, private_launch_dir) = prepare_packaged_sidecar(source)?;
    let program = release_execution_path(&private_launch_dir);
    Ok(SidecarLaunchSpec {
        program,
        args: local_sidecar_args(),
        current_dir: None,
        remove_env: &RELEASE_FORBIDDEN_SIDECAR_ENV,
        private_launch_dir: Some(private_launch_dir),
        verified_executable: Some(verified_executable),
    })
}

fn prepare_packaged_sidecar(
    path: &Path,
) -> HostResult<(VerifiedExecutableFile, PrivateLaunchDirectory)> {
    prepare_packaged_sidecar_with_hooks(path, || {}, || {}, || {})
}

fn prepare_packaged_sidecar_with_hooks(
    path: &Path,
    before_copy: impl FnOnce(),
    after_copy: impl FnOnce(),
    after_reread: impl FnOnce(),
) -> HostResult<(VerifiedExecutableFile, PrivateLaunchDirectory)> {
    let owner_policy = packaged_source_owner_policy()?;
    let (parent, name) = open_trusted_source_parent(path)?;
    let initial_identity = source_identity_at(&parent, &name)?;
    validate_packaged_source_identity(&initial_identity, owner_policy)?;
    let mut source = openat_file(
        parent.as_raw_fd(),
        &name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        0,
    )
    .map_err(|_| packaged_sidecar_identity_error())?;
    if file_identity(&source).map_err(|_| packaged_sidecar_identity_error())? != initial_identity {
        return Err(packaged_sidecar_identity_error());
    }
    validate_anchored_extended_acl(&source)?;
    let initial_digest = hash_file_at(&source, initial_identity.size)
        .map_err(|_| packaged_sidecar_identity_error())?;

    let mut private_dir = PrivateLaunchDirectory::create()?;
    let private_directory = &private_dir.directory;
    let private_name = CString::new(BUNDLED_SIDECAR_BINARY).expect("constant has no NUL");
    let mut writer = openat_file(
        private_directory.as_raw_fd(),
        &private_name,
        libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0o600,
    )
    .map_err(|_| private_sidecar_error())?;
    let reader = openat_file(
        private_directory.as_raw_fd(),
        &private_name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    )
    .map_err(|_| private_sidecar_error())?;
    let writer_identity = file_identity(&writer).map_err(|_| private_sidecar_error())?;
    let reader_identity = file_identity(&reader).map_err(|_| private_sidecar_error())?;
    if writer_identity != reader_identity || writer_identity.links != 1 {
        return Err(private_sidecar_error());
    }
    #[cfg(target_os = "linux")]
    {
        if unsafe { libc::unlinkat(private_directory.as_raw_fd(), private_name.as_ptr(), 0) } == -1
        {
            return Err(private_sidecar_error());
        }
        if source_identity_at_optional(private_directory, &private_name)?.is_some()
            || file_identity(&writer)
                .map_err(|_| private_sidecar_error())?
                .links
                != 0
            || file_identity(&reader)
                .map_err(|_| private_sidecar_error())?
                .links
                != 0
        {
            return Err(private_sidecar_error());
        }
    }
    #[cfg(target_os = "macos")]
    if source_identity_at(private_directory, &private_name)? != writer_identity {
        return Err(private_sidecar_error());
    }

    before_copy();
    if file_identity(&source).map_err(|_| packaged_sidecar_identity_error())? != initial_identity
        || source_identity_at(&parent, &name)? != initial_identity
    {
        return Err(packaged_sidecar_identity_error());
    }
    let copied_digest = copy_and_hash(&mut source, &mut writer, initial_identity.size)?;
    if copied_digest != initial_digest {
        return Err(packaged_sidecar_identity_error());
    }
    after_copy();
    let reread_digest = hash_file_at(&source, initial_identity.size)
        .map_err(|_| packaged_sidecar_identity_error())?;
    after_reread();
    let final_fd_identity =
        file_identity(&source).map_err(|_| packaged_sidecar_identity_error())?;
    let final_path_identity = source_identity_at(&parent, &name)?;
    if reread_digest != copied_digest
        || final_fd_identity != initial_identity
        || final_path_identity != initial_identity
    {
        return Err(packaged_sidecar_identity_error());
    }

    writer.flush().map_err(|_| private_sidecar_error())?;
    writer.sync_all().map_err(|_| private_sidecar_error())?;
    if unsafe { libc::fchmod(writer.as_raw_fd(), 0o500) } == -1 {
        return Err(private_sidecar_error());
    }
    writer.sync_all().map_err(|_| private_sidecar_error())?;
    private_directory
        .sync_all()
        .map_err(|_| private_sidecar_error())?;
    let final_writer_identity = file_identity(&writer).map_err(|_| private_sidecar_error())?;
    let final_reader_identity = file_identity(&reader).map_err(|_| private_sidecar_error())?;
    let target_digest =
        hash_file_at(&reader, initial_identity.size).map_err(|_| private_sidecar_error())?;
    #[cfg(target_os = "linux")]
    let expected_links = 0;
    #[cfg(target_os = "macos")]
    let expected_links = 1;
    if final_writer_identity != final_reader_identity
        || final_reader_identity.links != expected_links
        || final_reader_identity.owner != unsafe { libc::geteuid() }
        || final_reader_identity.size != initial_identity.size
        || final_reader_identity.mode & 0o777 != 0o500
        || target_digest != copied_digest
    {
        return Err(private_sidecar_error());
    }
    #[cfg(target_os = "macos")]
    if source_identity_at(private_directory, &private_name)? != final_reader_identity {
        return Err(private_sidecar_error());
    }
    drop(writer);
    private_dir.executable_identity = Some(final_reader_identity.clone());
    let verified = VerifiedExecutableFile {
        file: reader,
        identity: final_reader_identity,
        digest: copied_digest,
    };
    verified.validate()?;
    private_dir.validate()?;
    Ok((verified, private_dir))
}

fn packaged_source_owner_policy() -> HostResult<PackagedSourceOwnerPolicy> {
    let real_user = unsafe { libc::getuid() };
    let effective_user = unsafe { libc::geteuid() };
    validate_process_user_identity(real_user, effective_user)?;

    #[cfg(target_os = "linux")]
    {
        let executable = File::open("/proc/self/exe").map_err(|_| packaged_owner_error())?;
        let identity = file_identity(&executable).map_err(|_| packaged_owner_error())?;
        if identity.mode & libc::S_IFMT != libc::S_IFREG {
            return Err(packaged_owner_error());
        }
        validate_owner_is_root_or_effective(identity.owner, effective_user)?;
        Ok(PackagedSourceOwnerPolicy::MatchLoadedExecutable(
            identity.owner,
        ))
    }

    #[cfg(target_os = "macos")]
    {
        Ok(PackagedSourceOwnerPolicy::RootOrEffectiveUser(
            effective_user,
        ))
    }
}

fn validate_process_user_identity(real_user: u32, effective_user: u32) -> HostResult<()> {
    if real_user != effective_user {
        return Err(packaged_owner_error());
    }
    Ok(())
}

fn validate_owner_is_root_or_effective(owner: u32, effective_user: u32) -> HostResult<()> {
    if owner != 0 && owner != effective_user {
        return Err(packaged_owner_error());
    }
    Ok(())
}

fn validate_source_owner(source_owner: u32, policy: PackagedSourceOwnerPolicy) -> HostResult<()> {
    match policy {
        PackagedSourceOwnerPolicy::MatchLoadedExecutable(owner) if source_owner == owner => Ok(()),
        PackagedSourceOwnerPolicy::RootOrEffectiveUser(effective_user) => {
            validate_owner_is_root_or_effective(source_owner, effective_user)
        }
        PackagedSourceOwnerPolicy::MatchLoadedExecutable(_) => Err(packaged_owner_error()),
    }
}

fn open_trusted_source_parent(path: &Path) -> HostResult<(File, CString)> {
    if !path.is_absolute() {
        return Err(packaged_path_error());
    }
    let parent = path.parent().ok_or_else(packaged_path_error)?;
    let name = path.file_name().ok_or_else(packaged_path_error)?;
    let name = CString::new(name.as_bytes()).map_err(|_| packaged_path_error())?;
    let directory = open_directory_chain_no_follow(parent)?;
    Ok((directory, name))
}

fn open_directory_chain_no_follow(path: &Path) -> HostResult<File> {
    let mut current = open_directory(Path::new("/")).map_err(|_| packaged_path_error())?;
    validate_trusted_directory(&file_identity(&current).map_err(|_| packaged_path_error())?)?;
    validate_anchored_extended_acl(&current)?;
    for component in path.components() {
        match component {
            Component::RootDir => continue,
            Component::Normal(name) => {
                let name = CString::new(name.as_bytes()).map_err(|_| packaged_path_error())?;
                let next = openat_file(
                    current.as_raw_fd(),
                    &name,
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )
                .map_err(|_| packaged_path_error())?;
                validate_trusted_directory(
                    &file_identity(&next).map_err(|_| packaged_path_error())?,
                )?;
                validate_anchored_extended_acl(&next)?;
                current = next;
            }
            _ => return Err(packaged_path_error()),
        }
    }
    Ok(current)
}

fn validate_trusted_directory(identity: &FileIdentity) -> HostResult<()> {
    validate_trusted_directory_for_user(
        identity,
        unsafe { libc::geteuid() },
        trusted_directory_policy(),
    )
}

#[cfg(target_os = "linux")]
fn trusted_directory_policy() -> TrustedDirectoryPolicy {
    TrustedDirectoryPolicy::Strict
}

#[cfg(target_os = "macos")]
fn trusted_directory_policy() -> TrustedDirectoryPolicy {
    TrustedDirectoryPolicy::MacOsBundle
}

fn validate_trusted_directory_for_user(
    identity: &FileIdentity,
    effective_user: u32,
    policy: TrustedDirectoryPolicy,
) -> HostResult<()> {
    let is_directory = identity.mode & FILE_TYPE_MASK == DIRECTORY_FILE_TYPE;
    let trusted_owner = identity.owner == 0 || identity.owner == effective_user;
    let root_sticky_boundary = identity.owner == 0 && identity.mode & STICKY_MODE_BIT != 0;
    let world_writable = identity.mode & 0o002 != 0;
    let group_writable = identity.mode & 0o020 != 0;
    let macos_root_group_writable =
        policy == TrustedDirectoryPolicy::MacOsBundle && identity.owner == 0 && !world_writable;
    if !is_directory
        || !trusted_owner
        || (world_writable && !root_sticky_boundary)
        || (group_writable && !root_sticky_boundary && !macos_root_group_writable)
    {
        return Err(packaged_path_error());
    }
    Ok(())
}

fn validate_packaged_source_identity(
    identity: &FileIdentity,
    owner_policy: PackagedSourceOwnerPolicy,
) -> HostResult<()> {
    let kind = identity.mode & FILE_TYPE_MASK;
    if kind == SYMLINK_FILE_TYPE {
        return Err(NativeHostError::new(
            "bundled_sidecar_symlink",
            "The packaged OpenEvo Desktop bundled sidecar is an unsupported symbolic link.",
        ));
    }
    if kind != REGULAR_FILE_TYPE {
        return Err(NativeHostError::new(
            "bundled_sidecar_not_regular",
            "The packaged OpenEvo Desktop bundled sidecar is not a regular file.",
        ));
    }
    if identity.mode & 0o111 == 0 {
        return Err(NativeHostError::new(
            "bundled_sidecar_not_executable",
            "The packaged OpenEvo Desktop bundled sidecar is not executable. Reinstall the app.",
        ));
    }
    validate_source_owner(identity.owner, owner_policy)?;
    if identity.mode & 0o022 != 0 || identity.links != 1 || identity.size == 0 {
        return Err(NativeHostError::new(
            "bundled_sidecar_insecure",
            "The packaged OpenEvo Desktop bundled sidecar has unsafe file metadata. Reinstall the app.",
        ));
    }
    Ok(())
}

fn source_identity_at(directory: &File, name: &CString) -> HostResult<FileIdentity> {
    source_identity_at_optional(directory, name)?.ok_or_else(bundled_sidecar_missing_error)
}

fn source_identity_at_optional(
    directory: &File,
    name: &CString,
) -> HostResult<Option<FileIdentity>> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    let result = unsafe {
        libc::fstatat(
            directory.as_raw_fd(),
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result == 0 {
        return Ok(Some(file_identity_from_stat(unsafe {
            &stat.assume_init()
        })));
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == std::io::ErrorKind::NotFound {
        Ok(None)
    } else {
        Err(packaged_sidecar_identity_error())
    }
}

fn file_identity(file: &File) -> std::io::Result<FileIdentity> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(file.as_raw_fd(), stat.as_mut_ptr()) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(file_identity_from_stat(unsafe { &stat.assume_init() }))
}

fn file_identity_from_metadata(metadata: &Metadata) -> FileIdentity {
    FileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        mode: metadata.mode(),
        links: metadata.nlink(),
        owner: metadata.uid(),
        size: metadata.size(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    }
}

#[cfg(target_os = "linux")]
fn file_identity_from_stat(stat: &libc::stat) -> FileIdentity {
    FileIdentity {
        device: stat.st_dev,
        inode: stat.st_ino,
        mode: stat.st_mode,
        links: stat.st_nlink,
        owner: stat.st_uid,
        size: stat.st_size as u64,
        modified_seconds: stat.st_mtime,
        modified_nanoseconds: stat.st_mtime_nsec,
        changed_seconds: stat.st_ctime,
        changed_nanoseconds: stat.st_ctime_nsec,
    }
}

#[cfg(target_os = "macos")]
fn file_identity_from_stat(stat: &libc::stat) -> FileIdentity {
    FileIdentity {
        device: stat.st_dev as u64,
        inode: stat.st_ino,
        mode: stat.st_mode as u32,
        links: stat.st_nlink as u64,
        owner: stat.st_uid,
        size: stat.st_size as u64,
        modified_seconds: stat.st_mtime,
        modified_nanoseconds: stat.st_mtime_nsec,
        changed_seconds: stat.st_ctime,
        changed_nanoseconds: stat.st_ctime_nsec,
    }
}

fn open_directory(path: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
}

fn openat_file(
    directory: RawFd,
    name: &CString,
    flags: libc::c_int,
    mode: u32,
) -> std::io::Result<File> {
    let descriptor = unsafe { libc::openat(directory, name.as_ptr(), flags, mode) };
    if descriptor == -1 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(descriptor) })
    }
}

fn copy_and_hash(source: &mut File, target: &mut File, expected_size: u64) -> HostResult<[u8; 32]> {
    let mut hasher = Sha256::new();
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = source
            .read(&mut buffer)
            .map_err(|_| packaged_sidecar_identity_error())?;
        if count == 0 {
            break;
        }
        copied = copied
            .checked_add(count as u64)
            .ok_or_else(packaged_sidecar_identity_error)?;
        if copied > expected_size {
            return Err(packaged_sidecar_identity_error());
        }
        target
            .write_all(&buffer[..count])
            .map_err(|_| private_sidecar_error())?;
        hasher.update(&buffer[..count]);
    }
    if copied != expected_size {
        return Err(packaged_sidecar_identity_error());
    }
    Ok(hasher.finalize().into())
}

fn hash_file_at(file: &File, expected_size: u64) -> std::io::Result<[u8; 32]> {
    let mut hasher = Sha256::new();
    let mut consumed = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    while consumed < expected_size {
        let count = file.read_at(&mut buffer, consumed)?;
        if count == 0 {
            break;
        }
        consumed = consumed.checked_add(count as u64).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "file size overflow")
        })?;
        if consumed > expected_size {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "file exceeded expected size",
            ));
        }
        hasher.update(&buffer[..count]);
    }
    if consumed != expected_size {
        return Err(std::io::Error::new(
            std::io::ErrorKind::UnexpectedEof,
            "file did not match expected size",
        ));
    }
    let mut extra = [0_u8; 1];
    if file.read_at(&mut extra, expected_size)? != 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "file exceeded expected size",
        ));
    }
    Ok(hasher.finalize().into())
}

fn validate_private_launch_dir(path: &Path) -> HostResult<()> {
    let metadata = fs::symlink_metadata(path).map_err(|_| private_sidecar_error())?;
    if !metadata.file_type().is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(private_sidecar_error());
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn fd_execution_path() -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{INHERITED_EXECUTABLE_FD}"))
}

#[cfg(target_os = "macos")]
fn fd_execution_path() -> PathBuf {
    PathBuf::from(format!("/dev/fd/{INHERITED_EXECUTABLE_FD}"))
}

#[cfg(target_os = "linux")]
fn release_execution_path(_private_dir: &PrivateLaunchDirectory) -> PathBuf {
    fd_execution_path()
}

#[cfg(target_os = "macos")]
fn release_execution_path(private_dir: &PrivateLaunchDirectory) -> PathBuf {
    private_dir.path().join(BUNDLED_SIDECAR_BINARY)
}

#[cfg(target_os = "linux")]
fn finalize_private_executable_after_spawn(
    _private_dir: Option<&PrivateLaunchDirectory>,
    _executable: Option<&mut VerifiedExecutableFile>,
) -> HostResult<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn finalize_private_executable_after_spawn(
    private_dir: Option<&PrivateLaunchDirectory>,
    executable: Option<&mut VerifiedExecutableFile>,
) -> HostResult<()> {
    let (private_dir, executable) = match (private_dir, executable) {
        (None, None) => return Ok(()),
        (Some(private_dir), Some(executable)) => (private_dir, executable),
        _ => return Err(private_sidecar_error()),
    };
    private_dir.validate()?;
    executable.validate()?;
    if executable.identity.links != 1 {
        return Err(private_sidecar_error());
    }
    let name = CString::new(BUNDLED_SIDECAR_BINARY).expect("constant has no NUL");
    if source_identity_at(&private_dir.directory, &name)? != executable.identity {
        return Err(private_sidecar_error());
    }
    executable.validate()
}

#[cfg(target_os = "linux")]
fn cleanup_private_executable(
    _private_dir: Option<&mut PrivateLaunchDirectory>,
    _executable: Option<&mut VerifiedExecutableFile>,
) -> HostResult<()> {
    Ok(())
}

#[cfg(target_os = "macos")]
fn cleanup_private_executable(
    private_dir: Option<&mut PrivateLaunchDirectory>,
    executable: Option<&mut VerifiedExecutableFile>,
) -> HostResult<()> {
    let (private_dir, executable) = match (private_dir, executable) {
        (None, None) => return Ok(()),
        (Some(private_dir), Some(executable)) => (private_dir, executable),
        _ => return Err(private_sidecar_error()),
    };
    private_dir.validate()?;
    let expected = private_dir
        .executable_identity
        .as_ref()
        .ok_or_else(private_sidecar_error)?;
    let current = file_identity(&executable.file).map_err(|_| private_sidecar_error())?;
    if !same_identity_after_optional_unlink(&current, expected) || current.links > 1 {
        return Err(private_sidecar_error());
    }
    executable.identity = current;
    executable.validate()?;
    let name = CString::new(BUNDLED_SIDECAR_BINARY).expect("constant has no NUL");
    if executable.identity.links == 0 {
        return if source_identity_at_optional(&private_dir.directory, &name)?.is_none() {
            Ok(())
        } else {
            Err(private_sidecar_error())
        };
    }
    if source_identity_at(&private_dir.directory, &name)? != executable.identity {
        return Err(private_sidecar_error());
    }
    if unsafe { libc::unlinkat(private_dir.directory.as_raw_fd(), name.as_ptr(), 0) } == -1 {
        return Err(private_sidecar_error());
    }
    private_dir
        .directory
        .sync_all()
        .map_err(|_| private_sidecar_error())?;
    let unlinked = file_identity(&executable.file).map_err(|_| private_sidecar_error())?;
    if !same_identity_after_optional_unlink(&unlinked, expected) || unlinked.links != 0 {
        return Err(private_sidecar_error());
    }
    executable.identity = unlinked;
    executable.validate()?;
    if source_identity_at_optional(&private_dir.directory, &name)?.is_some() {
        Err(private_sidecar_error())
    } else {
        Ok(())
    }
}

fn bundled_sidecar_missing_error() -> NativeHostError {
    NativeHostError::new(
        "bundled_sidecar_missing",
        "The packaged OpenEvo Desktop bundled sidecar is missing. Reinstall the app.",
    )
}

fn packaged_sidecar_identity_error() -> NativeHostError {
    NativeHostError::new(
        "bundled_sidecar_identity_changed",
        "The packaged OpenEvo Desktop bundled sidecar changed during secure launch preparation.",
    )
}

fn packaged_owner_error() -> NativeHostError {
    NativeHostError::new(
        "bundled_sidecar_owner_invalid",
        "The packaged OpenEvo Desktop sidecar owner does not satisfy the native release policy.",
    )
}

fn packaged_path_error() -> NativeHostError {
    NativeHostError::new(
        "bundled_sidecar_path_untrusted",
        "OpenEvo Desktop could not bind a trusted packaged sidecar path.",
    )
}

fn private_sidecar_error() -> NativeHostError {
    NativeHostError::new(
        "private_sidecar_preparation_failed",
        "OpenEvo Desktop could not prepare its private sidecar launch image.",
    )
}

fn instance_credential_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_instance_credential_failed",
        "OpenEvo Desktop could not create a sidecar instance credential.",
    )
}

fn instance_channel_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_instance_channel_failed",
        "OpenEvo Desktop could not initialize the sidecar instance channel.",
    )
}

fn local_sidecar_args() -> Vec<String> {
    vec![
        "--listener-fd".to_string(),
        INHERITED_LISTENER_FD.to_string(),
        "--native-instance-stdin".to_string(),
    ]
}

#[cfg(debug_assertions)]
fn debug_sidecar_launch_spec(
    bundled_path: Option<&Path>,
    port: u16,
) -> HostResult<SidecarLaunchSpec> {
    const DEBUG_FALLBACK_MODULE: &str = "desktop.server.launcher";
    let configured_program = std::env::var_os("OPENEVO_DESKTOP_SIDECAR_PROGRAM");
    let configured_args = std::env::var_os("OPENEVO_DESKTOP_SIDECAR_ARGS_JSON");
    let (program, mut args) = if let Some(program) = configured_program {
        if program.is_empty() {
            return Err(NativeHostError::new(
                "debug_sidecar_program_invalid",
                "The debug sidecar program override is empty.",
            ));
        }
        let args = match configured_args {
            Some(value) => {
                serde_json::from_str::<Vec<String>>(&value.to_string_lossy()).map_err(|_| {
                    NativeHostError::new(
                        "debug_sidecar_args_invalid",
                        "The debug sidecar argument override must be a JSON string array.",
                    )
                })?
            }
            None => Vec::new(),
        };
        (PathBuf::from(program), args)
    } else {
        if configured_args.is_some() {
            return Err(NativeHostError::new(
                "debug_sidecar_args_invalid",
                "Debug sidecar arguments require a structured program override.",
            ));
        }
        if bundled_path.is_some_and(|path| fs::symlink_metadata(path).is_ok()) {
            return release_sidecar_launch_spec(bundled_path, port);
        }
        (
            PathBuf::from("python3"),
            vec!["-m".to_string(), DEBUG_FALLBACK_MODULE.to_string()],
        )
    };
    args.extend(local_sidecar_args());
    Ok(SidecarLaunchSpec {
        program,
        args,
        current_dir: std::env::var_os("OPENEVO_DESKTOP_SIDECAR_WORKDIR").map(PathBuf::from),
        remove_env: &[],
        private_launch_dir: None,
        verified_executable: None,
    })
}

fn duplicate_fd_at_least(fd: RawFd, minimum: RawFd) -> std::io::Result<RawFd> {
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, minimum) };
    if duplicate == -1 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(duplicate)
    }
}

fn parent_liveness_pipe() -> std::io::Result<(File, File)> {
    let mut pipe = [-1; 2];
    if unsafe { create_parent_liveness_pipe(pipe.as_mut_ptr()) } == -1 {
        return Err(std::io::Error::last_os_error());
    }
    let reader = unsafe { File::from_raw_fd(pipe[0]) };
    let writer = unsafe { File::from_raw_fd(pipe[1]) };
    set_close_on_exec(reader.as_raw_fd())?;
    set_close_on_exec(writer.as_raw_fd())?;
    let reader = unsafe { File::from_raw_fd(duplicate_fd_at_least(reader.as_raw_fd(), 10)?) };
    let writer = unsafe { File::from_raw_fd(duplicate_fd_at_least(writer.as_raw_fd(), 10)?) };
    Ok((reader, writer))
}

fn set_close_on_exec(fd: RawFd) -> std::io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } == -1 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(target_os = "linux")]
unsafe fn create_parent_liveness_pipe(pipe: *mut libc::c_int) -> libc::c_int {
    unsafe { libc::pipe2(pipe, libc::O_CLOEXEC) }
}

#[cfg(target_os = "macos")]
unsafe fn create_parent_liveness_pipe(pipe: *mut libc::c_int) -> libc::c_int {
    unsafe { libc::pipe(pipe) }
}

fn open_file_descriptor_limit() -> RawFd {
    let mut limit = std::mem::MaybeUninit::<libc::rlimit>::uninit();
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, limit.as_mut_ptr()) } == -1 {
        return 65_536;
    }
    let limit = unsafe { limit.assume_init() }.rlim_cur;
    limit.min(i32::MAX as libc::rlim_t) as RawFd
}

fn command_from_launch_spec(
    launch: &SidecarLaunchSpec,
    listener: &TcpListener,
) -> HostResult<PreparedCommand> {
    let (parent_liveness_reader, parent_liveness_writer) =
        parent_liveness_pipe().map_err(|_| {
            NativeHostError::new(
                "sidecar_spawn_failed",
                "OpenEvo Desktop could not create its sidecar parent-liveness channel.",
            )
        })?;
    let listener_fd = duplicate_fd_at_least(listener.as_raw_fd(), 10).map_err(|_| {
        NativeHostError::new(
            "sidecar_spawn_failed",
            "OpenEvo Desktop could not duplicate its sidecar listener.",
        )
    })?;
    let listener_guard = unsafe { TcpListener::from_raw_fd(listener_fd) };
    let executable_guard = match launch.verified_executable.as_ref() {
        Some(executable) => {
            executable.validate()?;
            let fd = duplicate_fd_at_least(executable.file.as_raw_fd(), 10)
                .map_err(|_| private_sidecar_error())?;
            Some(unsafe { File::from_raw_fd(fd) })
        }
        None => None,
    };
    let executable_fd = executable_guard.as_ref().map(AsRawFd::as_raw_fd);
    let expected_identity = launch
        .verified_executable
        .as_ref()
        .map(|executable| executable.identity.clone());
    let (
        private_directory_guard,
        expected_directory_identity,
        private_directory_path,
        private_executable_name,
        program_path,
    ) = match (
        launch.private_launch_dir.as_ref(),
        launch.verified_executable.as_ref(),
    ) {
        (Some(directory), Some(executable)) if executable.identity.links == 1 => {
            directory.validate()?;
            let fd = duplicate_fd_at_least(directory.directory.as_raw_fd(), 10)
                .map_err(|_| private_sidecar_error())?;
            let guard = unsafe { File::from_raw_fd(fd) };
            let directory_path = CString::new(directory.path().as_os_str().as_bytes())
                .map_err(|_| private_sidecar_error())?;
            let executable_name =
                CString::new(BUNDLED_SIDECAR_BINARY).expect("constant has no NUL");
            let program_path = CString::new(launch.program.as_os_str().as_bytes())
                .map_err(|_| private_sidecar_error())?;
            (
                Some(guard),
                Some(directory.identity.clone()),
                Some(directory_path),
                Some(executable_name),
                Some(program_path),
            )
        }
        _ => (None, None, None, None, None),
    };
    let private_directory_fd = private_directory_guard.as_ref().map(AsRawFd::as_raw_fd);
    let mut command = Command::new(&launch.program);
    command
        .args(&launch.args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for name in launch.remove_env {
        command.env_remove(name);
    }
    command.env_remove(NATIVE_LISTENER_FD_ENV);
    command.env_remove(NATIVE_EXECUTABLE_FD_ENV);
    command.env_remove(NATIVE_EXECUTABLE_PATH_ENV);
    command.env(NATIVE_LISTENER_FD_ENV, INHERITED_LISTENER_FD.to_string());
    if launch.verified_executable.is_some() {
        sanitize_pyinstaller_launch_environment(&mut command);
        command.env(
            NATIVE_EXECUTABLE_FD_ENV,
            INHERITED_EXECUTABLE_FD.to_string(),
        );
        #[cfg(target_os = "macos")]
        command.env(NATIVE_EXECUTABLE_PATH_ENV, &launch.program);
    }
    if let Some(workdir) = &launch.current_dir {
        command.current_dir(workdir);
    }
    let listener_fd = listener_guard.as_raw_fd();
    let parent_liveness_reader_fd = parent_liveness_reader.as_raw_fd();
    let parent_liveness_writer_fd = parent_liveness_writer.as_raw_fd();
    let descriptor_limit = open_file_descriptor_limit();
    // Command::spawn may wait for this block; keep it allocation-free and async-signal-safe.
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() == -1 {
                return Err(pre_exec_error());
            }
            let watchdog = libc::fork();
            if watchdog == -1 {
                return Err(pre_exec_error());
            }
            if watchdog == 0 {
                run_parent_liveness_watchdog(parent_liveness_reader_fd, descriptor_limit);
            }
            libc::close(parent_liveness_reader_fd);
            libc::close(parent_liveness_writer_fd);
            if libc::dup2(listener_fd, INHERITED_LISTENER_FD) == -1 {
                return Err(pre_exec_error());
            }
            clear_close_on_exec(INHERITED_LISTENER_FD)?;
            if let (Some(source_fd), Some(expected)) = (executable_fd, expected_identity.as_ref()) {
                if libc::dup2(source_fd, INHERITED_EXECUTABLE_FD) == -1 {
                    return Err(pre_exec_error());
                }
                clear_close_on_exec(INHERITED_EXECUTABLE_FD)?;
                let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
                if libc::fstat(INHERITED_EXECUTABLE_FD, stat.as_mut_ptr()) == -1 {
                    return Err(pre_exec_error());
                }
                let actual = file_identity_from_stat(&stat.assume_init());
                if &actual != expected {
                    return Err(std::io::Error::from_raw_os_error(libc::EPERM));
                }
            }
            if let (
                Some(directory_fd),
                Some(directory_expected),
                Some(directory_path),
                Some(executable_name),
                Some(program_path),
                Some(executable_expected),
            ) = (
                private_directory_fd,
                expected_directory_identity.as_ref(),
                private_directory_path.as_ref(),
                private_executable_name.as_ref(),
                program_path.as_ref(),
                expected_identity.as_ref(),
            ) {
                let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
                if libc::fstat(directory_fd, stat.as_mut_ptr()) == -1
                    || &file_identity_from_stat(&stat.assume_init()) != directory_expected
                    || libc::fstatat(
                        libc::AT_FDCWD,
                        directory_path.as_ptr(),
                        stat.as_mut_ptr(),
                        libc::AT_SYMLINK_NOFOLLOW,
                    ) == -1
                    || &file_identity_from_stat(&stat.assume_init()) != directory_expected
                    || libc::fstatat(
                        directory_fd,
                        executable_name.as_ptr(),
                        stat.as_mut_ptr(),
                        libc::AT_SYMLINK_NOFOLLOW,
                    ) == -1
                    || &file_identity_from_stat(&stat.assume_init()) != executable_expected
                    || libc::fstatat(
                        libc::AT_FDCWD,
                        program_path.as_ptr(),
                        stat.as_mut_ptr(),
                        libc::AT_SYMLINK_NOFOLLOW,
                    ) == -1
                    || &file_identity_from_stat(&stat.assume_init()) != executable_expected
                {
                    return Err(std::io::Error::from_raw_os_error(libc::EPERM));
                }
            }
            Ok(())
        });
    }
    Ok(PreparedCommand {
        command,
        _listener_guard: listener_guard,
        _executable_guard: executable_guard,
        _private_directory_guard: private_directory_guard,
        _parent_liveness_reader: parent_liveness_reader,
        parent_liveness_writer: Some(parent_liveness_writer),
    })
}

fn sanitize_pyinstaller_launch_environment(command: &mut Command) {
    for (name, _) in std::env::vars_os() {
        if name.as_bytes().starts_with(PYINSTALLER_PRIVATE_ENV_PREFIX) {
            command.env_remove(name);
        }
    }
    command.env(PYINSTALLER_RESET_ENVIRONMENT, "1");
}

unsafe fn run_parent_liveness_watchdog(reader: RawFd, descriptor_limit: RawFd) -> ! {
    let mut action = unsafe { std::mem::zeroed::<libc::sigaction>() };
    action.sa_sigaction = libc::SIG_IGN;
    unsafe {
        libc::sigemptyset(&mut action.sa_mask);
        libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut());
    }
    for fd in 0..reader {
        unsafe {
            libc::close(fd);
        }
    }
    unsafe {
        close_watchdog_descriptors_after(reader, descriptor_limit);
    }
    let mut byte = 0_u8;
    loop {
        let result = unsafe { libc::read(reader, (&mut byte as *mut u8).cast(), 1) };
        if result > 0 {
            continue;
        }
        if result == -1 && unsafe { current_errno() } == libc::EINTR {
            continue;
        }
        break;
    }
    unsafe {
        libc::kill(0, libc::SIGTERM);
    }
    let grace = libc::timespec {
        tv_sec: 0,
        tv_nsec: SIDECAR_EXIT_EMERGENCY_TERM_GRACE.as_nanos() as libc::c_long,
    };
    unsafe {
        libc::nanosleep(&grace, std::ptr::null_mut());
        libc::kill(0, libc::SIGKILL);
        libc::_exit(127);
    }
}

#[cfg(target_os = "linux")]
unsafe fn current_errno() -> libc::c_int {
    unsafe { *libc::__errno_location() }
}

#[cfg(all(test, target_os = "linux"))]
unsafe fn set_current_errno(value: libc::c_int) {
    unsafe {
        *libc::__errno_location() = value;
    }
}

#[cfg(target_os = "macos")]
unsafe fn current_errno() -> libc::c_int {
    unsafe { *libc::__error() }
}

#[cfg(target_os = "macos")]
unsafe fn set_current_errno(value: libc::c_int) {
    unsafe {
        *libc::__error() = value;
    }
}

#[cfg(target_os = "linux")]
unsafe fn close_watchdog_descriptors_after(reader: RawFd, descriptor_limit: RawFd) {
    let first = reader.saturating_add(1) as libc::c_uint;
    if unsafe { libc::syscall(libc::SYS_close_range, first, libc::c_uint::MAX, 0) } == 0 {
        return;
    }
    for fd in reader.saturating_add(1)..descriptor_limit {
        unsafe {
            libc::close(fd);
        }
    }
}

#[cfg(target_os = "macos")]
unsafe fn close_watchdog_descriptors_after(reader: RawFd, descriptor_limit: RawFd) {
    for fd in reader.saturating_add(1)..descriptor_limit {
        unsafe {
            libc::close(fd);
        }
    }
}

unsafe fn clear_close_on_exec(fd: RawFd) -> std::io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } == -1 {
        Err(pre_exec_error())
    } else {
        Ok(())
    }
}

fn pre_exec_error() -> std::io::Error {
    std::io::Error::from_raw_os_error(libc::EIO)
}

struct LoopbackHttpResponse {
    status: u16,
    body: String,
}

struct ActiveSidecarConnection {
    stream: TcpStream,
    handoff_token: EncodedSecret,
}

struct SelectedDirectory {
    _descriptor: File,
    path: PathBuf,
    device: u64,
    inode: u64,
}

fn loopback_http_get(
    port: u16,
    path: &str,
    header: Option<(&str, &str)>,
) -> HostResult<LoopbackHttpResponse> {
    loopback_http_request(
        port,
        "GET",
        path,
        header,
        None,
        SIDECAR_HEALTH_CONNECT_TIMEOUT,
        SIDECAR_HEALTH_RESPONSE_MAX_BYTES,
    )
}

fn loopback_http_request(
    port: u16,
    method: &str,
    path: &str,
    header: Option<(&str, &str)>,
    body: Option<&[u8]>,
    timeout: Duration,
    response_limit: usize,
) -> HostResult<LoopbackHttpResponse> {
    if !matches!(method, "GET" | "POST")
        || !path.starts_with('/')
        || path.contains(['\r', '\n'])
        || header.is_some_and(|(name, value)| {
            name.is_empty() || name.contains([':', '\r', '\n']) || value.contains(['\r', '\n'])
        })
        || response_limit == 0
    {
        return Err(sidecar_health_error());
    }
    let deadline = Instant::now() + timeout;
    let mut stream = connect_loopback_until(port, deadline)?;
    loopback_http_request_on_stream(
        &mut stream,
        method,
        path,
        header,
        body,
        deadline,
        response_limit,
    )
}

fn connect_loopback_until(port: u16, deadline: Instant) -> HostResult<TcpStream> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, remaining_deadline(deadline)?)
        .map_err(|_| sidecar_health_error())
}

fn remaining_deadline(deadline: Instant) -> HostResult<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .ok_or_else(sidecar_health_error)
}

fn cancellation_aware_timeout(
    deadline: Instant,
    operation: Option<&NativePickerOperation>,
    cancellation_deadline: &mut Option<Instant>,
) -> HostResult<Duration> {
    if operation.is_some_and(NativePickerOperation::is_cancelled) && cancellation_deadline.is_none()
    {
        *cancellation_deadline = Some(Instant::now() + NATIVE_WORKSPACE_CANCEL_GRACE);
    }
    let effective_deadline = cancellation_deadline
        .map(|cancelled| deadline.min(cancelled))
        .unwrap_or(deadline);
    let remaining = remaining_deadline(effective_deadline)?;
    Ok(if operation.is_some() {
        remaining.min(NATIVE_WORKSPACE_IO_POLL)
    } else {
        remaining
    })
}

fn write_all_until_with_cancellation(
    stream: &mut TcpStream,
    mut bytes: &[u8],
    deadline: Instant,
    operation: Option<&NativePickerOperation>,
    cancellation_deadline: &mut Option<Instant>,
) -> HostResult<()> {
    while !bytes.is_empty() {
        stream
            .set_write_timeout(Some(cancellation_aware_timeout(
                deadline,
                operation,
                cancellation_deadline,
            )?))
            .map_err(|_| sidecar_health_error())?;
        let written = match stream.write(bytes) {
            Ok(written) => written,
            Err(error)
                if operation.is_some()
                    && matches!(
                        error.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
            {
                continue;
            }
            Err(_) => return Err(sidecar_health_error()),
        };
        if written == 0 {
            return Err(sidecar_health_error());
        }
        bytes = &bytes[written..];
    }
    Ok(())
}

fn read_until_with_cancellation(
    stream: &mut TcpStream,
    buffer: &mut [u8],
    deadline: Instant,
    operation: Option<&NativePickerOperation>,
    cancellation_deadline: &mut Option<Instant>,
) -> HostResult<usize> {
    loop {
        stream
            .set_read_timeout(Some(cancellation_aware_timeout(
                deadline,
                operation,
                cancellation_deadline,
            )?))
            .map_err(|_| sidecar_health_error())?;
        match stream.read(buffer) {
            Ok(read) => return Ok(read),
            Err(error)
                if operation.is_some()
                    && matches!(
                        error.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) => {}
            Err(_) => return Err(sidecar_health_error()),
        }
    }
}

fn loopback_http_request_on_stream(
    stream: &mut TcpStream,
    method: &str,
    path: &str,
    header: Option<(&str, &str)>,
    body: Option<&[u8]>,
    deadline: Instant,
    response_limit: usize,
) -> HostResult<LoopbackHttpResponse> {
    loopback_http_request_on_stream_with_cancellation(
        stream,
        method,
        path,
        header,
        body,
        deadline,
        response_limit,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
fn loopback_http_request_on_stream_with_cancellation(
    stream: &mut TcpStream,
    method: &str,
    path: &str,
    header: Option<(&str, &str)>,
    body: Option<&[u8]>,
    deadline: Instant,
    response_limit: usize,
    operation: Option<&NativePickerOperation>,
) -> HostResult<LoopbackHttpResponse> {
    if !matches!(method, "GET" | "POST")
        || !path.starts_with('/')
        || path.contains(['\r', '\n'])
        || header.is_some_and(|(name, value)| {
            name.is_empty() || name.contains([':', '\r', '\n']) || value.contains(['\r', '\n'])
        })
        || response_limit == 0
    {
        return Err(sidecar_health_error());
    }
    let extra_header = header
        .map(|(name, value)| format!("{name}: {value}\r\n"))
        .unwrap_or_default();
    let body = body.unwrap_or_default();
    let content_headers = if body.is_empty() {
        String::new()
    } else {
        format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            body.len()
        )
    };
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{extra_header}{content_headers}Connection: close\r\n\r\n"
    );
    let mut cancellation_deadline = None;
    write_all_until_with_cancellation(
        stream,
        request.as_bytes(),
        deadline,
        operation,
        &mut cancellation_deadline,
    )?;
    if !body.is_empty() {
        write_all_until_with_cancellation(
            stream,
            body,
            deadline,
            operation,
            &mut cancellation_deadline,
        )?;
    }

    let mut response = Vec::with_capacity(response_limit.min(4096));
    let header_end = loop {
        if let Some(index) = response.windows(4).position(|window| window == b"\r\n\r\n") {
            break index + 4;
        }
        if response.len() >= response_limit {
            return Err(sidecar_health_error());
        }
        let mut chunk = [0_u8; 1024];
        let read = read_until_with_cancellation(
            stream,
            &mut chunk,
            deadline,
            operation,
            &mut cancellation_deadline,
        )?;
        if read == 0 || response.len().saturating_add(read) > response_limit {
            return Err(sidecar_health_error());
        }
        response.extend_from_slice(&chunk[..read]);
    };
    let headers =
        std::str::from_utf8(&response[..header_end - 4]).map_err(|_| sidecar_health_error())?;
    let mut lines = headers.split("\r\n");
    let mut status_fields = lines
        .next()
        .ok_or_else(sidecar_health_error)?
        .split_whitespace();
    let protocol = status_fields.next().ok_or_else(sidecar_health_error)?;
    let status = status_fields
        .next()
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(sidecar_health_error)?;
    if protocol != "HTTP/1.1" && protocol != "HTTP/1.0" {
        return Err(sidecar_health_error());
    }

    let mut content_length = None;
    for line in lines {
        let (name, value) = line.split_once(':').ok_or_else(sidecar_health_error)?;
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(sidecar_health_error());
        }
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.is_some() || value.trim().is_empty() {
                return Err(sidecar_health_error());
            }
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .map_err(|_| sidecar_health_error())?,
            );
        }
    }
    let content_length = match content_length {
        Some(content_length) => content_length,
        None if status == 204 => 0,
        None => return Err(sidecar_health_error()),
    };
    if content_length > response_limit.saturating_sub(header_end) {
        return Err(sidecar_health_error());
    }
    let mut response_body = response.split_off(header_end);
    if response_body.len() > content_length {
        return Err(sidecar_health_error());
    }
    while response_body.len() < content_length {
        let remaining = content_length - response_body.len();
        let mut chunk = [0_u8; 1024];
        let chunk_limit = remaining.min(chunk.len());
        let read = read_until_with_cancellation(
            stream,
            &mut chunk[..chunk_limit],
            deadline,
            operation,
            &mut cancellation_deadline,
        )?;
        if read == 0 {
            return Err(sidecar_health_error());
        }
        response_body.extend_from_slice(&chunk[..read]);
    }
    Ok(LoopbackHttpResponse {
        status,
        body: String::from_utf8(response_body).map_err(|_| sidecar_health_error())?,
    })
}

fn check_sidecar_health(port: u16, credential: &NativeInstanceCredential) -> HostResult<()> {
    check_sidecar_health_for_instance(port, &credential.instance_id, &credential.readiness_key)
}

fn check_sidecar_health_for_instance(
    port: u16,
    instance_id: &[u8; INSTANCE_ID_BYTES],
    readiness_key: &[u8; READINESS_KEY_BYTES],
) -> HostResult<()> {
    let mut challenge = [0_u8; READINESS_KEY_BYTES];
    OsRng
        .try_fill_bytes(&mut challenge)
        .map_err(|_| sidecar_health_error())?;
    check_sidecar_health_with_challenge_for_instance(
        port,
        instance_id,
        readiness_key,
        &encode_hex(&challenge),
    )
}

#[cfg(test)]
fn check_sidecar_health_with_challenge(
    port: u16,
    credential: &NativeInstanceCredential,
    challenge: &str,
) -> HostResult<()> {
    check_sidecar_health_with_challenge_for_instance(
        port,
        &credential.instance_id,
        &credential.readiness_key,
        challenge,
    )
}

fn check_sidecar_health_with_challenge_for_instance(
    port: u16,
    expected_instance_id: &[u8; INSTANCE_ID_BYTES],
    readiness_key: &[u8; READINESS_KEY_BYTES],
    challenge: &str,
) -> HostResult<()> {
    if challenge.len() != 64 || !challenge.bytes().all(is_lower_hex) {
        return Err(sidecar_health_error());
    }
    let response = loopback_http_get(
        port,
        "/health",
        Some(("X-OpenEvo-Native-Challenge", challenge)),
    )?;
    if response.status != 200 {
        return Err(sidecar_health_error());
    }
    let health: NativeHealthResponse =
        serde_json::from_str(&response.body).map_err(|_| sidecar_health_error())?;
    let instance_id = encode_hex(expected_instance_id);
    if health.service != "openevo-sidecar"
        || health.status != "ok"
        || health.protocol != NATIVE_SIDECAR_PROTOCOL
        || health.instance_id != instance_id
    {
        return Err(sidecar_health_error());
    }
    let proof = decode_hex_32(&health.instance_proof).ok_or_else(sidecar_health_error)?;
    let mut mac = HmacSha256::new_from_slice(readiness_key).map_err(|_| sidecar_health_error())?;
    mac.update(readiness_hmac_domain(&instance_id, challenge).as_bytes());
    mac.verify_slice(&proof).map_err(|_| sidecar_health_error())
}

fn check_sidecar_contract(port: u16) -> HostResult<NegotiatedContractV1> {
    let response = loopback_http_get(port, "/version", None)
        .map_err(|_| sidecar_contract_incompatible_error())?;
    if response.status != 200 {
        return Err(sidecar_contract_incompatible_error());
    }
    let version: VersionInfoV1 =
        serde_json::from_str(&response.body).map_err(|_| sidecar_contract_incompatible_error())?;
    let duplicate_features = version
        .feature_flags
        .iter()
        .enumerate()
        .any(|(index, flag)| version.feature_flags[..index].contains(flag));
    if version.schema_version != "1"
        || version.api_name != DESKTOP_LOCAL_API_NAME
        || version.preferred_major != 1
        || version.supported_majors.is_empty()
        || version.supported_majors.iter().any(|major| *major != 1)
        || version.openapi_sha256 != DESKTOP_LOCAL_API_OPENAPI_SHA256
        || version.build_version.is_empty()
        || version.build_version.len() > 512
        || version.build_version.contains('\0')
        || !(7..=40).contains(&version.source_commit.len())
        || !version.source_commit.bytes().all(is_lower_hex)
        || version.build_channel != "release"
        || version.provider_kind != "desktop_sidecar"
        || duplicate_features
    {
        return Err(sidecar_contract_incompatible_error());
    }
    let legacy = loopback_http_get(port, LEGACY_DESKTOP_SHELL_ROUTE, None)
        .map_err(|_| sidecar_contract_incompatible_error())?;
    if legacy.status != 404 {
        return Err(sidecar_contract_incompatible_error());
    }
    Ok(NegotiatedContractV1 {
        major: 1,
        openapi_sha256: DESKTOP_LOCAL_API_OPENAPI_SHA256.to_string(),
        provider_kind: "desktop_sidecar".to_string(),
        feature_flags: version.feature_flags,
    })
}

fn check_sidecar_session_binding(port: u16, session_token: &str) -> HostResult<()> {
    let authenticated = loopback_http_get(
        port,
        NATIVE_SESSION_PROBE_ROUTE,
        Some((NATIVE_SESSION_HEADER, session_token)),
    )
    .map_err(|_| sidecar_session_error())?;
    if authenticated.status != 204 || !authenticated.body.is_empty() {
        return Err(sidecar_session_error());
    }
    let unauthenticated = loopback_http_get(port, NATIVE_SESSION_PROBE_ROUTE, None)
        .map_err(|_| sidecar_session_error())?;
    if unauthenticated.status != 403 {
        return Err(sidecar_session_error());
    }
    Ok(())
}

fn register_native_workspace_source(
    stream: &mut TcpStream,
    handoff_token: &str,
    selection: NativeWorkspaceSelection<'_>,
    operation: &NativePickerOperation,
) -> HostResult<NativeWorkspaceImportResponseV1> {
    if selection.kind != "native_folder_snapshot"
        || selection.action_id.len() < 16
        || selection.action_id.len() > 256
        || selection.action_id.trim() != selection.action_id
        || selection
            .action_id
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
        || selection.selected_inode == 0
        || selection
            .project_id
            .is_some_and(|value| !is_valid_native_text(value))
        || selection.action_id != operation.action_id
    {
        return Err(workspace_selection_error());
    }
    let selected_path = selection
        .selected_path
        .to_str()
        .ok_or_else(workspace_selection_error)?;
    let cancellation_token = operation.encoded_cancellation_token();
    let request = NativeWorkspaceImportRequest {
        schema_version: "1",
        kind: "native_folder_snapshot",
        action_id: selection.action_id,
        selected_path,
        selected_device: selection.selected_device,
        selected_inode: selection.selected_inode,
        cancellation_token: cancellation_token.expose(),
        project_id: selection.project_id,
    };
    let body = serde_json::to_vec(&request).map_err(|_| workspace_import_error())?;
    let response = loopback_http_request_on_stream_with_cancellation(
        stream,
        "POST",
        NATIVE_WORKSPACE_IMPORT_ROUTE,
        Some((NATIVE_HANDOFF_HEADER, handoff_token)),
        Some(&body),
        Instant::now() + NATIVE_WORKSPACE_IMPORT_TIMEOUT,
        NATIVE_WORKSPACE_RESPONSE_MAX_BYTES,
        Some(operation),
    )
    .map_err(|_| {
        if operation.is_cancelled() {
            workspace_selection_cancelled_error()
        } else {
            workspace_import_error()
        }
    })?;
    if response.status != 201 {
        return Err(if operation.is_cancelled() {
            workspace_selection_cancelled_error()
        } else {
            workspace_import_error()
        });
    }
    let pending: NativeWorkspaceImportResponseV1 =
        serde_json::from_str(&response.body).map_err(|_| workspace_import_error())?;
    if pending.schema_version != "1"
        || pending.lease_token.len() != 64
        || !pending.lease_token.bytes().all(is_lower_hex)
    {
        return Err(workspace_import_error());
    }
    validate_native_project_source(&pending.source)?;
    Ok(pending)
}

fn cancel_native_workspace_operation(
    stream: &mut TcpStream,
    handoff_token: &str,
    operation: &NativePickerOperation,
) -> HostResult<()> {
    let cancellation_token = operation.encoded_cancellation_token();
    let body = serde_json::to_vec(&NativeWorkspaceCancelRequestV1 {
        schema_version: "1",
        action_id: &operation.action_id,
        cancellation_token: cancellation_token.expose(),
    })
    .map_err(|_| workspace_import_error())?;
    let response = loopback_http_request_on_stream(
        stream,
        "POST",
        NATIVE_WORKSPACE_CANCEL_ROUTE,
        Some((NATIVE_HANDOFF_HEADER, handoff_token)),
        Some(&body),
        Instant::now() + NATIVE_WORKSPACE_CANCEL_TIMEOUT,
        NATIVE_WORKSPACE_RESPONSE_MAX_BYTES,
    )
    .map_err(|_| workspace_import_error())?;
    if response.status != 204 || !response.body.is_empty() {
        return Err(workspace_import_error());
    }
    Ok(())
}

fn discard_native_workspace_source(
    stream: &mut TcpStream,
    handoff_token: &str,
    pending: &PendingNativeWorkspaceImport,
) -> HostResult<()> {
    let body = serde_json::to_vec(&NativeWorkspaceDiscardRequestV1 {
        schema_version: "1",
        action_id: &pending.action_id,
        import_ref: &pending.source.import_ref,
        lease_token: &pending.lease_token,
        project_id: pending.project_id.as_deref(),
    })
    .map_err(|_| workspace_import_error())?;
    let response = loopback_http_request_on_stream(
        stream,
        "POST",
        NATIVE_WORKSPACE_DISCARD_ROUTE,
        Some((NATIVE_HANDOFF_HEADER, handoff_token)),
        Some(&body),
        Instant::now() + NATIVE_WORKSPACE_IMPORT_TIMEOUT,
        NATIVE_WORKSPACE_RESPONSE_MAX_BYTES,
    )
    .map_err(|_| workspace_import_error())?;
    if response.status != 204 || !response.body.is_empty() {
        return Err(workspace_import_error());
    }
    Ok(())
}

fn validate_native_project_source(source: &NativeProjectSourceV1) -> HostResult<()> {
    let import = &source.import_ref;
    let import_suffix = import
        .import_id
        .strip_prefix("workspace-import-")
        .ok_or_else(workspace_import_error)?;
    if source.kind != "native_folder_snapshot"
        || source.display_name.is_empty()
        || source.display_name.len() > 256
        || source.display_name.trim() != source.display_name
        || matches!(source.display_name.as_str(), "." | "..")
        || source.display_name.contains(['/', '\\'])
        || source
            .display_name
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
        || import_suffix.len() != 48
        || !import_suffix.bytes().all(is_lower_hex)
        || import.content_sha256.len() != 64
        || !import.content_sha256.bytes().all(is_lower_hex)
        || !(1024..=16 * 1024 * 1024 * 1024).contains(&import.byte_size)
        || !import.byte_size.is_multiple_of(512)
        || import.entry_count > 100_000
        || import.extracted_byte_size > 16 * 1024 * 1024 * 1024
        || (import.entry_count == 0 && import.extracted_byte_size != 0)
    {
        return Err(workspace_import_error());
    }
    Ok(())
}

fn remember_pending_workspace_import(
    state: &DesktopHostState,
    pending: PendingNativeWorkspaceImport,
) -> HostResult<()> {
    let mut imports = state
        .pending_workspace_imports
        .lock()
        .map_err(|_| workspace_import_error())?;
    imports.retain(|_, value| value.sidecar_instance == pending.sidecar_instance);
    if let Some(existing) = imports.get(&pending.action_id) {
        return if existing == &pending {
            Ok(())
        } else {
            Err(workspace_import_error())
        };
    }
    if imports.len() >= MAX_PENDING_WORKSPACE_IMPORTS {
        return Err(workspace_import_error());
    }
    imports.insert(pending.action_id.clone(), pending);
    Ok(())
}

fn take_pending_workspace_import(
    state: &DesktopHostState,
    action_id: &str,
) -> HostResult<Option<PendingNativeWorkspaceImport>> {
    if !is_valid_native_text(action_id) || action_id.len() < 16 {
        return Err(workspace_selection_error());
    }
    let pending = state
        .pending_workspace_imports
        .lock()
        .map_err(|_| workspace_import_error())?
        .remove(action_id);
    Ok(pending)
}

fn restore_pending_workspace_import(
    state: &DesktopHostState,
    pending: PendingNativeWorkspaceImport,
) {
    if let Ok(mut imports) = state.pending_workspace_imports.lock() {
        if imports.len() < MAX_PENDING_WORKSPACE_IMPORTS {
            imports.entry(pending.action_id.clone()).or_insert(pending);
        }
    }
}

fn is_valid_native_text(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value.trim() == value
        && !value
            .chars()
            .any(|character| character <= '\u{1f}' || character == '\u{7f}')
}

fn active_sidecar_instance(state: &DesktopHostState) -> HostResult<[u8; INSTANCE_ID_BYTES]> {
    let sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_ref().ok_or_else(sidecar_state_error)?;
    if managed.lifecycle != ManagedLifecycle::Running {
        return Err(sidecar_state_error());
    }
    Ok(managed.instance_id)
}

fn active_sidecar_connection(
    state: &DesktopHostState,
    expected_instance: [u8; INSTANCE_ID_BYTES],
) -> HostResult<ActiveSidecarConnection> {
    let sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_ref().ok_or_else(sidecar_state_error)?;
    if managed.lifecycle != ManagedLifecycle::Running || managed.instance_id != expected_instance {
        return Err(sidecar_state_error());
    }
    let port = managed.status.port.ok_or_else(sidecar_state_error)?;
    let bootstrap = managed.bootstrap.as_ref().ok_or_else(sidecar_state_error)?;
    let stream = connect_loopback_until(port, Instant::now() + SIDECAR_HEALTH_CONNECT_TIMEOUT)?;
    Ok(ActiveSidecarConnection {
        stream,
        handoff_token: bootstrap.handoff_credential.expose(),
    })
}

fn open_selected_directory(selected: &Path) -> HostResult<SelectedDirectory> {
    let descriptor = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(selected)
        .map_err(|_| workspace_selection_error())?;
    let metadata = descriptor
        .metadata()
        .map_err(|_| workspace_selection_error())?;
    if !metadata.is_dir() {
        return Err(workspace_selection_error());
    }
    let path = path_for_open_directory(&descriptor)?;
    let current = fs::symlink_metadata(&path).map_err(|_| workspace_selection_error())?;
    if !current.is_dir()
        || current.dev() != metadata.dev()
        || current.ino() != metadata.ino()
        || path.parent().is_none()
    {
        return Err(workspace_selection_error());
    }
    Ok(SelectedDirectory {
        _descriptor: descriptor,
        path,
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

#[cfg(target_os = "linux")]
fn path_for_open_directory(descriptor: &File) -> HostResult<PathBuf> {
    fs::canonicalize(PathBuf::from("/proc/self/fd").join(descriptor.as_raw_fd().to_string()))
        .map_err(|_| workspace_selection_error())
}

#[cfg(target_os = "macos")]
fn path_for_open_directory(descriptor: &File) -> HostResult<PathBuf> {
    use std::ffi::{CStr, OsStr};

    let mut buffer = [0_i8; libc::PATH_MAX as usize];
    if unsafe { libc::fcntl(descriptor.as_raw_fd(), libc::F_GETPATH, buffer.as_mut_ptr()) } == -1 {
        return Err(workspace_selection_error());
    }
    let bytes = unsafe { CStr::from_ptr(buffer.as_ptr()) }.to_bytes();
    if bytes.is_empty() {
        return Err(workspace_selection_error());
    }
    Ok(PathBuf::from(OsStr::from_bytes(bytes)))
}

fn readiness_hmac_domain(instance_id: &str, challenge: &str) -> String {
    format!("{NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{challenge}")
}

#[cfg(test)]
fn wait_for_sidecar_ready(
    child: &mut Child,
    port: u16,
    credential: &NativeInstanceCredential,
    timeout: Duration,
    is_cancelled: impl Fn() -> bool,
) -> HostResult<NegotiatedContractV1> {
    wait_for_sidecar_ready_with_inspection(port, credential, timeout, is_cancelled, || {
        OsProcessControl
            .leader_exited(child)
            .map_err(|_| sidecar_inspection_error())
    })
}

fn wait_for_state_owned_sidecar_ready<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    port: u16,
    credential: &NativeInstanceCredential,
    timeout: Duration,
    startup_epoch: u64,
) -> HostResult<NegotiatedContractV1> {
    wait_for_sidecar_ready_with_inspection(
        port,
        credential,
        timeout,
        || startup_cancelled(state, startup_epoch),
        || {
            let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
            let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
            let child = managed.child_mut()?;
            match control.leader_exited(child) {
                Ok(exited) => Ok(exited),
                Err(_) => {
                    managed.mark_cleanup_pending();
                    Err(sidecar_inspection_error())
                }
            }
        },
    )
}

fn wait_for_sidecar_ready_with_inspection(
    port: u16,
    credential: &NativeInstanceCredential,
    timeout: Duration,
    is_cancelled: impl Fn() -> bool,
    mut child_exited: impl FnMut() -> HostResult<bool>,
) -> HostResult<NegotiatedContractV1> {
    let deadline = Instant::now() + timeout;
    loop {
        if is_cancelled() {
            return Err(sidecar_start_cancelled_error());
        }
        if child_exited()? {
            return Err(NativeHostError::new(
                "sidecar_exited_during_startup",
                "The OpenEvo Desktop sidecar exited before it became ready.",
            ));
        }
        if check_sidecar_health(port, credential).is_ok() {
            let contract = check_sidecar_contract(port)?;
            let session_token = EncodedSecret::new(&credential.session_token);
            check_sidecar_session_binding(port, session_token.expose())?;
            return Ok(contract);
        }
        if Instant::now() >= deadline {
            return Err(NativeHostError::new(
                "sidecar_startup_timeout",
                "The OpenEvo Desktop sidecar did not become ready in time.",
            ));
        }
        thread::sleep(SIDECAR_HEALTH_POLL_INTERVAL);
    }
}

fn sidecar_health_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_health_unavailable",
        "The OpenEvo Desktop sidecar did not prove its native instance identity.",
    )
}

fn sidecar_session_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_session_unavailable",
        "The OpenEvo Desktop sidecar did not prove its private session binding.",
    )
}

fn sidecar_contract_incompatible_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_contract_incompatible",
        "The OpenEvo Desktop sidecar does not match the frozen Local API contract.",
    )
}

fn workspace_selection_error() -> NativeHostError {
    NativeHostError::new(
        "workspace_selection_invalid",
        "OpenEvo Desktop could not use the selected research folder.",
    )
}

fn workspace_selection_cancelled_error() -> NativeHostError {
    NativeHostError::new(
        "workspace_selection_cancelled",
        "No research folder was selected.",
    )
}

fn workspace_import_error() -> NativeHostError {
    NativeHostError::new(
        "workspace_import_failed",
        "OpenEvo Desktop could not prepare the selected research folder.",
    )
}

#[cfg(test)]
fn terminate_process_group(
    child: &mut Child,
    process_group: i32,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<Option<ExitStatus>> {
    let mut authority = GroupSignalAuthority::Anchored;
    terminate_process_group_with(
        &OsProcessControl,
        child,
        process_group,
        &mut authority,
        term_timeout,
        kill_timeout,
    )
}

fn terminate_process_group_with<C: ProcessControl>(
    control: &C,
    child: &mut Child,
    process_group: i32,
    signal_authority: &mut GroupSignalAuthority,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<Option<ExitStatus>> {
    if process_group <= 0 {
        return Err(sidecar_stop_error());
    }
    if *signal_authority == GroupSignalAuthority::Finalizing {
        return control
            .reap_leader(child)
            .ok()
            .flatten()
            .map(Some)
            .ok_or_else(sidecar_stop_error);
    }
    let mut control_failed = false;
    if signal_verified_process_group(control, child, process_group, libc::SIGTERM).is_err() {
        control_failed = true;
    }
    let terminated =
        match wait_for_process_group_exit_with(control, child, process_group, term_timeout) {
            Ok(exited) => exited,
            Err(_) => {
                control_failed = true;
                false
            }
        };
    if terminated && !control_failed {
        return finalize_process_group_leader(control, child, signal_authority);
    }
    if signal_verified_process_group(control, child, process_group, libc::SIGKILL).is_err() {
        control_failed = true;
    }
    let killed = match wait_for_process_group_exit_with(control, child, process_group, kill_timeout)
    {
        Ok(exited) => exited,
        Err(_) => {
            control_failed = true;
            false
        }
    };
    if killed && !control_failed {
        return finalize_process_group_leader(control, child, signal_authority);
    }
    Err(sidecar_stop_error())
}

fn signal_verified_process_group<C: ProcessControl>(
    control: &C,
    child: &Child,
    process_group: i32,
    signal: libc::c_int,
) -> std::io::Result<()> {
    control.leader_exited(child)?;
    control.signal_group(process_group, signal)
}

fn wait_for_process_group_exit_with<C: ProcessControl>(
    control: &C,
    child: &mut Child,
    process_group: i32,
    timeout: Duration,
) -> std::io::Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        let leader_exited = control.leader_exited(child)?;
        let has_other_members =
            control.group_has_members_except_leader(process_group, child.id() as i32)?;
        if leader_exited && !has_other_members {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        control.sleep(SIDECAR_STOP_POLL_INTERVAL);
    }
}

fn finalize_process_group_leader<C: ProcessControl>(
    control: &C,
    child: &mut Child,
    signal_authority: &mut GroupSignalAuthority,
) -> HostResult<Option<ExitStatus>> {
    *signal_authority = GroupSignalAuthority::Finalizing;
    control
        .reap_leader(child)
        .ok()
        .flatten()
        .map(Some)
        .ok_or_else(sidecar_stop_error)
}

#[cfg(test)]
fn process_group_exists(process_group: i32) -> HostResult<bool> {
    let result = unsafe { libc::kill(-process_group, 0) };
    if result == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    match error.raw_os_error() {
        Some(libc::ESRCH) => Ok(false),
        Some(libc::EPERM) => Ok(true),
        _ => Err(sidecar_inspection_error()),
    }
}

fn sidecar_stop_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_stop_failed_owned",
        "OpenEvo Desktop retained ownership because bounded sidecar cleanup was not confirmed.",
    )
}

fn encode_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        encoded.push(HEX[(byte >> 4) as usize] as char);
        encoded.push(HEX[(byte & 0x0f) as usize] as char);
    }
    encoded
}

fn is_lower_hex(value: u8) -> bool {
    value.is_ascii_digit() || (b'a'..=b'f').contains(&value)
}

fn decode_hex_32(value: &str) -> Option<[u8; 32]> {
    if value.len() != 64 || !value.bytes().all(is_lower_hex) {
        return None;
    }
    let mut decoded = [0_u8; 32];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        decoded[index] = (decode_hex_nibble(pair[0])? << 4) | decode_hex_nibble(pair[1])?;
    }
    Some(decoded)
}

fn decode_hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        _ => None,
    }
}

#[cfg(test)]
fn cleanup_managed_sidecar(managed: &mut ManagedSidecar) -> HostResult<()> {
    cleanup_managed_sidecar_with(&OsProcessControl, managed)
}

fn cleanup_managed_sidecar_with<C: ProcessControl>(
    control: &C,
    managed: &mut ManagedSidecar,
) -> HostResult<()> {
    cleanup_managed_sidecar_with_bounds(
        control,
        managed,
        SIDECAR_TERM_TIMEOUT,
        SIDECAR_KILL_TIMEOUT,
    )
}

fn cleanup_managed_sidecar_with_bounds<C: ProcessControl>(
    control: &C,
    managed: &mut ManagedSidecar,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<()> {
    if managed.spawn_pending {
        managed.mark_cleanup_pending();
        return Err(sidecar_stop_error());
    }
    let process_result = if managed.process_cleanup_confirmed {
        Ok(())
    } else if let Some(child) = managed.child.as_mut() {
        terminate_process_group_with(
            control,
            child,
            managed.process_group,
            &mut managed.group_signal_authority,
            term_timeout,
            kill_timeout,
        )
        .map(|_| ())
    } else {
        Ok(())
    };
    let result = process_result.and_then(|()| {
        managed.process_cleanup_confirmed = true;
        cleanup_private_executable(
            managed._private_launch_dir.as_mut(),
            managed.verified_executable.as_mut(),
        )
    });
    if let Err(error) = result {
        managed.mark_cleanup_pending();
        Err(error)
    } else {
        Ok(())
    }
}

fn remove_cleaned_sidecar(
    state: &DesktopHostState,
    sidecar: &mut Option<ManagedSidecar>,
) -> HostResult<()> {
    let handoff = {
        let active = lock_spawn_handoff_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
        active.as_ref().cloned()
    };
    if let Some(handoff) = handoff {
        clear_spawn_handoff(state, &handoff)?;
    }
    abort_parent_liveness(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if let Some(managed) = sidecar.take() {
        drop(managed);
    }
    reset_launch_state_to_idle(state);
    Ok(())
}

fn cleanup_sidecar_on_exit(state: &DesktopHostState) {
    cleanup_sidecar_on_exit_with(
        state,
        &OsProcessControl,
        SIDECAR_STATE_LOCK_TIMEOUT,
        SIDECAR_EXIT_EMERGENCY_TERM_GRACE,
    );
}

fn cleanup_sidecar_on_exit_with<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    lock_timeout: Duration,
    _emergency_term_grace: Duration,
) {
    state.shutdown_requested.store(true, Ordering::Release);
    advance_cancellation(state);
    let _ = abort_parent_liveness(state, lock_timeout);
    let _ = cleanup_spawn_handoff_with_bounds(
        state,
        control,
        lock_timeout,
        _emergency_term_grace,
        _emergency_term_grace,
    );
    let Ok(mut sidecar) = lock_sidecar_bounded(state, lock_timeout) else {
        return;
    };
    let Some(managed) = sidecar.as_mut() else {
        return;
    };
    if cleanup_managed_sidecar_with(control, managed).is_ok() {
        let _ = remove_cleaned_sidecar(state, &mut sidecar);
    }
}

fn lock_parent_liveness_bounded(
    state: &DesktopHostState,
    timeout: Duration,
) -> HostResult<MutexGuard<'_, Option<File>>> {
    let deadline = Instant::now() + timeout;
    loop {
        match state.parent_liveness.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(TryLockError::Poisoned(poisoned)) => {
                let guard = poisoned.into_inner();
                state.parent_liveness.clear_poison();
                return Ok(guard);
            }
            Err(TryLockError::WouldBlock) if Instant::now() < deadline => {
                thread::sleep(SIDECAR_STOP_POLL_INTERVAL);
            }
            Err(TryLockError::WouldBlock) => return Err(sidecar_state_error()),
        }
    }
}

fn lock_spawn_handoff_bounded(
    state: &DesktopHostState,
    timeout: Duration,
) -> HostResult<MutexGuard<'_, Option<Arc<SpawnHandoff>>>> {
    let deadline = Instant::now() + timeout;
    loop {
        match state.spawn_handoff.try_lock() {
            Ok(guard) => return Ok(guard),
            Err(TryLockError::Poisoned(poisoned)) => {
                let guard = poisoned.into_inner();
                state.spawn_handoff.clear_poison();
                return Ok(guard);
            }
            Err(TryLockError::WouldBlock) if Instant::now() < deadline => {
                thread::sleep(SIDECAR_STOP_POLL_INTERVAL);
            }
            Err(TryLockError::WouldBlock) => return Err(sidecar_state_error()),
        }
    }
}

fn lock_spawn_outcome_bounded(
    handoff: &SpawnHandoff,
    timeout: Duration,
) -> HostResult<MutexGuard<'_, SpawnHandoffOutcome>> {
    let deadline = Instant::now() + timeout;
    loop {
        match handoff.outcome.try_lock() {
            Ok(outcome) => return Ok(outcome),
            Err(TryLockError::Poisoned(poisoned)) => {
                let outcome = poisoned.into_inner();
                handoff.outcome.clear_poison();
                return Ok(outcome);
            }
            Err(TryLockError::WouldBlock) if Instant::now() < deadline => {
                thread::sleep(SIDECAR_STOP_POLL_INTERVAL);
            }
            Err(TryLockError::WouldBlock) => return Err(sidecar_state_error()),
        }
    }
}

fn active_spawn_handoff(
    state: &DesktopHostState,
    startup_epoch: u64,
) -> HostResult<Arc<SpawnHandoff>> {
    let handoff = lock_spawn_handoff_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let handoff = handoff.as_ref().ok_or_else(sidecar_state_error)?;
    if handoff.startup_epoch != startup_epoch {
        return Err(sidecar_state_error());
    }
    Ok(Arc::clone(handoff))
}

fn clear_spawn_handoff(state: &DesktopHostState, expected: &Arc<SpawnHandoff>) -> HostResult<()> {
    let mut handoff = lock_spawn_handoff_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if handoff
        .as_ref()
        .is_some_and(|active| !Arc::ptr_eq(active, expected))
    {
        return Err(sidecar_state_error());
    }
    let outcome = lock_spawn_outcome_bounded(expected, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if !matches!(
        *outcome,
        SpawnHandoffOutcome::Failed | SpawnHandoffOutcome::Transferred
    ) {
        return Err(sidecar_state_error());
    }
    drop(outcome);
    if handoff.is_some() {
        handoff.take();
    }
    Ok(())
}

fn resolve_unstarted_spawn(state: &DesktopHostState, startup_epoch: u64) {
    let handoff = lock_spawn_handoff_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)
        .ok()
        .and_then(|active| active.as_ref().cloned())
        .filter(|handoff| handoff.startup_epoch == startup_epoch);
    let Some(handoff) = handoff else {
        return;
    };
    {
        let Ok(mut outcome) = lock_spawn_outcome_bounded(&handoff, SIDECAR_STATE_LOCK_TIMEOUT)
        else {
            return;
        };
        if matches!(*outcome, SpawnHandoffOutcome::Pending) {
            *outcome = SpawnHandoffOutcome::Failed;
        }
    }
    let mut sidecar = state
        .sidecar
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    state.sidecar.clear_poison();
    if let Some(managed) = sidecar
        .as_mut()
        .filter(|managed| managed.startup_epoch == startup_epoch)
    {
        managed.spawn_pending = false;
    }
    drop(sidecar);
    let _ = clear_spawn_handoff(state, &handoff);
}

fn cleanup_spawn_handoff_with_bounds<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    state_lock_timeout: Duration,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<()> {
    let handoff = {
        let active = lock_spawn_handoff_bounded(state, state_lock_timeout)?;
        active.as_ref().cloned()
    };
    let Some(handoff) = handoff else {
        return Ok(());
    };
    let mut outcome = lock_spawn_outcome_bounded(&handoff, state_lock_timeout)?;
    match &mut *outcome {
        SpawnHandoffOutcome::Pending if startup_cancelled(state, handoff.startup_epoch) => {
            *outcome = SpawnHandoffOutcome::Failed;
        }
        SpawnHandoffOutcome::Spawned(spawned) => {
            terminate_process_group_with(
                control,
                &mut spawned.child,
                spawned.process_group,
                &mut spawned.group_signal_authority,
                term_timeout,
                kill_timeout,
            )?;
            *outcome = SpawnHandoffOutcome::Transferred;
        }
        SpawnHandoffOutcome::Failed | SpawnHandoffOutcome::Transferred => {}
        SpawnHandoffOutcome::Pending | SpawnHandoffOutcome::Spawning => {
            return Err(sidecar_stop_error());
        }
    }
    drop(outcome);
    let mut sidecar = lock_sidecar_bounded(state, state_lock_timeout)?;
    if let Some(managed) = sidecar
        .as_mut()
        .filter(|managed| managed.startup_epoch == handoff.startup_epoch)
    {
        managed.spawn_pending = false;
    }
    drop(sidecar);
    clear_spawn_handoff(state, &handoff)?;
    Ok(())
}

fn install_parent_liveness(state: &DesktopHostState, writer: File) -> HostResult<()> {
    let mut parent_liveness = lock_parent_liveness_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if parent_liveness.is_some() {
        return Err(sidecar_state_error());
    }
    *parent_liveness = Some(writer);
    Ok(())
}

fn abort_parent_liveness(state: &DesktopHostState, timeout: Duration) -> HostResult<()> {
    let mut parent_liveness = lock_parent_liveness_bounded(state, timeout)?;
    drop(parent_liveness.take());
    Ok(())
}

fn lock_sidecar_bounded(
    state: &DesktopHostState,
    timeout: Duration,
) -> HostResult<MutexGuard<'_, Option<ManagedSidecar>>> {
    let deadline = Instant::now() + timeout;
    loop {
        match state.sidecar.try_lock() {
            Ok(sidecar) => return Ok(sidecar),
            Err(TryLockError::Poisoned(poisoned)) => {
                let mut sidecar = poisoned.into_inner();
                if let Some(managed) = sidecar.as_mut() {
                    managed.mark_cleanup_pending();
                }
                state.sidecar.clear_poison();
                return Ok(sidecar);
            }
            Err(TryLockError::WouldBlock) if Instant::now() < deadline => {
                thread::sleep(SIDECAR_STOP_POLL_INTERVAL);
            }
            Err(TryLockError::WouldBlock) => {
                return Err(NativeHostError::new(
                    "sidecar_state_timeout",
                    "OpenEvo Desktop timed out waiting for bounded sidecar state access.",
                ));
            }
        }
    }
}

fn startup_cancelled(state: &DesktopHostState, initial_epoch: u64) -> bool {
    let (epoch, phase) = decode_launch_state(state.launch_state.load(Ordering::Acquire));
    state.shutdown_requested.load(Ordering::Acquire)
        || epoch != initial_epoch
        || phase == LaunchPhase::Cancelled
}

fn sidecar_start_cancelled_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_start_cancelled",
        "OpenEvo Desktop cancelled sidecar startup.",
    )
}

fn sidecar_start_in_progress_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_start_in_progress",
        "OpenEvo Desktop is already starting its local sidecar.",
    )
}

fn advance_cancellation(state: &DesktopHostState) {
    advance_cancellation_with(state, || {});
}

fn advance_cancellation_with(state: &DesktopHostState, after_advance: impl FnOnce()) {
    let next_epoch = loop {
        let current = state.launch_state.load(Ordering::Acquire);
        let (epoch, _) = decode_launch_state(current);
        let next_epoch = epoch.wrapping_add(1) & (u64::MAX >> LAUNCH_PHASE_BITS);
        let cancelled = encode_launch_state(next_epoch, LaunchPhase::Cancelled);
        if state
            .launch_state
            .compare_exchange(current, cancelled, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            break next_epoch;
        }
    };
    state
        .cancellation_epoch
        .store(next_epoch, Ordering::Release);
    after_advance();
}

fn reset_launch_state_to_idle(state: &DesktopHostState) {
    loop {
        let current = state.launch_state.load(Ordering::Acquire);
        let (epoch, phase) = decode_launch_state(current);
        if phase == LaunchPhase::Idle {
            return;
        }
        if state
            .launch_state
            .compare_exchange(
                current,
                encode_launch_state(epoch, LaunchPhase::Idle),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            return;
        }
    }
}

fn spawn_sidecar_gated(
    state: &DesktopHostState,
    startup_epoch: u64,
    spawn: impl FnOnce() -> std::io::Result<Child>,
) -> HostResult<()> {
    if startup_cancelled(state, startup_epoch) {
        resolve_unstarted_spawn(state, startup_epoch);
        return Err(sidecar_start_cancelled_error());
    }
    let handoff = active_spawn_handoff(state, startup_epoch)?;
    let mut outcome = lock_spawn_outcome_bounded(&handoff, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if !matches!(*outcome, SpawnHandoffOutcome::Pending) {
        return Err(sidecar_state_error());
    }
    if state
        .launch_state
        .compare_exchange(
            encode_launch_state(startup_epoch, LaunchPhase::Reserved),
            encode_launch_state(startup_epoch, LaunchPhase::Spawning),
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .is_err()
    {
        *outcome = SpawnHandoffOutcome::Failed;
        drop(outcome);
        resolve_unstarted_spawn(state, startup_epoch);
        return Err(if startup_cancelled(state, startup_epoch) {
            sidecar_start_cancelled_error()
        } else {
            sidecar_state_error()
        });
    }
    *outcome = SpawnHandoffOutcome::Spawning;
    *outcome = match spawn() {
        Ok(child) => {
            let pid = child.id() as libc::pid_t;
            let identity = sidecar_process_birth_identity(pid).ok().filter(
                |(process_group, session_id, _)| *process_group == pid && *session_id == pid,
            );
            SpawnHandoffOutcome::Spawned(SpawnedHandoffProcess {
                child,
                process_group: identity
                    .as_ref()
                    .map_or(pid, |(process_group, _, _)| *process_group),
                session_id: identity
                    .as_ref()
                    .map_or(pid, |(_, session_id, _)| *session_id),
                birth_identity: identity.map(|(_, _, birth_identity)| birth_identity),
                group_signal_authority: GroupSignalAuthority::Anchored,
            })
        }
        Err(_) => SpawnHandoffOutcome::Failed,
    };
    drop(outcome);

    let (mut sidecar, sidecar_was_poisoned) = match state.sidecar.lock() {
        Ok(sidecar) => (sidecar, false),
        Err(poisoned) => (poisoned.into_inner(), true),
    };
    state.sidecar.clear_poison();
    let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
    if managed.startup_epoch != startup_epoch || managed.child.is_some() {
        return Err(sidecar_state_error());
    }
    let mut outcome = lock_spawn_outcome_bounded(&handoff, SIDECAR_STATE_LOCK_TIMEOUT)?;
    managed.spawn_pending = false;
    match std::mem::replace(&mut *outcome, SpawnHandoffOutcome::Transferred) {
        SpawnHandoffOutcome::Spawned(spawned) => {
            managed.status.pid = Some(spawned.child.id());
            managed.process_group = spawned.process_group;
            managed.session_id = spawned.session_id;
            managed.birth_identity = spawned.birth_identity;
            managed.group_signal_authority = spawned.group_signal_authority;
            managed.child = Some(spawned.child);
        }
        SpawnHandoffOutcome::Failed => {
            drop(outcome);
            clear_spawn_handoff(state, &handoff)?;
            return Err(NativeHostError::new(
                "sidecar_spawn_failed",
                "OpenEvo Desktop could not start its local sidecar.",
            ));
        }
        other @ (SpawnHandoffOutcome::Pending
        | SpawnHandoffOutcome::Spawning
        | SpawnHandoffOutcome::Transferred) => {
            *outcome = other;
            return Err(sidecar_state_error());
        }
    }
    drop(outcome);
    clear_spawn_handoff(state, &handoff)?;
    let pid = managed.child.as_ref().ok_or_else(sidecar_state_error)?.id() as i32;
    let identity_verified = managed.process_group == pid
        && managed.session_id == pid
        && managed.birth_identity.is_some();
    if identity_verified {
        eprintln!(
            "{SIDECAR_PROCESS_MARKER} {} {pid} {} {} {}",
            encode_hex(&managed.instance_id),
            managed.process_group,
            managed.session_id,
            managed
                .birth_identity
                .as_deref()
                .expect("verified sidecar identity has a birth identity"),
        );
    }
    if !identity_verified {
        managed.mark_cleanup_pending();
        Err(sidecar_inspection_error())
    } else if sidecar_was_poisoned {
        managed.mark_cleanup_pending();
        Err(sidecar_state_error())
    } else if startup_cancelled(state, startup_epoch) {
        managed.mark_cleanup_pending();
        Err(sidecar_start_cancelled_error())
    } else {
        Ok(())
    }
}

fn publish_sidecar_gated(
    state: &DesktopHostState,
    startup_epoch: u64,
    bootstrap: SidecarBootstrapState,
) -> HostResult<DesktopBootstrapContextV1> {
    publish_sidecar_gated_with(
        state,
        startup_epoch,
        bootstrap,
        SIDECAR_STATE_LOCK_TIMEOUT,
        || {},
    )
}

fn publish_sidecar_gated_with(
    state: &DesktopHostState,
    startup_epoch: u64,
    bootstrap: SidecarBootstrapState,
    state_lock_timeout: Duration,
    before_state_lock: impl FnOnce(),
) -> HostResult<DesktopBootstrapContextV1> {
    if startup_cancelled(state, startup_epoch) {
        return Err(sidecar_start_cancelled_error());
    }
    before_state_lock();
    let mut sidecar = lock_sidecar_bounded(state, state_lock_timeout)?;
    let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
    if managed.lifecycle != ManagedLifecycle::Starting || managed.child.is_none() {
        return Err(sidecar_state_error());
    }
    state
        .launch_state
        .compare_exchange(
            encode_launch_state(startup_epoch, LaunchPhase::Spawning),
            encode_launch_state(startup_epoch, LaunchPhase::Published),
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .map_err(|_| {
            if startup_cancelled(state, startup_epoch) {
                sidecar_start_cancelled_error()
            } else {
                sidecar_state_error()
            }
        })?;
    managed.lifecycle = ManagedLifecycle::Running;
    managed.status.state = "running".to_string();
    let port = managed.status.port.ok_or_else(sidecar_state_error)?;
    managed.status.url = Some(format!("http://127.0.0.1:{port}/openevo"));
    managed.bootstrap = Some(bootstrap);
    managed.bootstrap_context()
}

fn reserve_starting_sidecar(state: &DesktopHostState, managed: ManagedSidecar) -> HostResult<()> {
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if sidecar.is_some() {
        return Err(sidecar_start_in_progress_error());
    }
    state
        .launch_state
        .compare_exchange(
            encode_launch_state(managed.startup_epoch, LaunchPhase::Idle),
            encode_launch_state(managed.startup_epoch, LaunchPhase::Reserved),
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .map_err(|_| {
            if startup_cancelled(state, managed.startup_epoch) {
                sidecar_start_cancelled_error()
            } else {
                sidecar_state_error()
            }
        })?;
    let mut spawn_handoff = match lock_spawn_handoff_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT) {
        Ok(handoff) => handoff,
        Err(error) => {
            reset_launch_state_to_idle(state);
            return Err(error);
        }
    };
    if spawn_handoff.is_some() {
        reset_launch_state_to_idle(state);
        return Err(sidecar_state_error());
    }
    *spawn_handoff = Some(Arc::new(SpawnHandoff {
        startup_epoch: managed.startup_epoch,
        outcome: Mutex::new(SpawnHandoffOutcome::Pending),
    }));
    *sidecar = Some(managed);
    Ok(())
}

fn fail_state_owned_startup<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    startup_error: NativeHostError,
) -> NativeHostError {
    fail_state_owned_startup_with_bounds(
        state,
        control,
        startup_error,
        SIDECAR_STATE_LOCK_TIMEOUT,
        SIDECAR_TERM_TIMEOUT,
        SIDECAR_KILL_TIMEOUT,
    )
}

fn fail_state_owned_startup_with_bounds<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    startup_error: NativeHostError,
    state_lock_timeout: Duration,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> NativeHostError {
    let _ = abort_parent_liveness(state, state_lock_timeout);
    if let Err(error) = cleanup_spawn_handoff_with_bounds(
        state,
        control,
        state_lock_timeout,
        term_timeout,
        kill_timeout,
    ) {
        return error;
    }
    let mut sidecar = match lock_sidecar_bounded(state, state_lock_timeout) {
        Ok(sidecar) => sidecar,
        Err(error) => return error,
    };
    let Some(managed) = sidecar.as_mut() else {
        return startup_error;
    };
    if managed.lifecycle == ManagedLifecycle::Running {
        return sidecar_state_error();
    }
    match cleanup_managed_sidecar_with_bounds(control, managed, term_timeout, kill_timeout) {
        Ok(()) => match remove_cleaned_sidecar(state, &mut sidecar) {
            Ok(()) => startup_error,
            Err(error) => error,
        },
        Err(error) => error,
    }
}

fn validate_state_owned_executable(state: &DesktopHostState) -> HostResult<()> {
    let sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_ref().ok_or_else(sidecar_state_error)?;
    if let Some(executable) = managed.verified_executable.as_ref() {
        executable.validate()?;
    }
    Ok(())
}

fn finalize_state_owned_private_executable(state: &DesktopHostState) -> HostResult<()> {
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
    finalize_private_executable_after_spawn(
        managed._private_launch_dir.as_ref(),
        managed.verified_executable.as_mut(),
    )
}

fn write_state_owned_credential(
    state: &DesktopHostState,
    credential: &NativeInstanceCredential,
) -> HostResult<()> {
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
    credential.write_to_child(managed.child_mut()?)
}

fn wait_for_startup_to_finish<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    timeout: Duration,
) -> HostResult<()> {
    if !state.startup_in_progress.load(Ordering::Acquire) {
        return Ok(());
    }
    let deadline = Instant::now() + timeout;
    while state.startup_in_progress.load(Ordering::Acquire) {
        if Instant::now() >= deadline {
            return Err(NativeHostError::new(
                "sidecar_state_timeout",
                "OpenEvo Desktop timed out waiting for bounded sidecar state access.",
            ));
        }
        control.sleep(SIDECAR_STOP_POLL_INTERVAL);
    }
    Ok(())
}

fn host_status_inner(state: &DesktopHostState) -> HostResult<HostStatus> {
    host_status_inner_with(state, &OsProcessControl)
}

fn host_status_inner_with<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
) -> HostResult<HostStatus> {
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let Some(managed) = sidecar.as_mut() else {
        return Ok(stopped_host_status());
    };
    if managed.lifecycle == ManagedLifecycle::CleanupPending {
        return Ok(managed.host_status());
    }
    if managed.child.is_none() {
        return Ok(managed.host_status());
    }
    match control.leader_exited(managed.child_mut()?) {
        Ok(true) => {
            managed.status.state = "exited".to_string();
            managed.status.url = None;
            if cleanup_managed_sidecar_with(control, managed).is_ok() {
                let status = managed.host_status();
                remove_cleaned_sidecar(state, &mut sidecar)?;
                Ok(status)
            } else {
                Ok(managed.host_status())
            }
        }
        Ok(false) => Ok(managed.host_status()),
        Err(_) => {
            managed.mark_cleanup_pending();
            Err(sidecar_inspection_error())
        }
    }
}

fn ensure_running_sidecar_monitor(
    state: &DesktopHostState,
    expected_context: &DesktopBootstrapContextV1,
) -> HostResult<()> {
    let instance_id = {
        let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
        let managed = sidecar.as_mut().ok_or_else(sidecar_state_error)?;
        if managed.lifecycle != ManagedLifecycle::Running || managed.child.is_none() {
            return Err(sidecar_state_error());
        }
        let current_context = managed.bootstrap_context()?;
        if current_context.endpoint != expected_context.endpoint
            || current_context.session_token != expected_context.session_token
        {
            return Err(sidecar_state_error());
        }
        if managed.monitor_started {
            return Ok(());
        }
        managed.monitor_started = true;
        managed.instance_id
    };
    let monitor_state = state.clone();
    if thread::Builder::new()
        .name("openevo-sidecar-monitor".to_string())
        .spawn(move || monitor_running_sidecar(monitor_state, instance_id))
        .is_err()
    {
        let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
        if let Some(managed) = sidecar
            .as_mut()
            .filter(|managed| managed.instance_id == instance_id)
        {
            managed.monitor_started = false;
            managed.mark_cleanup_pending();
            abort_parent_liveness(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
            cleanup_managed_sidecar_with(&OsProcessControl, managed)?;
            remove_cleaned_sidecar(state, &mut sidecar)?;
        }
        return Err(NativeHostError::new(
            "sidecar_monitor_failed",
            "OpenEvo Desktop could not monitor its local sidecar process.",
        ));
    }
    Ok(())
}

fn monitor_running_sidecar(state: DesktopHostState, instance_id: [u8; INSTANCE_ID_BYTES]) {
    loop {
        let mut sidecar = match lock_sidecar_bounded(&state, SIDECAR_STATE_LOCK_TIMEOUT) {
            Ok(sidecar) => sidecar,
            Err(_) => {
                thread::sleep(SIDECAR_MONITOR_POLL_INTERVAL);
                continue;
            }
        };
        let Some(managed) = sidecar.as_mut() else {
            return;
        };
        if managed.instance_id != instance_id || !managed.monitor_started {
            return;
        }
        let cleanup_required = match managed.lifecycle {
            ManagedLifecycle::Starting => false,
            ManagedLifecycle::CleanupPending => true,
            ManagedLifecycle::Running => match managed
                .child
                .as_ref()
                .ok_or_else(sidecar_state_error)
                .and_then(|child| {
                    OsProcessControl
                        .leader_exited(child)
                        .map_err(|_| sidecar_inspection_error())
                }) {
                Ok(false) => false,
                Ok(true) => {
                    managed.status.state = "exited".to_string();
                    managed.status.url = None;
                    true
                }
                Err(_) => {
                    managed.mark_cleanup_pending();
                    true
                }
            },
        };
        if cleanup_required && cleanup_managed_sidecar_with(&OsProcessControl, managed).is_ok() {
            if remove_cleaned_sidecar(&state, &mut sidecar).is_ok() {
                return;
            }
            if let Some(managed) = sidecar
                .as_mut()
                .filter(|managed| managed.instance_id == instance_id)
            {
                managed.mark_cleanup_pending();
            }
        }
        drop(sidecar);
        thread::sleep(SIDECAR_MONITOR_POLL_INTERVAL);
    }
}

fn start_sidecar_inner(
    state: &DesktopHostState,
    policy: LaunchPolicy,
    bundled_path: Option<&Path>,
) -> HostResult<DesktopBootstrapContextV1> {
    let context = start_sidecar_inner_with(state, policy, bundled_path, &OsProcessControl)?;
    ensure_running_sidecar_monitor(state, &context)?;
    Ok(context)
}

fn start_sidecar_inner_with<C: ProcessControl>(
    state: &DesktopHostState,
    policy: LaunchPolicy,
    bundled_path: Option<&Path>,
    control: &C,
) -> HostResult<DesktopBootstrapContextV1> {
    if state.shutdown_requested.load(Ordering::Acquire) {
        return Err(NativeHostError::new(
            "sidecar_host_shutting_down",
            "OpenEvo Desktop is shutting down its native sidecar host.",
        ));
    }
    let (_startup_claim, startup_epoch) = StartupClaim::acquire(state)?;
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if let Some(managed) = sidecar.as_mut() {
        if managed.lifecycle == ManagedLifecycle::CleanupPending {
            return Err(sidecar_stop_error());
        }
        if managed.lifecycle == ManagedLifecycle::Starting {
            return Err(sidecar_start_in_progress_error());
        }
        match control.leader_exited(managed.child_mut()?) {
            Ok(false) => {
                let port = managed.status.port.ok_or_else(sidecar_state_error)?;
                let validation = (|| {
                    let bootstrap = managed.bootstrap.as_ref().ok_or_else(sidecar_state_error)?;
                    check_sidecar_health_for_instance(
                        port,
                        &managed.instance_id,
                        &bootstrap.readiness_credential.0,
                    )?;
                    let negotiated = check_sidecar_contract(port)?;
                    if negotiated != bootstrap.negotiated_contract {
                        return Err(sidecar_contract_incompatible_error());
                    }
                    let session_token = EncodedSecret::new(&bootstrap.session_credential.0);
                    check_sidecar_session_binding(port, session_token.expose())
                })();
                if validation.is_ok() {
                    return managed.bootstrap_context();
                }
                managed.mark_cleanup_pending();
                if cleanup_managed_sidecar_with(control, managed).is_err() {
                    return Err(sidecar_stop_error());
                }
                remove_cleaned_sidecar(state, &mut sidecar)?;
            }
            Ok(true) => {
                if cleanup_managed_sidecar_with(control, managed).is_err() {
                    return Err(sidecar_stop_error());
                }
                remove_cleaned_sidecar(state, &mut sidecar)?;
            }
            Err(_) => {
                managed.mark_cleanup_pending();
                return Err(sidecar_inspection_error());
            }
        }
    }
    drop(sidecar);

    let allocated = allocate_sidecar_listener()?;
    let port = allocated.port;
    let mut launch = sidecar_launch_spec(policy, bundled_path, allocated.port)?;
    if let Some(executable) = launch.verified_executable.as_ref() {
        executable.validate()?;
    }
    let mut credential = NativeInstanceCredential::generate()?;
    let mut prepared = command_from_launch_spec(&launch, &allocated.listener)?;
    let parent_liveness_writer = prepared.take_parent_liveness_writer()?;
    let status = LifecycleStatus {
        state: "starting".to_string(),
        port: Some(port),
        pid: None,
        url: None,
    };
    let managed = ManagedSidecar {
        status,
        bootstrap: None,
        lifecycle: ManagedLifecycle::Starting,
        startup_epoch,
        instance_id: credential.instance_id,
        monitor_started: false,
        spawn_pending: true,
        child: None,
        process_group: 0,
        session_id: 0,
        birth_identity: None,
        group_signal_authority: GroupSignalAuthority::Anchored,
        process_cleanup_confirmed: false,
        _private_launch_dir: launch.private_launch_dir.take(),
        verified_executable: launch.verified_executable.take(),
        _listener: allocated.listener,
    };
    reserve_starting_sidecar(state, managed)?;
    if let Err(error) = install_parent_liveness(state, parent_liveness_writer) {
        resolve_unstarted_spawn(state, startup_epoch);
        return Err(fail_state_owned_startup(state, control, error));
    }
    if let Err(error) = spawn_sidecar_gated(state, startup_epoch, || prepared.spawn()) {
        return Err(fail_state_owned_startup(state, control, error));
    }
    if let Err(error) = finalize_state_owned_private_executable(state) {
        return Err(fail_state_owned_startup(state, control, error));
    }
    if let Err(error) = validate_state_owned_executable(state) {
        return Err(fail_state_owned_startup(state, control, error));
    }
    if let Err(error) = write_state_owned_credential(state, &credential) {
        return Err(fail_state_owned_startup(state, control, error));
    }
    let negotiated_contract = match wait_for_state_owned_sidecar_ready(
        state,
        control,
        port,
        &credential,
        SIDECAR_STARTUP_TIMEOUT,
        startup_epoch,
    ) {
        Ok(contract) => contract,
        Err(error) => return Err(fail_state_owned_startup(state, control, error)),
    };
    let bootstrap = SidecarBootstrapState {
        session_credential: credential.take_session_credential(),
        readiness_credential: ReadinessCredential(std::mem::replace(
            &mut credential.readiness_key,
            [0_u8; READINESS_KEY_BYTES],
        )),
        handoff_credential: credential.take_handoff_credential(),
        negotiated_contract,
    };
    match publish_sidecar_gated(state, startup_epoch, bootstrap) {
        Ok(context) => Ok(context),
        Err(error) => Err(fail_state_owned_startup(state, control, error)),
    }
}

fn stop_sidecar_inner(state: &DesktopHostState) -> HostResult<HostStatus> {
    stop_sidecar_inner_with(state, &OsProcessControl, SIDECAR_STATE_LOCK_TIMEOUT)
}

fn renderer_ready_inner(state: &DesktopHostState, openapi_sha256: &str) -> HostResult<()> {
    renderer_ready_inner_with(state, openapi_sha256, &OsProcessControl)
}

fn renderer_ready_inner_with<C: ProcessControl>(
    state: &DesktopHostState,
    openapi_sha256: &str,
    control: &C,
) -> HostResult<()> {
    if openapi_sha256 != DESKTOP_LOCAL_API_OPENAPI_SHA256 {
        return Err(sidecar_contract_incompatible_error());
    }
    let (instance_id, port, session_token) = {
        let sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
        let managed = sidecar.as_ref().ok_or_else(sidecar_state_error)?;
        if managed.lifecycle != ManagedLifecycle::Running {
            return Err(sidecar_state_error());
        }
        let child = managed.child.as_ref().ok_or_else(sidecar_state_error)?;
        if control
            .leader_exited(child)
            .map_err(|_| sidecar_inspection_error())?
        {
            return Err(sidecar_state_error());
        }
        let bootstrap = managed.bootstrap.as_ref().ok_or_else(sidecar_state_error)?;
        if bootstrap.negotiated_contract.major != 1
            || bootstrap.negotiated_contract.openapi_sha256 != openapi_sha256
            || bootstrap.negotiated_contract.provider_kind != "desktop_sidecar"
        {
            return Err(sidecar_contract_incompatible_error());
        }
        (
            managed.instance_id,
            managed.status.port.ok_or_else(sidecar_state_error)?,
            EncodedSecret::new(&bootstrap.session_credential.0),
        )
    };

    check_sidecar_session_binding(port, session_token.expose())?;

    let sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let managed = sidecar.as_ref().ok_or_else(sidecar_state_error)?;
    if managed.instance_id != instance_id || managed.lifecycle != ManagedLifecycle::Running {
        return Err(sidecar_state_error());
    }
    let child = managed.child.as_ref().ok_or_else(sidecar_state_error)?;
    if control
        .leader_exited(child)
        .map_err(|_| sidecar_inspection_error())?
    {
        return Err(sidecar_state_error());
    }
    let bootstrap = managed.bootstrap.as_ref().ok_or_else(sidecar_state_error)?;
    if managed.status.port != Some(port)
        || bootstrap.negotiated_contract.major != 1
        || bootstrap.negotiated_contract.openapi_sha256 != openapi_sha256
        || bootstrap.negotiated_contract.provider_kind != "desktop_sidecar"
    {
        return Err(sidecar_contract_incompatible_error());
    }
    let pid = child.id() as i32;
    let (observed_process_group, observed_session_id, birth_identity) =
        sidecar_process_birth_identity(pid).map_err(|_| sidecar_inspection_error())?;
    if managed.process_group != pid
        || managed.session_id != pid
        || observed_process_group != managed.process_group
        || observed_session_id != managed.session_id
        || managed.birth_identity.as_deref() != Some(&birth_identity)
    {
        return Err(sidecar_inspection_error());
    }
    eprintln!("{RENDERER_READY_MARKER} {openapi_sha256}");
    Ok(())
}

fn stop_sidecar_inner_with<C: ProcessControl>(
    state: &DesktopHostState,
    control: &C,
    lock_timeout: Duration,
) -> HostResult<HostStatus> {
    advance_cancellation(state);
    abort_parent_liveness(state, lock_timeout)?;
    wait_for_startup_to_finish(state, control, lock_timeout)?;
    cleanup_spawn_handoff_with_bounds(
        state,
        control,
        lock_timeout,
        SIDECAR_TERM_TIMEOUT,
        SIDECAR_KILL_TIMEOUT,
    )?;
    let mut sidecar = lock_sidecar_bounded(state, lock_timeout)?;
    let Some(managed) = sidecar.as_mut() else {
        reset_launch_state_to_idle(state);
        return Ok(stopped_host_status());
    };
    cleanup_managed_sidecar_with(control, managed)?;
    remove_cleaned_sidecar(state, &mut sidecar)?;
    Ok(stopped_host_status())
}

struct RunRetryRecoveryRoot {
    path: PathBuf,
    directory: File,
    device: u64,
    inode: u64,
}

struct RunRetryRecoveryProcessLock {
    root: RunRetryRecoveryRoot,
    name: CString,
    file: File,
    identity: FileIdentity,
}

impl RunRetryRecoveryProcessLock {
    fn acquire(path: &Path) -> HostResult<Self> {
        let root =
            open_run_retry_recovery_root(path, true)?.ok_or_else(run_retry_recovery_error)?;
        root.validate()?;
        let name = run_retry_recovery_lock_name();
        let previous_identity = run_retry_recovery_identity_at_optional(&root.directory, &name)?;
        if let Some(identity) = previous_identity.as_ref() {
            validate_run_retry_recovery_lock_identity(identity)?;
        }
        let file = openat_file(
            root.directory.as_raw_fd(),
            &name,
            libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            0o600,
        )
        .map_err(|_| run_retry_recovery_error())?;
        let identity = file_identity(&file).map_err(|_| run_retry_recovery_error())?;
        validate_run_retry_recovery_lock_identity(&identity)?;
        validate_anchored_extended_acl(&file).map_err(|_| run_retry_recovery_error())?;
        if previous_identity.is_some_and(|previous| previous != identity)
            || run_retry_recovery_identity_at_optional(&root.directory, &name)?.as_ref()
                != Some(&identity)
        {
            return Err(run_retry_recovery_error());
        }
        root.validate()?;

        let deadline = Instant::now() + RUN_RETRY_RECOVERY_LOCK_TIMEOUT;
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                break;
            }
            let error = std::io::Error::last_os_error();
            match error.raw_os_error() {
                Some(code) if code == libc::EINTR => continue,
                Some(code) if code == libc::EAGAIN || code == libc::EWOULDBLOCK => {
                    let now = Instant::now();
                    if now >= deadline {
                        return Err(run_retry_recovery_error());
                    }
                    thread::sleep(
                        RUN_RETRY_RECOVERY_LOCK_POLL_INTERVAL
                            .min(deadline.saturating_duration_since(now)),
                    );
                }
                _ => return Err(run_retry_recovery_error()),
            }
        }

        let lock = Self {
            root,
            name,
            file,
            identity,
        };
        lock.validate()?;
        Ok(lock)
    }

    fn validate(&self) -> HostResult<()> {
        self.root.validate()?;
        let open_identity = file_identity(&self.file).map_err(|_| run_retry_recovery_error())?;
        validate_run_retry_recovery_lock_identity(&open_identity)?;
        validate_anchored_extended_acl(&self.file).map_err(|_| run_retry_recovery_error())?;
        if open_identity != self.identity
            || run_retry_recovery_identity_at_optional(&self.root.directory, &self.name)?.as_ref()
                != Some(&self.identity)
        {
            return Err(run_retry_recovery_error());
        }
        Ok(())
    }
}

impl Drop for RunRetryRecoveryProcessLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

impl RunRetryRecoveryRoot {
    fn validate(&self) -> HostResult<()> {
        let reopened = open_run_retry_recovery_root(&self.path, false)?
            .ok_or_else(run_retry_recovery_error)?;
        let held = file_identity(&self.directory).map_err(|_| run_retry_recovery_error())?;
        let current = file_identity(&reopened.directory).map_err(|_| run_retry_recovery_error())?;
        if held.device != self.device
            || held.inode != self.inode
            || current.device != self.device
            || current.inode != self.inode
        {
            return Err(run_retry_recovery_error());
        }
        validate_run_retry_recovery_root_identity(&held)
    }
}

struct RunRetryRecoveryTemp<'a> {
    directory: &'a File,
    name: CString,
    file: File,
    published: bool,
}

impl Drop for RunRetryRecoveryTemp<'_> {
    fn drop(&mut self) {
        if self.published {
            return;
        }
        let Ok(open_identity) = file_identity(&self.file) else {
            return;
        };
        let Ok(Some(path_identity)) =
            run_retry_recovery_identity_at_optional(self.directory, &self.name)
        else {
            return;
        };
        if open_identity == path_identity
            && open_identity.mode & FILE_TYPE_MASK == REGULAR_FILE_TYPE
            && open_identity.owner == unsafe { libc::geteuid() }
            && open_identity.links == 1
        {
            let removed =
                unsafe { libc::unlinkat(self.directory.as_raw_fd(), self.name.as_ptr(), 0) };
            if removed == 0 {
                let _ = self.directory.sync_all();
            }
        }
    }
}

fn run_retry_recovery_root_path(app: &tauri::AppHandle) -> HostResult<PathBuf> {
    app.path()
        .app_data_dir()
        .map_err(|_| run_retry_recovery_error())
}

fn with_run_retry_recovery_transaction<T>(
    state: &DesktopHostState,
    path: &Path,
    operation: impl FnOnce() -> HostResult<T>,
) -> HostResult<T> {
    let _thread_guard = state
        .run_retry_recovery
        .lock()
        .map_err(|_| run_retry_recovery_error())?;
    let process_lock = RunRetryRecoveryProcessLock::acquire(path)?;
    let result = operation();
    process_lock.validate()?;
    result
}

fn open_run_retry_recovery_root(
    path: &Path,
    create: bool,
) -> HostResult<Option<RunRetryRecoveryRoot>> {
    if !path.is_absolute() {
        return Err(run_retry_recovery_error());
    }
    let mut names = Vec::new();
    for component in path.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => names.push(name),
            _ => return Err(run_retry_recovery_error()),
        }
    }
    if names.is_empty() {
        return Err(run_retry_recovery_error());
    }

    let mut current = open_directory(Path::new("/")).map_err(|_| run_retry_recovery_error())?;
    validate_run_retry_recovery_parent(&current)?;
    for (index, name) in names.iter().enumerate() {
        let name = CString::new(name.as_bytes()).map_err(|_| run_retry_recovery_error())?;
        let is_root = index + 1 == names.len();
        let next = match openat_file(
            current.as_raw_fd(),
            &name,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        ) {
            Ok(directory) => directory,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && !create => {
                return Ok(None);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let created = unsafe { libc::mkdirat(current.as_raw_fd(), name.as_ptr(), 0o700) };
                if created == -1 {
                    let mkdir_error = std::io::Error::last_os_error();
                    if mkdir_error.kind() != std::io::ErrorKind::AlreadyExists {
                        return Err(run_retry_recovery_error());
                    }
                } else {
                    current.sync_all().map_err(|_| run_retry_recovery_error())?;
                }
                openat_file(
                    current.as_raw_fd(),
                    &name,
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )
                .map_err(|_| run_retry_recovery_error())?
            }
            Err(_) => return Err(run_retry_recovery_error()),
        };
        if is_root {
            validate_run_retry_recovery_root_identity(
                &file_identity(&next).map_err(|_| run_retry_recovery_error())?,
            )?;
        } else {
            validate_run_retry_recovery_parent(&next)?;
        }
        validate_anchored_extended_acl(&next).map_err(|_| run_retry_recovery_error())?;
        current = next;
    }
    let identity = file_identity(&current).map_err(|_| run_retry_recovery_error())?;
    Ok(Some(RunRetryRecoveryRoot {
        path: path.to_path_buf(),
        directory: current,
        device: identity.device,
        inode: identity.inode,
    }))
}

fn validate_run_retry_recovery_parent(directory: &File) -> HostResult<()> {
    let identity = file_identity(directory).map_err(|_| run_retry_recovery_error())?;
    let effective_user = unsafe { libc::geteuid() };
    let root_sticky_boundary = identity.owner == 0 && identity.mode & STICKY_MODE_BIT != 0;
    if identity.mode & FILE_TYPE_MASK != DIRECTORY_FILE_TYPE
        || (identity.owner != 0 && identity.owner != effective_user)
        || (identity.mode & 0o022 != 0 && !root_sticky_boundary)
    {
        return Err(run_retry_recovery_error());
    }
    validate_anchored_extended_acl(directory).map_err(|_| run_retry_recovery_error())
}

fn validate_run_retry_recovery_root_identity(identity: &FileIdentity) -> HostResult<()> {
    if identity.mode & FILE_TYPE_MASK != DIRECTORY_FILE_TYPE
        || identity.owner != unsafe { libc::geteuid() }
        || identity.mode & 0o777 != 0o700
    {
        return Err(run_retry_recovery_error());
    }
    Ok(())
}

fn run_retry_recovery_identity_at_optional(
    directory: &File,
    name: &CString,
) -> HostResult<Option<FileIdentity>> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    let result = unsafe {
        libc::fstatat(
            directory.as_raw_fd(),
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result == 0 {
        return Ok(Some(file_identity_from_stat(unsafe {
            &stat.assume_init()
        })));
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == std::io::ErrorKind::NotFound {
        Ok(None)
    } else {
        Err(run_retry_recovery_error())
    }
}

fn validate_run_retry_recovery_file_identity(identity: &FileIdentity) -> HostResult<()> {
    if identity.mode & FILE_TYPE_MASK != REGULAR_FILE_TYPE
        || identity.owner != unsafe { libc::geteuid() }
        || identity.mode & 0o777 != 0o600
        || identity.links != 1
    {
        return Err(run_retry_recovery_error());
    }
    Ok(())
}

fn validate_run_retry_recovery_lock_identity(identity: &FileIdentity) -> HostResult<()> {
    validate_run_retry_recovery_file_identity(identity)?;
    if identity.size != 0 {
        return Err(run_retry_recovery_error());
    }
    Ok(())
}

fn validate_run_retry_recovery_read_identity(identity: &FileIdentity) -> HostResult<()> {
    validate_run_retry_recovery_file_identity(identity)?;
    if identity.size > RUN_RETRY_RECOVERY_MAX_BYTES as u64 {
        return Err(run_retry_recovery_error());
    }
    Ok(())
}

fn run_retry_recovery_name() -> CString {
    CString::new(RUN_RETRY_RECOVERY_FILE_NAME).expect("recovery file name has no NUL")
}

fn run_retry_recovery_lock_name() -> CString {
    CString::new(RUN_RETRY_RECOVERY_LOCK_FILE_NAME).expect("recovery lock name has no NUL")
}

fn read_run_retry_recovery_from_root(
    root: &RunRetryRecoveryRoot,
    expected_identity: Option<&FileIdentity>,
) -> HostResult<Option<String>> {
    root.validate()?;
    let name = run_retry_recovery_name();
    let Some(path_identity) = run_retry_recovery_identity_at_optional(&root.directory, &name)?
    else {
        return if expected_identity.is_none() {
            Ok(None)
        } else {
            Err(run_retry_recovery_error())
        };
    };
    validate_run_retry_recovery_read_identity(&path_identity)?;
    if expected_identity.is_some_and(|expected| expected != &path_identity) {
        return Err(run_retry_recovery_error());
    }
    let mut file = openat_file(
        root.directory.as_raw_fd(),
        &name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        0,
    )
    .map_err(|_| run_retry_recovery_error())?;
    let opened_identity = file_identity(&file).map_err(|_| run_retry_recovery_error())?;
    if opened_identity != path_identity {
        return Err(run_retry_recovery_error());
    }
    validate_run_retry_recovery_read_identity(&opened_identity)?;
    validate_anchored_extended_acl(&file).map_err(|_| run_retry_recovery_error())?;

    let capacity = usize::try_from(opened_identity.size).map_err(|_| run_retry_recovery_error())?;
    let mut bytes = Vec::with_capacity(capacity);
    (&mut file)
        .take((RUN_RETRY_RECOVERY_MAX_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| run_retry_recovery_error())?;
    if bytes.len() != capacity || bytes.len() > RUN_RETRY_RECOVERY_MAX_BYTES {
        return Err(run_retry_recovery_error());
    }
    let final_open_identity = file_identity(&file).map_err(|_| run_retry_recovery_error())?;
    let final_path_identity = run_retry_recovery_identity_at_optional(&root.directory, &name)?
        .ok_or_else(run_retry_recovery_error)?;
    if final_open_identity != opened_identity || final_path_identity != opened_identity {
        return Err(run_retry_recovery_error());
    }
    root.validate()?;
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| run_retry_recovery_error())
}

fn read_run_retry_recovery_at(path: &Path) -> HostResult<Option<String>> {
    let Some(root) = open_run_retry_recovery_root(path, false)? else {
        return Ok(None);
    };
    read_run_retry_recovery_from_root(&root, None)
}

fn create_run_retry_recovery_temp(
    root: &RunRetryRecoveryRoot,
) -> HostResult<RunRetryRecoveryTemp<'_>> {
    for _ in 0..64 {
        let mut random = [0_u8; 16];
        OsRng
            .try_fill_bytes(&mut random)
            .map_err(|_| run_retry_recovery_error())?;
        let name = CString::new(format!(
            "{RUN_RETRY_RECOVERY_TEMP_PREFIX}{}",
            encode_hex(&random)
        ))
        .expect("recovery temp name has no NUL");
        let file = match openat_file(
            root.directory.as_raw_fd(),
            &name,
            libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        ) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(run_retry_recovery_error()),
        };
        let temp = RunRetryRecoveryTemp {
            directory: &root.directory,
            name,
            file,
            published: false,
        };
        temp.file
            .set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|_| run_retry_recovery_error())?;
        let identity = file_identity(&temp.file).map_err(|_| run_retry_recovery_error())?;
        validate_run_retry_recovery_file_identity(&identity)?;
        if run_retry_recovery_identity_at_optional(&root.directory, &temp.name)?.as_ref()
            != Some(&identity)
        {
            return Err(run_retry_recovery_error());
        }
        return Ok(temp);
    }
    Err(run_retry_recovery_error())
}

fn write_some_run_retry_recovery_at_with<F>(
    path: &Path,
    value: &str,
    after_directory_sync: F,
) -> HostResult<()>
where
    F: FnOnce(&mut File) -> HostResult<()>,
{
    if value.len() > RUN_RETRY_RECOVERY_MAX_BYTES {
        return Err(run_retry_recovery_too_large_error());
    }
    let root = open_run_retry_recovery_root(path, true)?.ok_or_else(run_retry_recovery_error)?;
    root.validate()?;
    let target_name = run_retry_recovery_name();
    let previous_identity = run_retry_recovery_identity_at_optional(&root.directory, &target_name)?;
    if let Some(identity) = previous_identity.as_ref() {
        validate_run_retry_recovery_file_identity(identity)?;
        read_run_retry_recovery_from_root(&root, Some(identity))?;
    }

    let mut temp = create_run_retry_recovery_temp(&root)?;
    temp.file
        .write_all(value.as_bytes())
        .map_err(|_| run_retry_recovery_error())?;
    temp.file.flush().map_err(|_| run_retry_recovery_error())?;
    temp.file
        .sync_all()
        .map_err(|_| run_retry_recovery_error())?;
    let temp_identity = file_identity(&temp.file).map_err(|_| run_retry_recovery_error())?;
    validate_run_retry_recovery_file_identity(&temp_identity)?;
    if temp_identity.size != value.len() as u64
        || run_retry_recovery_identity_at_optional(&root.directory, &temp.name)?.as_ref()
            != Some(&temp_identity)
        || run_retry_recovery_identity_at_optional(&root.directory, &target_name)?
            != previous_identity
    {
        return Err(run_retry_recovery_error());
    }
    root.validate()?;
    let renamed = unsafe {
        libc::renameat(
            root.directory.as_raw_fd(),
            temp.name.as_ptr(),
            root.directory.as_raw_fd(),
            target_name.as_ptr(),
        )
    };
    if renamed == -1 {
        return Err(run_retry_recovery_error());
    }
    temp.published = true;
    root.directory
        .sync_all()
        .map_err(|_| run_retry_recovery_error())?;
    after_directory_sync(&mut temp.file)?;

    let published_identity = file_identity(&temp.file).map_err(|_| run_retry_recovery_error())?;
    validate_run_retry_recovery_file_identity(&published_identity)?;
    if read_run_retry_recovery_from_root(&root, Some(&published_identity))?.as_deref()
        != Some(value)
    {
        return Err(run_retry_recovery_error());
    }
    Ok(())
}

fn write_some_run_retry_recovery_at(path: &Path, value: &str) -> HostResult<()> {
    write_some_run_retry_recovery_at_with(path, value, |_| Ok(()))
}

fn same_run_retry_recovery_identity_after_unlink(
    actual: &FileIdentity,
    expected: &FileIdentity,
) -> bool {
    if expected.links != 1 || actual.links != 0 {
        return false;
    }
    let mut normalized = actual.clone();
    normalized.links = expected.links;
    normalized.changed_seconds = expected.changed_seconds;
    normalized.changed_nanoseconds = expected.changed_nanoseconds;
    &normalized == expected
}

fn clear_run_retry_recovery_at(path: &Path) -> HostResult<()> {
    let Some(root) = open_run_retry_recovery_root(path, false)? else {
        return Ok(());
    };
    root.validate()?;
    let name = run_retry_recovery_name();
    let Some(path_identity) = run_retry_recovery_identity_at_optional(&root.directory, &name)?
    else {
        return Ok(());
    };
    validate_run_retry_recovery_file_identity(&path_identity)?;
    let file = openat_file(
        root.directory.as_raw_fd(),
        &name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        0,
    )
    .map_err(|_| run_retry_recovery_error())?;
    let opened_identity = file_identity(&file).map_err(|_| run_retry_recovery_error())?;
    if opened_identity != path_identity {
        return Err(run_retry_recovery_error());
    }
    validate_run_retry_recovery_file_identity(&opened_identity)?;
    validate_anchored_extended_acl(&file).map_err(|_| run_retry_recovery_error())?;
    root.validate()?;
    if run_retry_recovery_identity_at_optional(&root.directory, &name)?.as_ref()
        != Some(&opened_identity)
    {
        return Err(run_retry_recovery_error());
    }
    if unsafe { libc::unlinkat(root.directory.as_raw_fd(), name.as_ptr(), 0) } == -1 {
        return Err(run_retry_recovery_error());
    }
    root.directory
        .sync_all()
        .map_err(|_| run_retry_recovery_error())?;
    let unlinked_identity = file_identity(&file).map_err(|_| run_retry_recovery_error())?;
    if !same_run_retry_recovery_identity_after_unlink(&unlinked_identity, &opened_identity)
        || run_retry_recovery_identity_at_optional(&root.directory, &name)?.is_some()
    {
        return Err(run_retry_recovery_error());
    }
    root.validate()
}

fn write_run_retry_recovery_at(path: &Path, value: Option<&str>) -> HostResult<()> {
    match value {
        Some(value) => write_some_run_retry_recovery_at(path, value),
        None => clear_run_retry_recovery_at(path),
    }
}

fn compare_and_swap_run_retry_recovery_at(
    state: &DesktopHostState,
    path: &Path,
    expected_value: Option<&str>,
    value: Option<&str>,
) -> HostResult<()> {
    with_run_retry_recovery_transaction(state, path, || {
        if expected_value.is_some_and(|expected| expected.len() > RUN_RETRY_RECOVERY_MAX_BYTES)
            || value.is_some_and(|new_value| new_value.len() > RUN_RETRY_RECOVERY_MAX_BYTES)
        {
            return Err(run_retry_recovery_too_large_error());
        }
        if read_run_retry_recovery_at(path)?.as_deref() != expected_value {
            return Err(run_retry_recovery_conflict_error());
        }
        write_run_retry_recovery_at(path, value)
    })
}

fn run_retry_recovery_error() -> NativeHostError {
    NativeHostError::new(
        "run_retry_recovery_unavailable",
        "OpenEvo Desktop could not securely access saved run retry recovery state.",
    )
}

fn run_retry_recovery_too_large_error() -> NativeHostError {
    NativeHostError::new(
        "run_retry_recovery_too_large",
        "OpenEvo Desktop could not save run retry recovery because it is too large.",
    )
}

fn run_retry_recovery_conflict_error() -> NativeHostError {
    NativeHostError::new(
        "run_retry_recovery_conflict",
        "Saved run retry recovery state changed before this update could be applied.",
    )
}

#[tauri::command]
fn host_status(state: tauri::State<'_, DesktopHostState>) -> HostResult<HostStatus> {
    host_status_inner(&state)
}

#[tauri::command(rename_all = "camelCase")]
fn read_run_retry_recovery(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
) -> HostResult<Option<String>> {
    let root = run_retry_recovery_root_path(&app)?;
    with_run_retry_recovery_transaction(&state, &root, || read_run_retry_recovery_at(&root))
}

#[tauri::command(rename_all = "camelCase")]
fn write_run_retry_recovery(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
    expected_value: Option<String>,
    value: Option<String>,
) -> HostResult<()> {
    let root = run_retry_recovery_root_path(&app)?;
    compare_and_swap_run_retry_recovery_at(
        &state,
        &root,
        expected_value.as_deref(),
        value.as_deref(),
    )
}

#[tauri::command]
fn start_sidecar(
    _app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
) -> HostResult<DesktopBootstrapContextV1> {
    let bundled_path = bundled_sidecar_path();
    start_sidecar_inner(&state, active_launch_policy(), bundled_path.as_deref())
}

#[tauri::command]
fn stop_sidecar(state: tauri::State<'_, DesktopHostState>) -> HostResult<HostStatus> {
    stop_sidecar_inner(&state)
}

#[tauri::command(rename_all = "camelCase")]
fn renderer_ready(
    state: tauri::State<'_, DesktopHostState>,
    openapi_sha256: String,
) -> HostResult<()> {
    renderer_ready_inner(&state, &openapi_sha256)
}

#[tauri::command(rename_all = "camelCase")]
async fn select_project_source(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
    kind: String,
    action_id: String,
    project_id: Option<String>,
) -> HostResult<NativeProjectSourceV1> {
    if kind != "native_folder_snapshot" {
        return Err(workspace_selection_error());
    }
    let expected_instance = active_sidecar_instance(&state)?;
    let picker_claim = PickerClaim::acquire(&state, action_id.clone(), expected_instance)?;
    let operation = picker_claim.operation.clone();
    let selected =
        tauri::async_runtime::spawn_blocking(move || app.dialog().file().blocking_pick_folder())
            .await
            .map_err(|_| workspace_selection_error())?
            .ok_or_else(workspace_selection_cancelled_error)?;
    if operation.is_cancelled() {
        return Err(workspace_selection_cancelled_error());
    }
    let selected = selected
        .into_path()
        .map_err(|_| workspace_selection_error())?;
    let selected = open_selected_directory(&selected)?;
    let ActiveSidecarConnection {
        mut stream,
        handoff_token,
    } = active_sidecar_connection(&state, expected_instance)?;
    let pending_action_id = action_id.clone();
    let pending_project_id = project_id.clone();
    let request_operation = operation.clone();
    let response = tauri::async_runtime::spawn_blocking(move || {
        register_native_workspace_source(
            &mut stream,
            handoff_token.expose(),
            NativeWorkspaceSelection {
                kind: &kind,
                action_id: &action_id,
                selected_path: &selected.path,
                selected_device: selected.device,
                selected_inode: selected.inode,
                project_id: project_id.as_deref(),
            },
            &request_operation,
        )
    })
    .await
    .map_err(|_| workspace_import_error())??;
    let pending = PendingNativeWorkspaceImport {
        sidecar_instance: expected_instance,
        action_id: pending_action_id,
        project_id: pending_project_id,
        source: response.source.clone(),
        lease_token: response.lease_token,
    };
    if let Err(error) = remember_pending_workspace_import(&state, pending.clone()) {
        if let Ok(ActiveSidecarConnection {
            mut stream,
            handoff_token,
        }) = active_sidecar_connection(&state, expected_instance)
        {
            let _ = tauri::async_runtime::spawn_blocking(move || {
                discard_native_workspace_source(&mut stream, handoff_token.expose(), &pending)
            })
            .await;
        }
        return Err(error);
    }
    if operation.is_cancelled() {
        if let Ok(ActiveSidecarConnection {
            mut stream,
            handoff_token,
        }) = active_sidecar_connection(&state, expected_instance)
        {
            let pending_to_discard = pending.clone();
            let discard_result = tauri::async_runtime::spawn_blocking(move || {
                discard_native_workspace_source(
                    &mut stream,
                    handoff_token.expose(),
                    &pending_to_discard,
                )
            })
            .await;
            if matches!(discard_result, Ok(Ok(()))) {
                let _ = take_pending_workspace_import(&state, &pending.action_id);
            }
        }
        return Err(workspace_selection_cancelled_error());
    }
    Ok(response.source)
}

#[tauri::command(rename_all = "camelCase")]
async fn cancel_project_source(
    state: tauri::State<'_, DesktopHostState>,
    action_id: String,
) -> HostResult<()> {
    let Some(operation) = cancel_active_picker(&state, &action_id)? else {
        return Ok(());
    };
    let ActiveSidecarConnection {
        mut stream,
        handoff_token,
    } = match active_sidecar_connection(&state, operation.sidecar_instance) {
        Ok(connection) => connection,
        Err(_) => return Ok(()),
    };
    tauri::async_runtime::spawn_blocking(move || {
        cancel_native_workspace_operation(&mut stream, handoff_token.expose(), &operation)
    })
    .await
    .map_err(|_| workspace_import_error())?
}

#[tauri::command(rename_all = "camelCase")]
async fn settle_project_source(
    state: tauri::State<'_, DesktopHostState>,
    action_id: String,
    outcome: String,
) -> HostResult<()> {
    if !matches!(outcome.as_str(), "adopt" | "discard") {
        return Err(workspace_selection_error());
    }
    let Some(pending) = take_pending_workspace_import(&state, &action_id)? else {
        return Ok(());
    };
    if outcome == "adopt" {
        return Ok(());
    }
    let current_instance = match active_sidecar_instance(&state) {
        Ok(instance) => instance,
        Err(_) => return Ok(()),
    };
    if current_instance != pending.sidecar_instance {
        return Ok(());
    }
    let ActiveSidecarConnection {
        mut stream,
        handoff_token,
    } = match active_sidecar_connection(&state, pending.sidecar_instance) {
        Ok(connection) => connection,
        Err(error) => {
            restore_pending_workspace_import(&state, pending);
            return Err(error);
        }
    };
    let (pending, result) = tauri::async_runtime::spawn_blocking(move || {
        let result = discard_native_workspace_source(&mut stream, handoff_token.expose(), &pending);
        (pending, result)
    })
    .await
    .map_err(|_| workspace_import_error())?;
    if let Err(error) = result {
        restore_pending_workspace_import(&state, pending);
        return Err(error);
    }
    Ok(())
}

fn sidecar_state_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_state_unavailable",
        "OpenEvo Desktop sidecar state is temporarily unavailable.",
    )
}

fn sidecar_inspection_error() -> NativeHostError {
    NativeHostError::new(
        "sidecar_process_inspection_failed",
        "OpenEvo Desktop could not inspect the sidecar process.",
    )
}

fn main() {
    let app = match tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(DesktopHostState::default())
        .invoke_handler(tauri::generate_handler![
            host_status,
            read_run_retry_recovery,
            write_run_retry_recovery,
            start_sidecar,
            stop_sidecar,
            renderer_ready,
            select_project_source,
            cancel_project_source,
            settle_project_source
        ])
        .build(tauri::generate_context!())
    {
        Ok(app) => app,
        Err(_) => {
            eprintln!("OpenEvo Desktop failed to initialize.");
            std::process::exit(1);
        }
    };
    app.run(|app_handle, event| {
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            let state = app_handle.state::<DesktopHostState>();
            cleanup_sidecar_on_exit(&state);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    use std::collections::VecDeque;
    use std::ffi::OsString;
    use std::fs::{self, hard_link, OpenOptions};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::os::unix::net::UnixStream;
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command, ExitStatus, Stdio};
    use std::sync::{mpsc, Arc, Barrier, Mutex};

    fn expect_host_error<T>(result: HostResult<T>) -> NativeHostError {
        match result {
            Err(error) => error,
            Ok(_) => panic!("operation unexpectedly succeeded"),
        }
    }
    use std::thread;
    use std::time::{Duration, Instant};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn test_run_retry_recovery_root(temp: &TempDir) -> PathBuf {
        temp.path().join("app-data")
    }

    fn run_retry_recovery_target(root: &Path) -> PathBuf {
        root.join(RUN_RETRY_RECOVERY_FILE_NAME)
    }

    fn run_retry_recovery_lock_target(root: &Path) -> PathBuf {
        root.join(RUN_RETRY_RECOVERY_LOCK_FILE_NAME)
    }

    #[test]
    fn run_retry_recovery_roundtrips_and_clears_private_state() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let value = r#"{"schema_version":"1","run_id":"run-1"}"#;

        assert_eq!(read_run_retry_recovery_at(&root).unwrap(), None);
        write_run_retry_recovery_at(&root, Some(value)).unwrap();
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some(value)
        );

        let root_metadata = fs::symlink_metadata(&root).unwrap();
        let target_metadata = fs::symlink_metadata(run_retry_recovery_target(&root)).unwrap();
        assert_eq!(root_metadata.permissions().mode() & 0o777, 0o700);
        assert_eq!(root_metadata.uid(), unsafe { libc::geteuid() });
        assert!(target_metadata.file_type().is_file());
        assert_eq!(target_metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!(target_metadata.nlink(), 1);
        assert_eq!(target_metadata.uid(), unsafe { libc::geteuid() });

        write_run_retry_recovery_at(&root, None).unwrap();
        assert_eq!(read_run_retry_recovery_at(&root).unwrap(), None);
        assert!(!run_retry_recovery_target(&root).exists());
    }

    #[test]
    fn run_retry_recovery_compare_and_swap_rejects_stale_process_authority() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let first_state = DesktopHostState::default();
        let second_state = DesktopHostState::default();

        compare_and_swap_run_retry_recovery_at(&first_state, &root, None, Some("A")).unwrap();
        let error = compare_and_swap_run_retry_recovery_at(&second_state, &root, None, Some("B"))
            .unwrap_err();
        assert_eq!(error.code, "run_retry_recovery_conflict");
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some("A")
        );

        compare_and_swap_run_retry_recovery_at(&second_state, &root, Some("A"), Some("B")).unwrap();
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some("B")
        );
        compare_and_swap_run_retry_recovery_at(&first_state, &root, Some("B"), Some("A")).unwrap();
        compare_and_swap_run_retry_recovery_at(&second_state, &root, Some("A"), None).unwrap();
        assert_eq!(read_run_retry_recovery_at(&root).unwrap(), None);
    }

    #[test]
    fn run_retry_recovery_compare_and_swap_rejects_oversize_inputs_without_changes() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let state = DesktopHostState::default();
        let oversized = "x".repeat(RUN_RETRY_RECOVERY_MAX_BYTES + 1);

        compare_and_swap_run_retry_recovery_at(&state, &root, None, Some("A")).unwrap();
        let expected_error =
            compare_and_swap_run_retry_recovery_at(&state, &root, Some(&oversized), Some("B"))
                .unwrap_err();
        assert_eq!(expected_error.code, "run_retry_recovery_too_large");
        let value_error =
            compare_and_swap_run_retry_recovery_at(&state, &root, Some("A"), Some(&oversized))
                .unwrap_err();
        assert_eq!(value_error.code, "run_retry_recovery_too_large");
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some("A")
        );
    }

    #[test]
    fn run_retry_recovery_fails_when_post_sync_verification_changes() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);

        let error = write_some_run_retry_recovery_at_with(&root, "durable", |file| {
            file.set_len(0).map_err(|_| run_retry_recovery_error())?;
            file.sync_all().map_err(|_| run_retry_recovery_error())
        })
        .unwrap_err();

        assert_eq!(error.code, "run_retry_recovery_unavailable");
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some("")
        );
    }

    #[test]
    fn run_retry_recovery_rejects_oversize_write_and_existing_file() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let oversized = "x".repeat(RUN_RETRY_RECOVERY_MAX_BYTES + 1);

        let error = write_run_retry_recovery_at(&root, Some(&oversized)).unwrap_err();
        assert_eq!(error.code, "run_retry_recovery_too_large");

        open_run_retry_recovery_root(&root, true).unwrap().unwrap();
        let target = run_retry_recovery_target(&root);
        fs::write(&target, oversized.as_bytes()).unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap_err().code,
            "run_retry_recovery_unavailable"
        );
        write_run_retry_recovery_at(&root, None).unwrap();
        assert!(!target.exists());

        fs::write(&target, [0xff]).unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap_err().code,
            "run_retry_recovery_unavailable"
        );
        write_run_retry_recovery_at(&root, None).unwrap();
        assert!(!target.exists());
    }

    #[test]
    fn run_retry_recovery_rejects_symlink_and_hardlink_targets() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        open_run_retry_recovery_root(&root, true).unwrap().unwrap();
        let target = run_retry_recovery_target(&root);
        let outside = temp.path().join("outside");
        fs::write(&outside, "outside").unwrap();
        fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();

        symlink(&outside, &target).unwrap();
        assert!(read_run_retry_recovery_at(&root).is_err());
        let state = DesktopHostState::default();
        assert_eq!(
            compare_and_swap_run_retry_recovery_at(&state, &root, None, Some("replacement"))
                .unwrap_err()
                .code,
            "run_retry_recovery_unavailable"
        );
        assert_eq!(fs::read(&outside).unwrap(), b"outside");
        assert!(write_run_retry_recovery_at(&root, None).is_err());
        assert!(write_run_retry_recovery_at(&root, Some("replacement")).is_err());
        fs::remove_file(&target).unwrap();

        fs::write(&target, "linked").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        let alias = root.join("alias");
        hard_link(&target, &alias).unwrap();
        assert!(read_run_retry_recovery_at(&root).is_err());
        assert!(write_run_retry_recovery_at(&root, None).is_err());
        assert!(write_run_retry_recovery_at(&root, Some("replacement")).is_err());
    }

    #[test]
    fn run_retry_recovery_rejects_symlinked_or_non_private_root() {
        let temp = tempfile::tempdir().unwrap();
        let actual = temp.path().join("actual");
        fs::create_dir(&actual).unwrap();
        fs::set_permissions(&actual, fs::Permissions::from_mode(0o700)).unwrap();
        let linked = temp.path().join("linked");
        symlink(&actual, &linked).unwrap();
        assert!(read_run_retry_recovery_at(&linked).is_err());

        let non_private = temp.path().join("non-private");
        fs::create_dir(&non_private).unwrap();
        fs::set_permissions(&non_private, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(read_run_retry_recovery_at(&non_private).is_err());
        assert!(write_run_retry_recovery_at(&non_private, Some("value")).is_err());
    }

    #[test]
    fn run_retry_recovery_lock_serializes_without_sidecar_lock() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let state = DesktopHostState::default();

        let sidecar_guard = state.sidecar.lock().unwrap();
        let (sidecar_tx, sidecar_rx) = mpsc::channel();
        let sidecar_state = state.clone();
        let sidecar_root = root.clone();
        let sidecar_writer = thread::spawn(move || {
            let result = with_run_retry_recovery_transaction(&sidecar_state, &sidecar_root, || {
                write_run_retry_recovery_at(&sidecar_root, Some("first"))
            });
            sidecar_tx.send(result).unwrap();
        });
        sidecar_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("recovery write waited for the sidecar lifecycle lock")
            .unwrap();
        drop(sidecar_guard);
        sidecar_writer.join().unwrap();

        let recovery_guard = state.run_retry_recovery.lock().unwrap();
        let (recovery_tx, recovery_rx) = mpsc::channel();
        let recovery_state = state.clone();
        let recovery_root = root.clone();
        let recovery_writer = thread::spawn(move || {
            let result =
                with_run_retry_recovery_transaction(&recovery_state, &recovery_root, || {
                    write_run_retry_recovery_at(&recovery_root, Some("second"))
                });
            recovery_tx.send(result).unwrap();
        });
        assert!(recovery_rx
            .recv_timeout(Duration::from_millis(100))
            .is_err());
        drop(recovery_guard);
        recovery_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("recovery write did not resume after its dedicated lock")
            .unwrap();
        recovery_writer.join().unwrap();
        assert_eq!(
            read_run_retry_recovery_at(&root).unwrap().as_deref(),
            Some("second")
        );
    }

    #[test]
    fn run_retry_recovery_lock_serializes_across_processes_and_independent_states() {
        const CHILD_ENV: &str = "OPENEVO_TEST_RUN_RETRY_LOCK_CHILD";
        const ROOT_ENV: &str = "OPENEVO_TEST_RUN_RETRY_LOCK_ROOT";
        const READY_ENV: &str = "OPENEVO_TEST_RUN_RETRY_LOCK_READY";

        if std::env::var_os(CHILD_ENV).is_some() {
            let root = PathBuf::from(std::env::var_os(ROOT_ENV).unwrap());
            let ready = PathBuf::from(std::env::var_os(READY_ENV).unwrap());
            let state = DesktopHostState::default();
            with_run_retry_recovery_transaction(&state, &root, || {
                fs::write(&ready, b"ready").map_err(|_| run_retry_recovery_error())?;
                thread::sleep(Duration::from_secs(5));
                Ok(())
            })
            .unwrap();
            return;
        }

        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        let ready = temp.path().join("holder-ready");
        let mut holder = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg(
                "tests::run_retry_recovery_lock_serializes_across_processes_and_independent_states",
            )
            .arg("--nocapture")
            .env(CHILD_ENV, "1")
            .env(ROOT_ENV, &root)
            .env(READY_ENV, &ready)
            .spawn()
            .unwrap();

        let ready_deadline = Instant::now() + Duration::from_secs(3);
        while !ready.exists() {
            assert!(
                holder.try_wait().unwrap().is_none(),
                "lock holder exited before acquiring the lock"
            );
            assert!(
                Instant::now() < ready_deadline,
                "lock holder did not acquire the lock"
            );
            thread::sleep(Duration::from_millis(10));
        }

        let contender_state = DesktopHostState::default();
        let contender_root = root.clone();
        let (entered_tx, entered_rx) = mpsc::channel();
        let contender = thread::spawn(move || {
            with_run_retry_recovery_transaction(&contender_state, &contender_root, || {
                entered_tx.send(()).unwrap();
                Ok(())
            })
        });
        assert!(entered_rx.recv_timeout(Duration::from_millis(150)).is_err());

        holder.kill().unwrap();
        assert!(!holder.wait().unwrap().success());
        entered_rx
            .recv_timeout(Duration::from_secs(3))
            .expect("contender did not enter after the holder process exited");
        contender.join().unwrap().unwrap();
        let lock_metadata = fs::symlink_metadata(run_retry_recovery_lock_target(&root)).unwrap();
        assert!(lock_metadata.file_type().is_file());
        assert_eq!(lock_metadata.nlink(), 1);
        assert_eq!(lock_metadata.permissions().mode() & 0o777, 0o600);
    }

    #[test]
    fn run_retry_recovery_lock_rejects_unsafe_lockfiles_without_removing_them() {
        let temp = tempfile::tempdir().unwrap();
        let root = test_run_retry_recovery_root(&temp);
        open_run_retry_recovery_root(&root, true).unwrap().unwrap();
        let lock = run_retry_recovery_lock_target(&root);
        let outside = temp.path().join("outside-lock");
        fs::write(&outside, b"outside").unwrap();
        fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();

        symlink(&outside, &lock).unwrap();
        let state = DesktopHostState::default();
        assert_eq!(
            with_run_retry_recovery_transaction(&state, &root, || Ok(()))
                .unwrap_err()
                .code,
            "run_retry_recovery_unavailable"
        );
        assert!(fs::symlink_metadata(&lock)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read(&outside).unwrap(), b"outside");
        fs::remove_file(&lock).unwrap();

        fs::write(&lock, b"").unwrap();
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(with_run_retry_recovery_transaction(&state, &root, || Ok(())).is_err());
        assert_eq!(
            fs::symlink_metadata(&lock).unwrap().permissions().mode() & 0o777,
            0o644
        );
        fs::remove_file(&lock).unwrap();

        fs::write(&lock, b"").unwrap();
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o600)).unwrap();
        let alias = root.join("lock-alias");
        hard_link(&lock, &alias).unwrap();
        assert!(with_run_retry_recovery_transaction(&state, &root, || Ok(())).is_err());
        assert_eq!(fs::symlink_metadata(&lock).unwrap().nlink(), 2);
        assert_eq!(fs::symlink_metadata(&alias).unwrap().nlink(), 2);
    }

    #[test]
    fn allocated_listener_keeps_the_selected_port_reserved() {
        let allocated = allocate_sidecar_listener().unwrap();

        let competing_bind = TcpListener::bind(("127.0.0.1", allocated.port));

        assert!(competing_bind.is_err());
    }

    #[test]
    fn sidecar_status_has_no_command_path_credential_or_log_surface() {
        let status = serde_json::to_value(stopped_host_status()).unwrap();

        assert_eq!(status, serde_json::json!({"state": "stopped"}));

        for forbidden in [
            "command",
            "path",
            "args",
            "backend_url",
            "instance_credential",
            "stdout",
            "stderr",
            "logs",
        ] {
            assert!(status.get(forbidden).is_none());
        }
    }

    #[test]
    fn bootstrap_and_host_status_use_exact_disjoint_renderer_dtos() {
        let context = DesktopBootstrapContextV1 {
            schema_version: "1",
            endpoint: "http://127.0.0.1:49152".to_string(),
            session_token: "7c".repeat(SESSION_TOKEN_BYTES),
            negotiated_contract: test_bootstrap_state().negotiated_contract,
        };
        let context = serde_json::to_value(context).unwrap();
        let status = serde_json::to_value(HostStatus {
            state: "running".to_string(),
        })
        .unwrap();

        assert_eq!(
            context
                .as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            [
                "endpoint",
                "negotiated_contract",
                "schema_version",
                "session_token",
            ]
        );
        assert_eq!(
            context["negotiated_contract"]
                .as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            ["feature_flags", "major", "openapi_sha256", "provider_kind"]
        );
        assert_eq!(status, serde_json::json!({"state": "running"}));
        for forbidden in ["endpoint", "session_token", "pid", "port", "url", "command"] {
            assert!(status.get(forbidden).is_none());
        }
    }

    #[test]
    fn native_child_frame_has_exact_private_credential_keys() {
        let credential = test_credential();
        let instance_id = credential.instance_id_hex();
        let readiness_key = encode_hex(&credential.readiness_key);
        let session_token = encode_hex(&credential.session_token);
        let handoff_token = encode_hex(&credential.handoff_token);
        let frame = serde_json::to_value(NativeInstanceFrame {
            protocol: NATIVE_SIDECAR_PROTOCOL,
            instance_id: &instance_id,
            readiness_key: &readiness_key,
            session_token: &session_token,
            handoff_token: &handoff_token,
        })
        .unwrap();

        assert_eq!(
            frame
                .as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            [
                "handoff_token",
                "instance_id",
                "protocol",
                "readiness_key",
                "session_token"
            ]
        );
        assert_eq!(session_token.len(), SESSION_TOKEN_BYTES * 2);
        assert_eq!(handoff_token.len(), HANDOFF_TOKEN_BYTES * 2);
    }

    #[test]
    fn renderer_readiness_requires_a_running_sidecar_with_the_frozen_contract() {
        let empty = DesktopHostState::default();
        assert_eq!(
            renderer_ready_inner(&empty, DESKTOP_LOCAL_API_OPENAPI_SHA256)
                .unwrap_err()
                .code,
            "sidecar_state_unavailable"
        );

        let state = DesktopHostState::default();
        let (managed, _, _) = managed_test_sidecar();
        let session_listener = managed._listener.try_clone().unwrap();
        let session_server = thread::spawn(move || serve_session_probe(&session_listener, false));
        *state.sidecar.lock().unwrap() = Some(managed);
        renderer_ready_inner(&state, DESKTOP_LOCAL_API_OPENAPI_SHA256).unwrap();
        session_server.join().unwrap();

        assert_eq!(
            renderer_ready_inner(&state, &"0".repeat(64))
                .unwrap_err()
                .code,
            "sidecar_contract_incompatible"
        );
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn renderer_readiness_rejects_an_exited_unreaped_sidecar() {
        let state = DesktopHostState::default();
        let (managed, _, _) = managed_test_sidecar();
        let process_group = managed.process_group;
        *state.sidecar.lock().unwrap() = Some(managed);

        assert_eq!(unsafe { libc::kill(process_group, libc::SIGKILL) }, 0);
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            let exited = {
                let sidecar = state.sidecar.lock().unwrap();
                OsProcessControl
                    .leader_exited(sidecar.as_ref().unwrap().child.as_ref().unwrap())
                    .unwrap()
            };
            if exited {
                break;
            }
            assert!(Instant::now() < deadline, "sidecar leader did not exit");
            thread::sleep(Duration::from_millis(10));
        }

        let error = renderer_ready_inner(&state, DESKTOP_LOCAL_API_OPENAPI_SHA256).unwrap_err();

        assert_eq!(error.code, "sidecar_state_unavailable");
        assert!(!error.message.contains(&process_group.to_string()));
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn renderer_readiness_requires_the_private_sidecar_session() {
        let state = DesktopHostState::default();
        let (mut managed, _, _) = managed_test_sidecar();
        let unreachable = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
        let unreachable_port = unreachable.local_addr().unwrap().port();
        managed.status.port = Some(unreachable_port);
        drop(unreachable);
        *state.sidecar.lock().unwrap() = Some(managed);

        let error = renderer_ready_inner(&state, DESKTOP_LOCAL_API_OPENAPI_SHA256).unwrap_err();

        assert_eq!(error.code, "sidecar_session_unavailable");
        assert!(!error.message.contains(&unreachable_port.to_string()));
        assert!(!error.message.contains(&"7c".repeat(SESSION_TOKEN_BYTES)));
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn macos_pid_count_abstraction_retries_full_buffers_and_finds_a_descendant() {
        let leader = 4100;
        let descendant = 4101;
        let calls = AtomicU64::new(0);

        let found = macos_group_has_members_except_leader_with(
            leader,
            || Ok(1),
            |buffer| {
                let call = calls.fetch_add(1, Ordering::AcqRel);
                if call == 0 {
                    buffer.fill(leader);
                    Ok(buffer.len())
                } else {
                    buffer[0] = descendant;
                    Ok(1)
                }
            },
        )
        .unwrap();

        assert!(found);
        assert_eq!(calls.load(Ordering::Acquire), 2);
    }

    #[test]
    fn macos_pid_count_abstraction_observes_growth_after_an_empty_count() {
        let found = macos_group_has_members_except_leader_with(
            4100,
            || Ok(0),
            |buffer| {
                buffer[0] = 4101;
                Ok(1)
            },
        )
        .unwrap();

        assert!(found);
    }

    #[test]
    fn macos_pid_count_abstraction_fails_closed_on_persistent_truncation() {
        let error =
            macos_group_has_members_except_leader_with(4100, || Ok(1), |buffer| Ok(buffer.len()))
                .unwrap_err();

        assert_eq!(error.kind(), std::io::ErrorKind::Other);
    }

    #[test]
    fn macos_proc_list_raw_result_clears_errno_and_rejects_zero_with_errno() {
        unsafe {
            set_current_errno(libc::EPERM);
        }
        let empty = macos_proc_listpgrppids_call(|| {
            assert_eq!(unsafe { current_errno() }, 0);
            0
        })
        .unwrap();
        let error = macos_proc_listpgrppids_call(|| {
            assert_eq!(unsafe { current_errno() }, 0);
            unsafe {
                set_current_errno(libc::EIO);
            }
            0
        })
        .unwrap_err();
        let negative = macos_proc_listpgrppids_call(|| {
            assert_eq!(unsafe { current_errno() }, 0);
            -1
        })
        .unwrap_err();

        assert_eq!(empty, 0);
        assert_eq!(error.raw_os_error(), Some(libc::EIO));
        assert_eq!(negative.raw_os_error(), Some(libc::EIO));
    }

    #[test]
    fn macos_proc_list_count_fill_and_growth_errors_all_fail_closed() {
        let count_error = macos_group_has_members_except_leader_with(
            4100,
            || {
                macos_proc_listpgrppids_call(|| {
                    unsafe {
                        set_current_errno(libc::EPERM);
                    }
                    0
                })
            },
            |_| unreachable!("fill must not run after a count error"),
        )
        .unwrap_err();
        let fill_error = macos_group_has_members_except_leader_with(
            4100,
            || Ok(1),
            |_| {
                macos_proc_listpgrppids_call(|| {
                    unsafe {
                        set_current_errno(libc::EACCES);
                    }
                    0
                })
            },
        )
        .unwrap_err();
        let fill_calls = AtomicU64::new(0);
        let growth_error = macos_group_has_members_except_leader_with(
            4100,
            || Ok(1),
            |buffer| {
                let call = fill_calls.fetch_add(1, Ordering::AcqRel);
                macos_proc_listpgrppids_call(|| {
                    if call == 0 {
                        libc::c_int::try_from(buffer.len()).unwrap()
                    } else {
                        unsafe {
                            set_current_errno(libc::EFAULT);
                        }
                        0
                    }
                })
            },
        )
        .unwrap_err();

        assert_eq!(count_error.raw_os_error(), Some(libc::EPERM));
        assert_eq!(fill_error.raw_os_error(), Some(libc::EACCES));
        assert_eq!(growth_error.raw_os_error(), Some(libc::EFAULT));
        assert_eq!(fill_calls.load(Ordering::Acquire), 2);
    }

    #[test]
    fn linux_proc_inspection_fails_closed_on_permission_and_parse_errors() {
        let permission = inspect_linux_proc_stat(
            Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied)),
            4100,
        )
        .unwrap_err();
        let malformed =
            inspect_linux_proc_stat(Ok("4101 malformed".to_string()), 4100).unwrap_err();

        assert_eq!(permission.kind(), std::io::ErrorKind::PermissionDenied);
        assert_eq!(malformed.kind(), std::io::ErrorKind::InvalidData);
        assert_eq!(
            inspect_linux_proc_stat(
                Err(std::io::Error::from(std::io::ErrorKind::NotFound)),
                4100,
            )
            .unwrap(),
            None
        );
        assert_eq!(
            inspect_linux_proc_stat(Err(std::io::Error::from_raw_os_error(libc::ESRCH)), 4100,)
                .unwrap(),
            None
        );
    }

    #[test]
    fn linux_proc_birth_identity_uses_the_kernel_start_ticks_field() {
        let stat = concat!(
            "4101 (openevo sidecar) S 1 4101 4101 0 0 0 0 0 0 ",
            "0 0 0 0 0 0 0 0 0 987654 0 0"
        );

        assert_eq!(linux_proc_stat_start_ticks(stat).unwrap(), 987654);
        assert!(linux_proc_stat_start_ticks("4101 malformed").is_err());
        assert!(linux_proc_stat_start_ticks(
            "4101 (sidecar) S 1 4101 4101 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        .is_err());
    }

    #[test]
    fn release_policy_executes_only_a_verified_private_copy() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_sidecar_env();
        let fixture = SidecarFixture::executable(b"packaged-sidecar-v1");
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_PROGRAM", "/tmp/untrusted");
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_ARGS_JSON", r#"["--steal"]"#);
        std::env::set_var("OPENEVO_DESKTOP_BACKEND_BASE_URL", "https://secret.invalid");

        let spec = sidecar_launch_spec(LaunchPolicy::Release, Some(fixture.path()), 49152).unwrap();
        clear_sidecar_env();

        assert_eq!(
            spec.program,
            release_execution_path(spec.private_launch_dir.as_ref().unwrap())
        );
        let executable = spec.verified_executable.as_ref().unwrap();
        executable.validate().unwrap();
        assert_eq!(read_verified_file(executable), b"packaged-sidecar-v1");
        #[cfg(target_os = "linux")]
        assert_eq!(executable.identity.links, 0);
        #[cfg(target_os = "macos")]
        assert_eq!(executable.identity.links, 1);
        assert_eq!(executable.identity.mode & 0o777, 0o500);
        let private_root = spec.private_launch_dir.as_ref().unwrap().path();
        assert_eq!(
            fs::symlink_metadata(private_root)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        #[cfg(target_os = "linux")]
        assert_eq!(fs::read_dir(private_root).unwrap().count(), 0);
        #[cfg(target_os = "macos")]
        assert_eq!(fs::read_dir(private_root).unwrap().count(), 1);
        assert_eq!(
            spec.args,
            vec!["--listener-fd", "3", "--native-instance-stdin",]
        );
        assert!(spec.current_dir.is_none());
        assert_eq!(spec.remove_env, RELEASE_FORBIDDEN_SIDECAR_ENV);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn unlinked_private_fd_survives_source_path_replacement_and_is_cleaned_on_drop() {
        let fixture = SidecarFixture::executable(b"trusted-bytes");
        let spec = sidecar_launch_spec(LaunchPolicy::Release, Some(fixture.path()), 49153).unwrap();
        let private_root = spec
            .private_launch_dir
            .as_ref()
            .unwrap()
            .path()
            .to_path_buf();

        fs::remove_file(fixture.path()).unwrap();
        fs::write(fixture.path(), b"replacement").unwrap();
        fs::set_permissions(fixture.path(), fs::Permissions::from_mode(0o700)).unwrap();

        let executable = spec.verified_executable.as_ref().unwrap();
        executable.validate().unwrap();
        assert_eq!(read_verified_file(executable), b"trusted-bytes");
        assert_eq!(fs::read_dir(&private_root).unwrap().count(), 0);
        drop(spec);
        assert!(!private_root.exists());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn private_cleanup_never_recursively_removes_a_replacement_path() {
        let fixture = SidecarFixture::executable(b"trusted-bytes");
        let spec = sidecar_launch_spec(LaunchPolicy::Release, Some(fixture.path()), 49153).unwrap();
        let private_root = spec
            .private_launch_dir
            .as_ref()
            .unwrap()
            .path()
            .to_path_buf();
        let displaced = private_root.with_extension("displaced");
        fs::rename(&private_root, &displaced).unwrap();
        fs::create_dir(&private_root).unwrap();
        let replacement = private_root.join("same-uid-owned-data");
        fs::write(&replacement, b"must-not-be-removed").unwrap();

        drop(spec);

        assert_eq!(fs::read(&replacement).unwrap(), b"must-not-be-removed");
        fs::remove_dir_all(&private_root).unwrap();
        fs::remove_dir(&displaced).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn fd_execution_uses_verified_bytes_after_source_replacement() {
        let fixture = SidecarFixture::from_existing(Path::new("/bin/sh"));
        let (verified_executable, private_launch_dir) =
            prepare_packaged_sidecar(fixture.path()).unwrap();
        fs::remove_file(fixture.path()).unwrap();
        fs::copy("/bin/false", fixture.path()).unwrap();
        fs::set_permissions(fixture.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let private_root = private_launch_dir.path().to_path_buf();
        let allocated = allocate_sidecar_listener().unwrap();
        let launch = SidecarLaunchSpec {
            program: fd_execution_path(),
            args: vec![
                "-c".to_string(),
                "test \"$OPENEVO_NATIVE_EXECUTABLE_FD\" = 4 || exit 24; exit 23".to_string(),
            ],
            current_dir: None,
            remove_env: &[],
            private_launch_dir: Some(private_launch_dir),
            verified_executable: Some(verified_executable),
        };

        let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let mut child = prepared.spawn().unwrap();
        drop(child.stdin.take());
        let status = child.wait().unwrap();

        assert_eq!(status.code(), Some(23));
        launch
            .verified_executable
            .as_ref()
            .unwrap()
            .validate()
            .unwrap();
        assert_eq!(fs::read_dir(&private_root).unwrap().count(), 0);
        drop(launch);
        assert!(!private_root.exists());
    }

    #[test]
    fn verified_packaged_launch_rejects_poisoned_pyinstaller_environment() {
        let _guard = ENV_LOCK.lock().unwrap();
        let _environment = ScopedEnvironment::set(&[
            (
                "_PYI_APPLICATION_HOME_DIR",
                "/tmp/attacker-controlled-extraction",
            ),
            ("_PYI_ARCHIVE_FILE", "/tmp/attacker-controlled-archive"),
            ("_PYI_PARENT_PROCESS_LEVEL", "99"),
            ("_PYI_SPLASH_IPC", "31337"),
            ("_PYI_UNKNOWN_PRIVATE_STATE", "poisoned"),
            (PYINSTALLER_RESET_ENVIRONMENT, "0"),
            (NATIVE_EXECUTABLE_FD_ENV, "99"),
            (NATIVE_EXECUTABLE_PATH_ENV, "/tmp/attacker-sidecar"),
        ]);
        let fixture = SidecarFixture::from_existing(Path::new("/bin/sh"));
        let (verified_executable, private_launch_dir) =
            prepare_packaged_sidecar(fixture.path()).unwrap();
        let allocated = allocate_sidecar_listener().unwrap();
        let launch = SidecarLaunchSpec {
            program: fd_execution_path(),
            args: vec![
                "-c".to_string(),
                concat!(
                    "test -z \"${_PYI_APPLICATION_HOME_DIR+x}\" || exit 31; ",
                    "test -z \"${_PYI_ARCHIVE_FILE+x}\" || exit 32; ",
                    "test -z \"${_PYI_PARENT_PROCESS_LEVEL+x}\" || exit 33; ",
                    "test -z \"${_PYI_SPLASH_IPC+x}\" || exit 34; ",
                    "test -z \"${_PYI_UNKNOWN_PRIVATE_STATE+x}\" || exit 35; ",
                    "test \"$PYINSTALLER_RESET_ENVIRONMENT\" = 1 || exit 36"
                )
                .to_string(),
            ],
            current_dir: None,
            remove_env: &RELEASE_FORBIDDEN_SIDECAR_ENV,
            private_launch_dir: Some(private_launch_dir),
            verified_executable: Some(verified_executable),
        };

        let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let mut child = prepared.spawn().unwrap();
        drop(child.stdin.take());
        let status = child.wait().unwrap();

        assert_eq!(status.code(), Some(0));
    }

    #[test]
    fn packaged_launch_owns_the_native_executable_environment() {
        let _guard = ENV_LOCK.lock().unwrap();
        let _environment = ScopedEnvironment::set(&[
            (NATIVE_LISTENER_FD_ENV, "99"),
            (NATIVE_EXECUTABLE_FD_ENV, "99"),
            (NATIVE_EXECUTABLE_PATH_ENV, "/tmp/attacker-sidecar"),
        ]);
        let fixture = SidecarFixture::from_existing(Path::new("/bin/true"));
        let allocated = allocate_sidecar_listener().unwrap();
        let launch = release_sidecar_launch_spec(Some(fixture.path()), allocated.port).unwrap();
        let prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let native_environment = prepared
            .command
            .get_envs()
            .filter(|(name, _)| {
                *name == NATIVE_LISTENER_FD_ENV
                    || *name == NATIVE_EXECUTABLE_FD_ENV
                    || *name == NATIVE_EXECUTABLE_PATH_ENV
            })
            .collect::<Vec<_>>();

        assert!(native_environment.iter().any(|(name, value)| {
            *name == NATIVE_LISTENER_FD_ENV
                && value.is_some_and(|value| value == std::ffi::OsStr::new("3"))
        }));
        assert!(native_environment.iter().any(|(name, value)| {
            *name == NATIVE_EXECUTABLE_FD_ENV
                && value.is_some_and(|value| value == std::ffi::OsStr::new("4"))
        }));
        #[cfg(target_os = "linux")]
        assert!(native_environment
            .iter()
            .any(|(name, value)| *name == NATIVE_EXECUTABLE_PATH_ENV && value.is_none()));
        #[cfg(target_os = "macos")]
        assert!(native_environment.iter().any(|(name, value)| {
            *name == NATIVE_EXECUTABLE_PATH_ENV
                && value.is_some_and(|value| value == launch.program.as_os_str())
        }));
    }

    #[test]
    fn secure_copy_rejects_source_mutation_before_copy() {
        let fixture = SidecarFixture::executable(b"trusted-bytes");
        let source = fixture.path().to_path_buf();

        let error = prepare_packaged_sidecar_with_hooks(
            &source,
            || overwrite_same_length(&source, b"changed-bytes"),
            || {},
            || {},
        )
        .unwrap_err();

        assert_eq!(error.code, "bundled_sidecar_identity_changed");
    }

    #[test]
    fn secure_copy_rejects_source_mutation_between_copy_and_reread() {
        let fixture = SidecarFixture::executable(b"trusted-bytes");
        let source = fixture.path().to_path_buf();

        let error = prepare_packaged_sidecar_with_hooks(
            &source,
            || {},
            || overwrite_same_length(&source, b"changed-bytes"),
            || {},
        )
        .unwrap_err();

        assert_eq!(error.code, "bundled_sidecar_identity_changed");
    }

    #[test]
    fn secure_copy_rejects_source_path_replacement_after_reread() {
        let fixture = SidecarFixture::executable(b"trusted-bytes");
        let source = fixture.path().to_path_buf();
        let displaced = fixture.root.join("displaced");

        let error = prepare_packaged_sidecar_with_hooks(
            &source,
            || {},
            || {},
            || {
                fs::rename(&source, &displaced).unwrap();
                fs::write(&source, b"trusted-bytes").unwrap();
                fs::set_permissions(&source, fs::Permissions::from_mode(0o700)).unwrap();
            },
        )
        .unwrap_err();

        assert_eq!(error.code, "bundled_sidecar_identity_changed");
    }

    #[test]
    fn packaged_source_owner_policy_is_explicit_for_loaded_and_macos_anchors() {
        assert!(validate_process_user_identity(501, 501).is_ok());
        assert_eq!(
            validate_process_user_identity(501, 0).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );

        let loaded_user = PackagedSourceOwnerPolicy::MatchLoadedExecutable(501);
        let loaded_root = PackagedSourceOwnerPolicy::MatchLoadedExecutable(0);
        assert!(validate_source_owner(501, loaded_user).is_ok());
        assert!(validate_source_owner(0, loaded_root).is_ok());

        assert_eq!(
            validate_source_owner(0, loaded_user).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );

        let macos_install = PackagedSourceOwnerPolicy::RootOrEffectiveUser(501);
        assert!(validate_source_owner(501, macos_install).is_ok());
        assert!(validate_source_owner(0, macos_install).is_ok());
        assert_eq!(
            validate_source_owner(502, macos_install).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );
    }

    #[test]
    fn trusted_bundle_components_apply_platform_write_policy() {
        let mut identity = mock_directory_identity(0);
        assert!(validate_trusted_directory_for_user(
            &identity,
            501,
            TrustedDirectoryPolicy::Strict
        )
        .is_ok());
        identity.owner = 501;
        assert!(validate_trusted_directory_for_user(
            &identity,
            501,
            TrustedDirectoryPolicy::Strict
        )
        .is_ok());

        identity.mode = DIRECTORY_FILE_TYPE | 0o770;
        assert_eq!(
            validate_trusted_directory_for_user(&identity, 501, TrustedDirectoryPolicy::Strict,)
                .unwrap_err()
                .code,
            "bundled_sidecar_path_untrusted"
        );

        identity.owner = 0;
        identity.mode = DIRECTORY_FILE_TYPE | 0o775;
        assert_eq!(
            validate_trusted_directory_for_user(&identity, 501, TrustedDirectoryPolicy::Strict,)
                .unwrap_err()
                .code,
            "bundled_sidecar_path_untrusted"
        );
        assert!(validate_trusted_directory_for_user(
            &identity,
            501,
            TrustedDirectoryPolicy::MacOsBundle,
        )
        .is_ok());
        identity.owner = 501;
        assert_eq!(
            validate_trusted_directory_for_user(
                &identity,
                501,
                TrustedDirectoryPolicy::MacOsBundle,
            )
            .unwrap_err()
            .code,
            "bundled_sidecar_path_untrusted"
        );

        identity.owner = 0;
        identity.mode = DIRECTORY_FILE_TYPE | STICKY_MODE_BIT | 0o777;
        assert!(validate_trusted_directory_for_user(
            &identity,
            501,
            TrustedDirectoryPolicy::Strict,
        )
        .is_ok());
        identity.mode = DIRECTORY_FILE_TYPE | 0o777;
        assert_eq!(
            validate_trusted_directory_for_user(
                &identity,
                501,
                TrustedDirectoryPolicy::MacOsBundle,
            )
            .unwrap_err()
            .code,
            "bundled_sidecar_path_untrusted"
        );

        identity.owner = 502;
        identity.mode = DIRECTORY_FILE_TYPE | 0o755;
        assert_eq!(
            validate_trusted_directory_for_user(
                &identity,
                501,
                TrustedDirectoryPolicy::MacOsBundle,
            )
            .unwrap_err()
            .code,
            "bundled_sidecar_path_untrusted"
        );
    }

    #[test]
    fn extended_acl_policy_rejects_every_mutating_allow_ace() {
        assert!(validate_extended_acl_entries(&[
            ExtendedAclEntry {
                tag: ExtendedAclTag::Allow,
                permissions: ACL_READ_ONLY_PERMISSIONS,
            },
            ExtendedAclEntry {
                tag: ExtendedAclTag::Deny,
                permissions: ACL_MUTATING_PERMISSIONS,
            },
        ])
        .is_ok());

        for permission in [
            ACL_PERMISSION_WRITE_DATA,
            ACL_PERMISSION_DELETE,
            ACL_PERMISSION_APPEND_DATA,
            ACL_PERMISSION_DELETE_CHILD,
            ACL_PERMISSION_WRITE_ATTRIBUTES,
            ACL_PERMISSION_WRITE_EXTATTRIBUTES,
            ACL_PERMISSION_WRITE_SECURITY,
            ACL_PERMISSION_CHANGE_OWNER,
        ] {
            let error = validate_extended_acl_entries(&[ExtendedAclEntry {
                tag: ExtendedAclTag::Allow,
                permissions: permission,
            }])
            .unwrap_err();
            assert_eq!(error.code, "bundled_sidecar_path_untrusted");
        }
    }

    #[test]
    fn extended_acl_policy_fails_closed_on_unknown_tags_and_permissions() {
        let unknown_tag = validate_extended_acl_entries(&[ExtendedAclEntry {
            tag: ExtendedAclTag::Unknown,
            permissions: 0,
        }])
        .unwrap_err();
        let unknown_permission = validate_extended_acl_entries(&[ExtendedAclEntry {
            tag: ExtendedAclTag::Allow,
            permissions: 1 << 63,
        }])
        .unwrap_err();

        assert_eq!(unknown_tag.code, "bundled_sidecar_path_untrusted");
        assert_eq!(unknown_permission.code, "bundled_sidecar_path_untrusted");
    }

    #[test]
    fn unlink_identity_allows_only_the_kernel_link_and_ctime_transition() {
        let mut expected = mock_directory_identity(501);
        expected.links = 1;
        expected.changed_seconds = 100;
        expected.changed_nanoseconds = 200;
        assert!(same_identity_after_optional_unlink(&expected, &expected));

        let mut unlinked = expected.clone();
        unlinked.links = 0;
        unlinked.changed_seconds = 101;
        unlinked.changed_nanoseconds = 300;
        assert!(same_identity_after_optional_unlink(&unlinked, &expected));

        let mut modified = unlinked.clone();
        modified.size += 1;
        assert!(!same_identity_after_optional_unlink(&modified, &expected));
        let mut extra_link = expected.clone();
        extra_link.links = 2;
        assert!(!same_identity_after_optional_unlink(&extra_link, &expected));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_release_selects_standard_applications_directory_policy() {
        assert_eq!(
            trusted_directory_policy(),
            TrustedDirectoryPolicy::MacOsBundle
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_anchored_acl_reader_accepts_a_fresh_acl_free_file() {
        let fixture = SidecarFixture::executable(b"acl-free-sidecar");
        let file = File::open(fixture.path()).unwrap();

        validate_anchored_extended_acl(&file).unwrap();
    }

    #[test]
    fn release_policy_rejects_unsafe_packaged_paths() {
        let cases = [
            (SidecarFixture::symlink(), "bundled_sidecar_symlink"),
            (SidecarFixture::directory(), "bundled_sidecar_not_regular"),
            (
                SidecarFixture::non_executable(),
                "bundled_sidecar_not_executable",
            ),
            (SidecarFixture::hard_link(), "bundled_sidecar_insecure"),
            (SidecarFixture::writable(), "bundled_sidecar_insecure"),
            (SidecarFixture::group_writable(), "bundled_sidecar_insecure"),
            (
                SidecarFixture::through_directory_symlink(),
                "bundled_sidecar_path_untrusted",
            ),
        ];

        for (fixture, expected_code) in cases {
            let error = sidecar_launch_spec(LaunchPolicy::Release, Some(fixture.path()), 49154)
                .unwrap_err();
            assert_eq!(error.code, expected_code);
            assert!(!error
                .message
                .contains(fixture.path().to_string_lossy().as_ref()));
        }
    }

    #[test]
    fn release_policy_rejects_missing_packaged_sidecar_without_path_disclosure() {
        let fixture = SidecarFixture::missing();
        let path = fixture.path();

        let error = sidecar_launch_spec(LaunchPolicy::Release, Some(path), 49155).unwrap_err();

        assert_eq!(error.code, "bundled_sidecar_missing");
        assert!(!error.message.contains(path.to_string_lossy().as_ref()));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_release_executes_the_inherited_fd_through_procfs() {
        assert_eq!(fd_execution_path(), PathBuf::from("/proc/self/fd/4"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_release_uses_private_path_and_keeps_the_verified_fd() {
        assert_eq!(fd_execution_path(), PathBuf::from("/dev/fd/4"));
        let private = PrivateLaunchDirectory::create().unwrap();
        assert_eq!(
            release_execution_path(&private),
            private.path().join(BUNDLED_SIDECAR_BINARY)
        );
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_policy_uses_structured_override_without_shell_parsing() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_sidecar_env();
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_PROGRAM", "/tmp/dev-sidecar");
        std::env::set_var(
            "OPENEVO_DESKTOP_SIDECAR_ARGS_JSON",
            r#"["--config","dev profile.json"]"#,
        );
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_WORKDIR", "/tmp/dev-workdir");

        let spec = sidecar_launch_spec(LaunchPolicy::Debug, None, 49156).unwrap();
        clear_sidecar_env();

        assert_eq!(spec.program, PathBuf::from("/tmp/dev-sidecar"));
        assert_eq!(
            spec.args,
            vec![
                "--config",
                "dev profile.json",
                "--listener-fd",
                "3",
                "--native-instance-stdin",
            ]
        );
        assert_eq!(spec.current_dir, Some(PathBuf::from("/tmp/dev-workdir")));
        assert!(!spec.args.iter().any(|argument| argument == "-c"));
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_policy_has_a_structured_source_launcher_fallback() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_sidecar_env();
        let missing = unique_test_dir().join("missing-sidecar");

        let spec = sidecar_launch_spec(LaunchPolicy::Debug, Some(&missing), 49157).unwrap();

        assert_eq!(spec.program, PathBuf::from("python3"));
        assert_eq!(&spec.args[..2], ["-m", "desktop.server.launcher"]);
        assert!(!spec.args.iter().any(|argument| argument == "-c"));
    }

    #[cfg(not(debug_assertions))]
    #[test]
    fn production_cfg_excludes_debug_override_and_launcher_fallback() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_sidecar_env();
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_PROGRAM", "/tmp/dev-sidecar");
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_ARGS_JSON", r#"["--debug"]"#);
        let missing = SidecarFixture::missing();

        assert_eq!(active_launch_policy(), LaunchPolicy::Release);
        let error =
            sidecar_launch_spec(active_launch_policy(), Some(missing.path()), 49158).unwrap_err();
        clear_sidecar_env();

        assert_eq!(error.code, "bundled_sidecar_missing");
    }

    #[test]
    fn arbitrary_health_200_cannot_impersonate_the_child_instance() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2048];
            let _ = stream.read(&mut request);
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 44\r\n\r\n{\"service\":\"openevo-sidecar\",\"status\":\"ok\"}",
                )
                .unwrap();
        });
        let credential = test_credential();

        let error = check_sidecar_health(port, &credential).unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_health_unavailable");
    }

    #[test]
    fn loopback_http_uses_one_total_deadline_for_trickled_responses() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}" {
                if stream.write_all(&[*byte]).is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(15));
            }
        });
        let started = Instant::now();

        let error = expect_host_error(loopback_http_request(
            port,
            "GET",
            "/health",
            None,
            None,
            Duration::from_millis(60),
            1024,
        ));
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_health_unavailable");
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[test]
    fn loopback_http_accepts_a_204_without_content_length() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            stream
                .write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                .unwrap();
        });

        let response = loopback_http_get(port, "/openevo-native/session", None).unwrap();
        server.join().unwrap();

        assert_eq!(response.status, 204);
        assert!(response.body.is_empty());
    }

    #[test]
    fn native_workspace_request_stays_bound_to_the_pre_restart_listener() {
        let state = DesktopHostState::default();
        let (managed, _private_root, old_port) = managed_test_sidecar();
        let old_listener = managed._listener.try_clone().unwrap();
        let expected_instance = managed.instance_id;
        *state.sidecar.lock().unwrap() = Some(managed);

        let ActiveSidecarConnection {
            mut stream,
            handoff_token,
        } = active_sidecar_connection(&state, expected_instance).unwrap();

        let replacement = TcpListener::bind("127.0.0.1:0").unwrap();
        let replacement_port = replacement.local_addr().unwrap().port();
        let replacement_probe = replacement.try_clone().unwrap();
        replacement_probe.set_nonblocking(true).unwrap();
        {
            let mut sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_mut().unwrap();
            managed.status.port = Some(replacement_port);
            managed.status.url = Some(format!("http://127.0.0.1:{replacement_port}/openevo"));
            managed._listener = replacement;
        }
        let old_server = thread::spawn(move || {
            let (mut accepted, _) = old_listener.accept().unwrap();
            let (headers, _) = read_http_request_with_body(&mut accepted);
            assert!(headers.contains(NATIVE_WORKSPACE_IMPORT_ROUTE));
            write_json_response(
                &mut accepted,
                201,
                serde_json::json!({
                    "schema_version": "1",
                    "lease_token": "3c".repeat(32),
                    "source": {
                        "kind": "native_folder_snapshot",
                        "display_name": "study",
                        "import_ref": {
                            "import_id": format!("workspace-import-{}", "1a".repeat(24)),
                            "content_sha256": "2b".repeat(32),
                            "byte_size": 1024,
                            "entry_count": 1,
                            "extracted_byte_size": 4,
                        }
                    },
                }),
            );
        });

        let operation = test_picker_operation("native-source-action-pinned-0001");
        let source = register_native_workspace_source(
            &mut stream,
            handoff_token.expose(),
            NativeWorkspaceSelection {
                kind: "native_folder_snapshot",
                action_id: "native-source-action-pinned-0001",
                selected_path: Path::new("/private/study"),
                selected_device: 17,
                selected_inode: 29,
                project_id: None,
            },
            &operation,
        )
        .unwrap();
        old_server.join().unwrap();

        assert_eq!(source.source.display_name, "study");
        assert_eq!(old_port, stream.peer_addr().unwrap().port());
        assert!(matches!(
            replacement_probe.accept(),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock
        ));

        let mut managed = state.sidecar.lock().unwrap().take().unwrap();
        if let Some(mut child) = managed.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    #[test]
    fn native_workspace_cancel_releases_the_action_scoped_picker_claim() {
        let state = DesktopHostState::default();
        let first = PickerClaim::acquire(
            &state,
            "native-picker-first-action-0001".to_string(),
            [0x11; INSTANCE_ID_BYTES],
        )
        .unwrap();

        let error = PickerClaim::acquire(
            &state,
            "native-picker-second-action-0001".to_string(),
            [0x11; INSTANCE_ID_BYTES],
        )
        .err()
        .unwrap();
        assert_eq!(error.code, "workspace_selection_in_progress");

        let cancelled = cancel_active_picker(&state, "native-picker-first-action-0001")
            .unwrap()
            .unwrap();
        assert!(cancelled.is_cancelled());
        let second = PickerClaim::acquire(
            &state,
            "native-picker-second-action-0001".to_string(),
            [0x11; INSTANCE_ID_BYTES],
        )
        .unwrap();
        drop(first);
        assert!(state.active_picker.lock().unwrap().is_some());
        drop(second);
        assert!(state.active_picker.lock().unwrap().is_none());
    }

    #[test]
    fn native_workspace_cancel_before_select_consumes_a_bounded_tombstone() {
        let state = DesktopHostState::default();
        let action_id = "native-picker-cancel-before-select-0001";

        assert!(cancel_active_picker(&state, action_id).unwrap().is_none());
        let error = PickerClaim::acquire(&state, action_id.to_string(), [0x11; INSTANCE_ID_BYTES])
            .err()
            .unwrap();
        assert_eq!(error.code, "workspace_selection_cancelled");
        assert!(state.cancelled_picker_actions.lock().unwrap().is_empty());

        let replay =
            PickerClaim::acquire(&state, action_id.to_string(), [0x11; INSTANCE_ID_BYTES]).unwrap();
        drop(replay);
    }

    #[test]
    fn native_workspace_cancel_uses_only_the_hidden_operation_identity() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handoff_token = "8d".repeat(HANDOFF_TOKEN_BYTES);
        let expected_handoff = handoff_token.clone();
        let operation = test_picker_operation("native-picker-cancel-route-0001");
        operation.cancel();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let (headers, body) = read_http_request_with_body(&mut stream);
            assert!(headers.starts_with(&format!(
                "POST {NATIVE_WORKSPACE_CANCEL_ROUTE} HTTP/1.1\r\n"
            )));
            assert!(headers
                .lines()
                .any(|line| line == format!("{NATIVE_HANDOFF_HEADER}: {expected_handoff}")));
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
                serde_json::json!({
                    "schema_version": "1",
                    "action_id": "native-picker-cancel-route-0001",
                    "cancellation_token": "4d".repeat(32),
                })
            );
            stream
                .write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                .unwrap();
        });

        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        cancel_native_workspace_operation(&mut stream, &handoff_token, &operation).unwrap();
        server.join().unwrap();
    }

    #[test]
    fn native_workspace_cancel_bounds_an_unresponsive_import_read() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request_with_body(&mut stream);
            let mut byte = [0_u8; 1];
            while stream.read(&mut byte).unwrap_or(0) != 0 {}
        });
        let operation = Arc::new(test_picker_operation(
            "native-picker-cancel-bounded-read-0001",
        ));
        let cancellation = operation.clone();
        let cancel_thread = thread::spawn(move || {
            thread::sleep(Duration::from_millis(100));
            cancellation.cancel();
        });
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let started = Instant::now();

        let error = expect_host_error(register_native_workspace_source(
            &mut stream,
            &"8d".repeat(HANDOFF_TOKEN_BYTES),
            NativeWorkspaceSelection {
                kind: "native_folder_snapshot",
                action_id: "native-picker-cancel-bounded-read-0001",
                selected_path: Path::new("/private/study"),
                selected_device: 17,
                selected_inode: 29,
                project_id: None,
            },
            &operation,
        ));
        drop(stream);
        cancel_thread.join().unwrap();
        server.join().unwrap();

        assert_eq!(error.code, "workspace_selection_cancelled");
        assert!(started.elapsed() < NATIVE_WORKSPACE_CANCEL_GRACE + Duration::from_secs(1));
    }

    #[test]
    fn native_workspace_pending_handoffs_have_a_fixed_capacity() {
        let state = DesktopHostState::default();
        for index in 0..MAX_PENDING_WORKSPACE_IMPORTS {
            let action_id = format!("native-pending-action-{index:04}");
            remember_pending_workspace_import(
                &state,
                PendingNativeWorkspaceImport {
                    sidecar_instance: [0x11; INSTANCE_ID_BYTES],
                    action_id,
                    project_id: None,
                    source: NativeProjectSourceV1 {
                        kind: "native_folder_snapshot".to_string(),
                        display_name: "study".to_string(),
                        import_ref: NativeWorkspaceImportRefV1 {
                            import_id: format!("workspace-import-{:048x}", index),
                            content_sha256: format!("{:064x}", index),
                            byte_size: 1024,
                            entry_count: 1,
                            extracted_byte_size: 4,
                        },
                    },
                    lease_token: format!("{:064x}", index),
                },
            )
            .unwrap();
        }
        let overflow = PendingNativeWorkspaceImport {
            sidecar_instance: [0x11; INSTANCE_ID_BYTES],
            action_id: "native-pending-action-overflow".to_string(),
            project_id: None,
            source: state
                .pending_workspace_imports
                .lock()
                .unwrap()
                .values()
                .next()
                .unwrap()
                .source
                .clone(),
            lease_token: "ff".repeat(32),
        };

        assert!(remember_pending_workspace_import(&state, overflow).is_err());
        assert_eq!(
            state.pending_workspace_imports.lock().unwrap().len(),
            MAX_PENDING_WORKSPACE_IMPORTS
        );
    }

    #[test]
    fn native_workspace_discard_uses_the_hidden_lease_route() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handoff_token = "8d".repeat(HANDOFF_TOKEN_BYTES);
        let expected_token = handoff_token.clone();
        let pending = PendingNativeWorkspaceImport {
            sidecar_instance: [0x11; INSTANCE_ID_BYTES],
            action_id: "native-pending-action-discard-0001".to_string(),
            project_id: Some("project-existing-1".to_string()),
            source: NativeProjectSourceV1 {
                kind: "native_folder_snapshot".to_string(),
                display_name: "study".to_string(),
                import_ref: NativeWorkspaceImportRefV1 {
                    import_id: format!("workspace-import-{}", "1a".repeat(24)),
                    content_sha256: "2b".repeat(32),
                    byte_size: 1024,
                    entry_count: 1,
                    extracted_byte_size: 4,
                },
            },
            lease_token: "3c".repeat(32),
        };
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let (headers, body) = read_http_request_with_body(&mut stream);
            assert!(headers.starts_with(&format!(
                "POST {NATIVE_WORKSPACE_DISCARD_ROUTE} HTTP/1.1\r\n"
            )));
            assert!(headers
                .lines()
                .any(|line| line == format!("{NATIVE_HANDOFF_HEADER}: {expected_token}")));
            let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(body["lease_token"], "3c".repeat(32));
            assert_eq!(body["action_id"], "native-pending-action-discard-0001");
            assert!(body.get("selected_path").is_none());
            stream
                .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
        });

        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        discard_native_workspace_source(&mut stream, &handoff_token, &pending).unwrap();
        server.join().unwrap();
    }

    #[test]
    fn native_workspace_picker_rejects_a_restarted_sidecar_instance() {
        let state = DesktopHostState::default();
        let (managed, _private_root, _port) = managed_test_sidecar();
        let expected_instance = managed.instance_id;
        *state.sidecar.lock().unwrap() = Some(managed);
        state.sidecar.lock().unwrap().as_mut().unwrap().instance_id = [0x44; INSTANCE_ID_BYTES];

        let error = active_sidecar_connection(&state, expected_instance)
            .err()
            .unwrap();

        assert_eq!(error.code, "sidecar_state_unavailable");
        let mut managed = state.sidecar.lock().unwrap().take().unwrap();
        if let Some(mut child) = managed.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    #[test]
    fn health_challenge_hmac_binds_readiness_to_the_instance_credential() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let instance_id = credential.instance_id;
        let readiness_key = credential.readiness_key;
        let server =
            thread::spawn(move || serve_health(listener, instance_id, readiness_key, None, false));

        check_sidecar_health(port, &credential).unwrap();
        server.join().unwrap();
    }

    #[test]
    fn session_probe_proves_token_acceptance_and_missing_token_rejection() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let token = "7c".repeat(SESSION_TOKEN_BYTES);
        let server = thread::spawn(move || serve_session_probe(&listener, false));

        check_sidecar_session_binding(port, &token).unwrap();
        server.join().unwrap();
    }

    #[test]
    fn session_probe_rejects_a_sidecar_that_accepts_missing_credentials() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let token = "7c".repeat(SESSION_TOKEN_BYTES);
        let server = thread::spawn(move || serve_session_probe(&listener, true));

        let error = check_sidecar_session_binding(port, &token).unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_session_unavailable");
    }

    #[test]
    fn native_workspace_registration_keeps_the_selected_path_out_of_renderer_data() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handoff_token = "8d".repeat(HANDOFF_TOKEN_BYTES);
        let expected_token = handoff_token.clone();
        let selected_path = PathBuf::from("/Users/researcher/private/study");
        let expected_path = selected_path.clone();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let (headers, body) = read_http_request_with_body(&mut stream);
            assert!(headers.starts_with(&format!(
                "POST {NATIVE_WORKSPACE_IMPORT_ROUTE} HTTP/1.1\r\n"
            )));
            assert!(headers
                .lines()
                .any(|line| { line == format!("{NATIVE_HANDOFF_HEADER}: {expected_token}") }));
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
                serde_json::json!({
                    "schema_version": "1",
                    "kind": "native_folder_snapshot",
                    "action_id": "native-source-action-0001",
                    "selected_path": expected_path.to_str().unwrap(),
                    "selected_device": 17,
                    "selected_inode": 29,
                    "cancellation_token": "4d".repeat(32),
                    "project_id": "project-existing-1",
                })
            );
            write_json_response(
                &mut stream,
                201,
                serde_json::json!({
                    "schema_version": "1",
                    "lease_token": "3c".repeat(32),
                    "source": {
                        "kind": "native_folder_snapshot",
                        "display_name": "study",
                        "import_ref": {
                            "import_id": format!("workspace-import-{}", "1a".repeat(24)),
                            "content_sha256": "2b".repeat(32),
                            "byte_size": 1024,
                            "entry_count": 1,
                            "extracted_byte_size": 4,
                        }
                    },
                }),
            );
        });

        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let operation = test_picker_operation("native-source-action-0001");
        let source = register_native_workspace_source(
            &mut stream,
            &handoff_token,
            NativeWorkspaceSelection {
                kind: "native_folder_snapshot",
                action_id: "native-source-action-0001",
                selected_path: &selected_path,
                selected_device: 17,
                selected_inode: 29,
                project_id: Some("project-existing-1"),
            },
            &operation,
        )
        .unwrap();
        server.join().unwrap();

        let renderer_data = serde_json::to_string(&source.source).unwrap();
        assert_eq!(source.source.display_name, "study");
        assert!(!renderer_data.contains(&source.lease_token));
        assert!(!renderer_data.contains("/Users"));
        assert!(!renderer_data.contains("private"));
        assert!(!renderer_data.contains("selected_path"));
    }

    #[test]
    fn native_workspace_registration_rejects_open_or_malformed_responses() {
        for body in [
            serde_json::json!({
                "schema_version": "1",
                "lease_token": "3c".repeat(32),
                "source": {
                    "kind": "native_folder_snapshot",
                    "display_name": "study",
                    "selected_path": "/private/study",
                    "import_ref": {
                        "import_id": format!("workspace-import-{}", "1a".repeat(24)),
                        "content_sha256": "2b".repeat(32),
                        "byte_size": 1024,
                        "entry_count": 1,
                        "extracted_byte_size": 4,
                    }
                },
            }),
            serde_json::json!({
                "schema_version": "1",
                "lease_token": "3c".repeat(32),
                "source": {
                    "kind": "native_folder_snapshot",
                    "display_name": "study",
                    "import_ref": {
                        "import_id": "workspace-import-not-opaque",
                        "content_sha256": "2b".repeat(32),
                        "byte_size": 1024,
                        "entry_count": 1,
                        "extracted_byte_size": 4,
                    }
                },
            }),
            serde_json::json!({
                "schema_version": "1",
                "lease_token": "3c".repeat(32),
                "source": {
                    "kind": "native_folder_snapshot",
                    "display_name": "/Users/researcher/private/study",
                    "import_ref": {
                        "import_id": format!("workspace-import-{}", "1a".repeat(24)),
                        "content_sha256": "2b".repeat(32),
                        "byte_size": 1024,
                        "entry_count": 1,
                        "extracted_byte_size": 4,
                    }
                },
            }),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let _ = read_http_request_with_body(&mut stream);
                write_json_response(&mut stream, 201, body);
            });

            let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
            let operation = test_picker_operation("native-source-action-0002");
            let error = expect_host_error(register_native_workspace_source(
                &mut stream,
                &"7c".repeat(SESSION_TOKEN_BYTES),
                NativeWorkspaceSelection {
                    kind: "native_folder_snapshot",
                    action_id: "native-source-action-0002",
                    selected_path: Path::new("/private/study"),
                    selected_device: 17,
                    selected_inode: 29,
                    project_id: None,
                },
                &operation,
            ));
            server.join().unwrap();

            assert_eq!(error.code, "workspace_import_failed");
        }
    }

    #[test]
    fn health_response_rejects_unknown_fields() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let instance_id = credential.instance_id;
        let readiness_key = credential.readiness_key;
        let server =
            thread::spawn(move || serve_health(listener, instance_id, readiness_key, None, true));

        let error = check_sidecar_health(port, &credential).unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_health_unavailable");
    }

    #[test]
    fn a_proof_for_an_old_challenge_cannot_mark_the_instance_ready() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let instance_id = credential.instance_id;
        let readiness_key = credential.readiness_key;
        let stale_challenge = "11".repeat(32);
        let fresh_challenge = "22".repeat(32);
        let server_stale_challenge = stale_challenge.clone();
        let server = thread::spawn(move || {
            serve_health(
                listener,
                instance_id,
                readiness_key,
                Some(server_stale_challenge),
                false,
            )
        });

        let error =
            check_sidecar_health_with_challenge(port, &credential, &fresh_challenge).unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_health_unavailable");
    }

    #[test]
    fn contract_handshake_returns_only_the_frozen_release_contract() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (requests_tx, requests_rx) = mpsc::channel();
        let server = thread::spawn(move || {
            serve_contract_response(&listener, valid_version_response(), 404, requests_tx)
        });

        let contract = check_sidecar_contract(port).unwrap();
        server.join().unwrap();

        assert_eq!(contract.major, 1);
        assert_eq!(contract.openapi_sha256, DESKTOP_LOCAL_API_OPENAPI_SHA256);
        assert_eq!(contract.provider_kind, "desktop_sidecar");
        assert_eq!(
            contract.feature_flags,
            [
                FeatureFlagV1::RemoteProfiles,
                FeatureFlagV1::ProjectValidation,
            ]
        );
        let requests = requests_rx.into_iter().collect::<Vec<_>>().join("\n");
        assert!(requests.contains("GET /version HTTP/1.1"));
        assert!(requests.contains(&format!("GET {LEGACY_DESKTOP_SHELL_ROUTE} HTTP/1.1")));
        assert!(!requests.contains(&"7c".repeat(SESSION_TOKEN_BYTES)));
    }

    #[test]
    fn contract_handshake_rejects_digest_provider_and_legacy_route_inventory_mismatches() {
        for version in [
            valid_version_response_with("openapi_sha256", serde_json::json!("0".repeat(64))),
            valid_version_response_with("provider_kind", serde_json::json!("contract_simulator")),
            valid_version_response_with("unexpected", serde_json::json!(true)),
            valid_version_response_with(
                "feature_flags",
                serde_json::json!(["remote_profiles", "remote_profiles"]),
            ),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let port = listener.local_addr().unwrap().port();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let _ = read_http_request(&mut stream);
                write_json_response(&mut stream, 200, version);
            });

            let error = check_sidecar_contract(port).unwrap_err();
            server.join().unwrap();

            assert_eq!(error.code, "sidecar_contract_incompatible");
        }

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (requests_tx, _) = mpsc::channel();
        let server = thread::spawn(move || {
            serve_contract_response(&listener, valid_version_response(), 200, requests_tx)
        });

        let error = check_sidecar_contract(port).unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_contract_incompatible");
    }

    #[test]
    fn contract_mismatch_cleanup_kills_the_owned_child_group() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let instance_id = credential.instance_id;
        let readiness_key = credential.readiness_key;
        let server = thread::spawn(move || {
            serve_health_on_listener(&listener, instance_id, readiness_key, None, false);
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            write_json_response(
                &mut stream,
                200,
                valid_version_response_with("openapi_sha256", serde_json::json!("0".repeat(64))),
            );
        });
        let state = DesktopHostState::default();
        let (mut managed, _, _) = managed_test_sidecar();
        let process_group = managed.process_group;
        managed.lifecycle = ManagedLifecycle::Starting;
        managed.status.state = "starting".to_string();
        managed.status.port = Some(port);
        managed.bootstrap = None;
        *state.sidecar.lock().unwrap() = Some(managed);
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Spawning),
            Ordering::Release,
        );

        let error = wait_for_state_owned_sidecar_ready(
            &state,
            &OsProcessControl,
            port,
            &credential,
            Duration::from_secs(1),
            0,
        )
        .unwrap_err();
        let returned = fail_state_owned_startup(&state, &OsProcessControl, error);
        server.join().unwrap();

        assert_eq!(returned.code, "sidecar_contract_incompatible");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!process_group_exists(process_group).unwrap());
    }

    #[test]
    fn readiness_rejects_identity_and_contract_without_session_binding() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let instance_id = credential.instance_id;
        let readiness_key = credential.readiness_key;
        let server = thread::spawn(move || {
            serve_health_on_listener(&listener, instance_id, readiness_key, None, false);
            let (requests_tx, _) = mpsc::channel();
            serve_contract_response(&listener, valid_version_response(), 404, requests_tx);
            let (mut stream, _) = listener.accept().unwrap();
            let _ = read_http_request(&mut stream);
            write_empty_response(&mut stream, 403);
        });

        let error = wait_for_sidecar_ready_with_inspection(
            port,
            &credential,
            Duration::from_secs(1),
            || false,
            || Ok(false),
        )
        .unwrap_err();
        server.join().unwrap();

        assert_eq!(error.code, "sidecar_session_unavailable");
    }

    #[test]
    fn explicit_stop_escalation_kills_the_entire_sidecar_process_group() {
        let fixture = SidecarFixture::directory();
        let descendant_pid_path = fixture.path().join("descendant.pid");
        let script = format!(
            "sleep 30 & echo $! > '{}'; wait",
            descendant_pid_path.display()
        );
        let mut child = spawn_test_process_group(&script);
        let process_group = child.id() as i32;
        wait_for_file(&descendant_pid_path);
        let descendant_pid: i32 = fs::read_to_string(&descendant_pid_path)
            .unwrap()
            .trim()
            .parse()
            .unwrap();

        terminate_process_group(
            &mut child,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(500),
        )
        .unwrap();

        assert!(!process_group_exists(process_group).unwrap());
        assert!(wait_for_pid_exit(descendant_pid, Duration::from_secs(1)));
    }

    #[test]
    fn ready_crash_monitor_removes_the_old_group_before_a_new_bootstrap() {
        let fixture = SidecarFixture::directory();
        let descendant_pid_path = fixture.path().join("monitored-descendant.pid");
        let allocated = allocate_sidecar_listener().unwrap();
        let old_port = allocated.port;
        let launch = SidecarLaunchSpec {
            program: PathBuf::from("/bin/sh"),
            args: vec![
                "-c".to_string(),
                format!(
                    "sleep 30 & echo $! > '{}'; wait",
                    descendant_pid_path.display()
                ),
            ],
            current_dir: None,
            remove_env: &[],
            private_launch_dir: None,
            verified_executable: None,
        };
        let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let parent_liveness = prepared.take_parent_liveness_writer().unwrap();
        let child = prepared.spawn().unwrap();
        let process_group = child.id() as i32;
        wait_for_file(&descendant_pid_path);
        let descendant_pid = fs::read_to_string(&descendant_pid_path)
            .unwrap()
            .trim()
            .parse::<i32>()
            .unwrap();
        let old_token = "11".repeat(SESSION_TOKEN_BYTES);
        let state = DesktopHostState::default();
        install_parent_liveness(&state, parent_liveness).unwrap();
        *state.sidecar.lock().unwrap() = Some(ManagedSidecar {
            status: LifecycleStatus {
                state: "running".to_string(),
                port: Some(old_port),
                pid: Some(child.id()),
                url: Some(format!("http://127.0.0.1:{old_port}/openevo")),
            },
            bootstrap: Some(SidecarBootstrapState {
                session_credential: SessionCredential([0x11; SESSION_TOKEN_BYTES]),
                readiness_credential: ReadinessCredential([0x11; READINESS_KEY_BYTES]),
                handoff_credential: HandoffCredential([0x11; HANDOFF_TOKEN_BYTES]),
                negotiated_contract: test_bootstrap_state().negotiated_contract,
            }),
            lifecycle: ManagedLifecycle::Running,
            startup_epoch: 0,
            instance_id: [0x11; INSTANCE_ID_BYTES],
            monitor_started: false,
            spawn_pending: false,
            child: Some(child),
            process_group,
            session_id: process_group,
            birth_identity: None,
            group_signal_authority: GroupSignalAuthority::Anchored,
            process_cleanup_confirmed: false,
            _private_launch_dir: None,
            verified_executable: None,
            _listener: allocated.listener,
        });
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Published),
            Ordering::Release,
        );
        let old_context = state
            .sidecar
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .bootstrap_context()
            .unwrap();
        ensure_running_sidecar_monitor(&state, &old_context).unwrap();

        assert_eq!(unsafe { libc::kill(process_group, libc::SIGKILL) }, 0);
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if state.sidecar.lock().unwrap().is_none() {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "ready-crash monitor did not release the old sidecar slot"
            );
            thread::sleep(Duration::from_millis(20));
        }

        assert!(!process_group_exists(process_group).unwrap());
        assert!(wait_for_pid_exit(descendant_pid, Duration::from_secs(1)));
        assert!(state.parent_liveness.lock().unwrap().is_none());
        let (mut replacement, replacement_root, replacement_port) = managed_test_sidecar();
        replacement.instance_id = [0x22; INSTANCE_ID_BYTES];
        replacement.bootstrap = Some(SidecarBootstrapState {
            session_credential: SessionCredential([0x22; SESSION_TOKEN_BYTES]),
            readiness_credential: ReadinessCredential([0x22; READINESS_KEY_BYTES]),
            handoff_credential: HandoffCredential([0x22; HANDOFF_TOKEN_BYTES]),
            negotiated_contract: test_bootstrap_state().negotiated_contract,
        });
        let probe_listener = replacement._listener.try_clone().unwrap();
        let probe_server = thread::spawn(move || {
            serve_health_on_listener(
                &probe_listener,
                [0x22; INSTANCE_ID_BYTES],
                [0x22; READINESS_KEY_BYTES],
                None,
                false,
            );
            let (requests, _) = mpsc::channel();
            serve_contract_response(&probe_listener, valid_version_response(), 404, requests);
            serve_session_probe(&probe_listener, false);
        });
        *state.sidecar.lock().unwrap() = Some(replacement);
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Published),
            Ordering::Release,
        );

        let replacement_context = start_sidecar_inner(&state, LaunchPolicy::Release, None).unwrap();
        probe_server.join().unwrap();

        assert_eq!(
            replacement_context.endpoint,
            format!("http://127.0.0.1:{replacement_port}")
        );
        assert_ne!(replacement_port, old_port);
        assert_ne!(replacement_context.session_token, old_token);
        stop_sidecar_inner(&state).unwrap();
        assert!(!replacement_root.exists());
    }

    #[test]
    fn live_but_unhealthy_sidecar_is_cleaned_before_restart_attempt() {
        let state = DesktopHostState::default();
        let (managed, private_root, old_port) = managed_test_sidecar();
        let old_process_group = managed.process_group;
        *state.sidecar.lock().unwrap() = Some(managed);
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Published),
            Ordering::Release,
        );

        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));

        assert_eq!(error.code, "bundled_sidecar_missing");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!private_root.exists());
        assert!(!process_group_exists(old_process_group).unwrap());
        assert_ne!(error.message, format!("http://127.0.0.1:{old_port}"));
    }

    #[test]
    fn stale_monitor_identity_cannot_touch_a_replacement_slot() {
        let state = DesktopHostState::default();
        let (mut replacement, private_root, listener_port) = managed_test_sidecar();
        replacement.instance_id = [0x44; INSTANCE_ID_BYTES];
        replacement.monitor_started = true;
        let process_group = replacement.process_group;
        *state.sidecar.lock().unwrap() = Some(replacement);
        let stale_context = DesktopBootstrapContextV1 {
            schema_version: "1",
            endpoint: format!("http://127.0.0.1:{listener_port}"),
            session_token: "33".repeat(SESSION_TOKEN_BYTES),
            negotiated_contract: test_bootstrap_state().negotiated_contract,
        };

        let error = ensure_running_sidecar_monitor(&state, &stale_context).unwrap_err();
        assert_eq!(error.code, "sidecar_state_unavailable");

        monitor_running_sidecar(state.clone(), [0x33; INSTANCE_ID_BYTES]);

        {
            let sidecar = state.sidecar.lock().unwrap();
            let replacement = sidecar.as_ref().unwrap();
            assert_eq!(replacement.instance_id, [0x44; INSTANCE_ID_BYTES]);
            assert_managed_resources(replacement, &private_root, listener_port);
        }
        assert!(process_group_exists(process_group).unwrap());
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn ready_crash_monitor_and_explicit_stop_share_single_reap_ownership() {
        let state = DesktopHostState::default();
        let (mut managed, private_root, _) = managed_test_sidecar();
        managed.instance_id = [0x55; INSTANCE_ID_BYTES];
        let process_group = managed.process_group;
        let context = managed.bootstrap_context().unwrap();
        *state.sidecar.lock().unwrap() = Some(managed);
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Published),
            Ordering::Release,
        );
        ensure_running_sidecar_monitor(&state, &context).unwrap();
        assert_eq!(unsafe { libc::kill(process_group, libc::SIGKILL) }, 0);
        let stop_state = state.clone();

        let stopped = thread::spawn(move || stop_sidecar_inner(&stop_state))
            .join()
            .unwrap()
            .unwrap();

        assert_eq!(stopped.state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!process_group_exists(process_group).unwrap());
        assert!(!private_root.exists());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_proc_listing_keeps_ownership_after_the_leader_exits() {
        let fixture = SidecarFixture::directory();
        let descendant_pid_path = fixture.path().join("linux-descendant.pid");
        let mut leader = spawn_exiting_leader_with_descendant(&descendant_pid_path);
        let process_group = leader.id() as i32;
        wait_for_file(&descendant_pid_path);
        wait_for_leader_exit(&leader);

        assert!(group_has_members_except_leader(process_group, process_group).unwrap());

        terminate_process_group(
            &mut leader,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(500),
        )
        .unwrap();
        assert!(!process_group_exists(process_group).unwrap());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_proc_listing_keeps_ownership_after_the_leader_exits() {
        let fixture = SidecarFixture::directory();
        let descendant_pid_path = fixture.path().join("macos-descendant.pid");
        let mut leader = spawn_exiting_leader_with_descendant(&descendant_pid_path);
        let process_group = leader.id() as i32;
        wait_for_file(&descendant_pid_path);
        wait_for_leader_exit(&leader);

        assert!(group_has_members_except_leader(process_group, process_group).unwrap());

        terminate_process_group(
            &mut leader,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(500),
        )
        .unwrap();
        assert!(!process_group_exists(process_group).unwrap());
    }

    #[test]
    fn signal_failure_returns_within_the_configured_bound() {
        let mut child = Command::new("sleep")
            .arg("30")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let invalid_group = i32::MAX;
        let started = Instant::now();

        let error = terminate_process_group(
            &mut child,
            invalid_group,
            Duration::from_millis(30),
            Duration::from_millis(30),
        )
        .unwrap_err();

        assert_eq!(error.code, "sidecar_stop_failed_owned");
        assert!(started.elapsed() < Duration::from_millis(500));
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn startup_timeout_returns_child_ownership_to_the_caller() {
        let mut child = spawn_test_process_group("sleep 30");
        let process_group = child.id() as i32;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let credential = test_credential();

        let error = wait_for_sidecar_ready(
            &mut child,
            port,
            &credential,
            Duration::from_millis(30),
            || false,
        )
        .unwrap_err();

        assert_eq!(error.code, "sidecar_startup_timeout");
        assert!(child.try_wait().unwrap().is_none());
        assert!(process_group_exists(process_group).unwrap());
        terminate_process_group(
            &mut child,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(500),
        )
        .unwrap();
    }

    #[test]
    fn cancelled_startup_returns_ownership_without_waiting_for_readiness_timeout() {
        let mut child = spawn_test_process_group("sleep 30");
        let process_group = child.id() as i32;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let credential = test_credential();
        let started = Instant::now();

        let error = wait_for_sidecar_ready(
            &mut child,
            port,
            &credential,
            Duration::from_secs(30),
            || true,
        )
        .unwrap_err();

        assert_eq!(error.code, "sidecar_start_cancelled");
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(child.try_wait().unwrap().is_none());
        terminate_process_group(
            &mut child,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(500),
        )
        .unwrap();
    }

    #[test]
    fn term_kill_and_wait_failures_retain_all_manager_resources() {
        for control in [
            ScriptedProcessControl::term_failure(),
            ScriptedProcessControl::kill_failure(),
            ScriptedProcessControl::wait_failure(),
        ] {
            let (mut managed, private_root, listener_port) = managed_test_sidecar();

            let error = cleanup_managed_sidecar_with_bounds(
                &control,
                &mut managed,
                Duration::ZERO,
                Duration::ZERO,
            )
            .unwrap_err();

            assert_eq!(error.code, "sidecar_stop_failed_owned");
            assert_owned_resources(&managed, &private_root, listener_port);
            assert_eq!(managed.lifecycle, ManagedLifecycle::CleanupPending);
            let signals = control.signals.lock().unwrap().clone();
            if control.fail_wait {
                assert!(signals.is_empty());
            } else {
                assert_eq!(signals, [libc::SIGTERM, libc::SIGKILL]);
            }
            cleanup_managed_sidecar(&mut managed).unwrap();
            drop(managed);
            assert!(!private_root.exists());
        }
    }

    #[test]
    fn handoff_cleanup_failure_retains_child_and_blocks_restart_until_stop_retry() {
        let state = DesktopHostState::default();
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let allocated = allocate_sidecar_listener().unwrap();
        reserve_starting_sidecar(
            &state,
            ManagedSidecar {
                status: LifecycleStatus {
                    state: "starting".to_string(),
                    port: Some(allocated.port),
                    pid: None,
                    url: None,
                },
                bootstrap: None,
                lifecycle: ManagedLifecycle::Starting,
                startup_epoch,
                instance_id: [1; INSTANCE_ID_BYTES],
                monitor_started: false,
                spawn_pending: true,
                child: None,
                process_group: 0,
                session_id: 0,
                birth_identity: None,
                group_signal_authority: GroupSignalAuthority::Anchored,
                process_cleanup_confirmed: false,
                _private_launch_dir: None,
                verified_executable: None,
                _listener: allocated.listener,
            },
        )
        .unwrap();
        let handoff = active_spawn_handoff(&state, startup_epoch).unwrap();
        let child = spawn_test_process_group("sleep 30");
        let process_group = child.id() as i32;
        state.launch_state.store(
            encode_launch_state(startup_epoch, LaunchPhase::Spawning),
            Ordering::Release,
        );
        *handoff.outcome.lock().unwrap() = SpawnHandoffOutcome::Spawned(SpawnedHandoffProcess {
            child,
            process_group,
            session_id: process_group,
            birth_identity: None,
            group_signal_authority: GroupSignalAuthority::Anchored,
        });
        advance_cancellation(&state);
        drop(startup_claim);

        let error = cleanup_spawn_handoff_with_bounds(
            &state,
            &ScriptedProcessControl::term_failure(),
            Duration::ZERO,
            Duration::ZERO,
            Duration::ZERO,
        )
        .unwrap_err();

        assert_eq!(error.code, "sidecar_stop_failed_owned");
        assert!(process_group_exists(process_group).unwrap());
        {
            let outcome = handoff.outcome.lock().unwrap();
            let SpawnHandoffOutcome::Spawned(spawned) = &*outcome else {
                panic!("cleanup failure dropped the spawned handoff");
            };
            assert_eq!(spawned.child.id(), process_group as u32);
            assert_eq!(
                spawned.group_signal_authority,
                GroupSignalAuthority::Anchored
            );
        }
        {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert!(managed.spawn_pending);
            assert!(managed.child.is_none());
        }
        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));
        assert_eq!(error.code, "sidecar_start_in_progress");

        assert_eq!(stop_sidecar_inner(&state).unwrap().state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(state.spawn_handoff.lock().unwrap().is_none());
        assert!(!process_group_exists(process_group).unwrap());
    }

    #[test]
    fn cleanup_retry_never_signals_after_a_possibly_consumed_leader() {
        let (mut managed, private_root, listener_port) = managed_test_sidecar();
        let control = ScriptedProcessControl::reap_then_fail();

        cleanup_managed_sidecar_with_bounds(&control, &mut managed, Duration::ZERO, Duration::ZERO)
            .unwrap_err();
        let signals_after_first_attempt = control.signals.lock().unwrap().len();
        cleanup_managed_sidecar_with_bounds(&control, &mut managed, Duration::ZERO, Duration::ZERO)
            .unwrap_err();

        assert_eq!(
            control.signals.lock().unwrap().len(),
            signals_after_first_attempt
        );
        assert!(!control.signalled_after_reap.load(Ordering::Acquire));
        assert_owned_resources(&managed, &private_root, listener_port);
        drop(managed);
        assert!(!private_root.exists());
    }

    #[test]
    fn cleanup_pending_blocks_restart_and_explicit_stop_retries_cleanup() {
        let state = DesktopHostState::default();
        let (managed, private_root, listener_port) = managed_test_sidecar();
        *state.sidecar.lock().unwrap() = Some(managed);
        let control = ScriptedProcessControl::term_failure();
        {
            let mut sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_mut().unwrap();
            cleanup_managed_sidecar_with_bounds(&control, managed, Duration::ZERO, Duration::ZERO)
                .unwrap_err();
            assert_owned_resources(managed, &private_root, listener_port);
        }

        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));
        assert_eq!(error.code, "sidecar_stop_failed_owned");
        assert!(private_root.exists());

        assert_eq!(stop_sidecar_inner(&state).unwrap().state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(state.parent_liveness.lock().unwrap().is_none());
        assert!(!private_root.exists());
    }

    #[test]
    fn status_try_wait_failure_marks_cleanup_pending_and_retains_resources() {
        let state = DesktopHostState::default();
        let (managed, private_root, listener_port) = managed_test_sidecar();
        *state.sidecar.lock().unwrap() = Some(managed);

        let error =
            host_status_inner_with(&state, &ScriptedProcessControl::wait_failure()).unwrap_err();

        assert_eq!(error.code, "sidecar_process_inspection_failed");
        {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::CleanupPending);
            assert_owned_resources(managed, &private_root, listener_port);
        }
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
    }

    #[test]
    fn start_try_wait_failure_marks_cleanup_pending_and_retains_resources() {
        let state = DesktopHostState::default();
        let (managed, private_root, listener_port) = managed_test_sidecar();
        *state.sidecar.lock().unwrap() = Some(managed);

        let error = expect_host_error(start_sidecar_inner_with(
            &state,
            LaunchPolicy::Release,
            None,
            &ScriptedProcessControl::wait_failure(),
        ));

        assert_eq!(error.code, "sidecar_process_inspection_failed");
        {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::CleanupPending);
            assert_owned_resources(managed, &private_root, listener_port);
        }
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
    }

    #[test]
    fn state_lock_timeout_keeps_starting_ownership_and_blocks_restart() {
        let state = Arc::new(DesktopHostState::default());
        let (mut managed, private_root, listener_port) = managed_test_sidecar();
        managed.lifecycle = ManagedLifecycle::Starting;
        managed.status.state = "starting".to_string();
        managed.status.url = None;
        *state.sidecar.lock().unwrap() = Some(managed);
        let guard = state.sidecar.lock().unwrap();
        let failure_state = Arc::clone(&state);

        let failure = thread::spawn(move || {
            fail_state_owned_startup_with_bounds(
                &failure_state,
                &OsProcessControl,
                sidecar_start_cancelled_error(),
                Duration::from_millis(30),
                Duration::ZERO,
                Duration::ZERO,
            )
        });
        let error = failure.join().unwrap();

        assert_eq!(error.code, "sidecar_state_timeout");
        let managed = guard.as_ref().unwrap();
        assert_eq!(managed.lifecycle, ManagedLifecycle::Starting);
        assert_managed_resources(managed, &private_root, listener_port);
        drop(guard);

        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));
        assert_eq!(error.code, "sidecar_start_in_progress");
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
    }

    #[test]
    fn successful_spawn_fills_the_preowned_starting_slot() {
        let state = DesktopHostState::default();
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let allocated = allocate_sidecar_listener().unwrap();
        let port = allocated.port;
        reserve_starting_sidecar(
            &state,
            ManagedSidecar {
                status: LifecycleStatus {
                    state: "starting".to_string(),
                    port: Some(port),
                    pid: None,
                    url: None,
                },
                bootstrap: None,
                lifecycle: ManagedLifecycle::Starting,
                startup_epoch,
                instance_id: [2; INSTANCE_ID_BYTES],
                monitor_started: false,
                spawn_pending: true,
                child: None,
                process_group: 0,
                session_id: 0,
                birth_identity: None,
                group_signal_authority: GroupSignalAuthority::Anchored,
                process_cleanup_confirmed: false,
                _private_launch_dir: None,
                verified_executable: None,
                _listener: allocated.listener,
            },
        )
        .unwrap();

        spawn_sidecar_gated(&state, startup_epoch, || {
            Ok(spawn_test_process_group("sleep 30"))
        })
        .unwrap();

        {
            let sidecar = lock_sidecar_bounded(&state, Duration::ZERO).unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::Starting);
            assert!(managed.child.is_some());
            assert!(managed.process_group > 0);
            assert_eq!(managed.session_id, managed.process_group);
            assert!(managed.birth_identity.is_some());
            assert_eq!(
                managed.status.pid,
                Some(managed.child.as_ref().unwrap().id())
            );
            assert_eq!(managed._listener.local_addr().unwrap().port(), port);
        }
        drop(startup_claim);
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn poisoned_manager_during_handoff_retains_child_for_stop_retry() {
        let state = Arc::new(DesktopHostState::default());
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let allocated = allocate_sidecar_listener().unwrap();
        reserve_starting_sidecar(
            &state,
            ManagedSidecar {
                status: LifecycleStatus {
                    state: "starting".to_string(),
                    port: Some(allocated.port),
                    pid: None,
                    url: None,
                },
                bootstrap: None,
                lifecycle: ManagedLifecycle::Starting,
                startup_epoch,
                instance_id: [3; INSTANCE_ID_BYTES],
                monitor_started: false,
                spawn_pending: true,
                child: None,
                process_group: 0,
                session_id: 0,
                birth_identity: None,
                group_signal_authority: GroupSignalAuthority::Anchored,
                process_cleanup_confirmed: false,
                _private_launch_dir: None,
                verified_executable: None,
                _listener: allocated.listener,
            },
        )
        .unwrap();
        let poison_state = Arc::clone(&state);
        assert!(thread::spawn(move || {
            let _guard = poison_state.sidecar.lock().unwrap();
            panic!("inject manager poison before spawn handoff");
        })
        .join()
        .is_err());

        let error = spawn_sidecar_gated(&state, startup_epoch, || {
            Ok(spawn_test_process_group("sleep 30"))
        })
        .unwrap_err();

        assert_eq!(error.code, "sidecar_state_unavailable");
        {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::CleanupPending);
            assert!(!managed.spawn_pending);
            assert!(managed.child.is_some());
        }
        assert!(state.spawn_handoff.lock().unwrap().is_none());
        drop(startup_claim);
        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));
        assert_eq!(error.code, "sidecar_stop_failed_owned");
        assert_eq!(stop_sidecar_inner(&state).unwrap().state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
    }

    #[test]
    fn poisoned_state_and_cleanup_failure_retain_retryable_ownership() {
        let state = Arc::new(DesktopHostState::default());
        let (mut managed, private_root, listener_port) = managed_test_sidecar();
        managed.lifecycle = ManagedLifecycle::Starting;
        managed.status.state = "starting".to_string();
        managed.status.url = None;
        *state.sidecar.lock().unwrap() = Some(managed);

        let poison_state = Arc::clone(&state);
        assert!(thread::spawn(move || {
            let _guard = poison_state.sidecar.lock().unwrap();
            panic!("inject sidecar state poison");
        })
        .join()
        .is_err());

        let error = fail_state_owned_startup_with_bounds(
            &state,
            &ScriptedProcessControl::term_failure(),
            sidecar_start_cancelled_error(),
            Duration::ZERO,
            Duration::ZERO,
            Duration::ZERO,
        );

        assert_eq!(error.code, "sidecar_stop_failed_owned");
        {
            let sidecar = lock_sidecar_bounded(&state, Duration::ZERO).unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::CleanupPending);
            assert_owned_resources(managed, &private_root, listener_port);
        }
        let error = expect_host_error(start_sidecar_inner(&state, LaunchPolicy::Release, None));
        assert_eq!(error.code, "sidecar_stop_failed_owned");
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
    }

    #[test]
    fn ready_publication_waits_for_short_state_contention() {
        let state = Arc::new(DesktopHostState::default());
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let (mut managed, private_root, _) = managed_test_sidecar();
        managed.lifecycle = ManagedLifecycle::Starting;
        managed.status.state = "starting".to_string();
        managed.status.url = None;
        *state.sidecar.lock().unwrap() = Some(managed);
        state.launch_state.store(
            encode_launch_state(startup_epoch, LaunchPhase::Spawning),
            Ordering::Release,
        );
        let guard = state.sidecar.lock().unwrap();
        let before_state_lock = Arc::new(Barrier::new(2));
        let publish_state = Arc::clone(&state);
        let reached = Arc::clone(&before_state_lock);

        let publication = thread::spawn(move || {
            publish_sidecar_gated_with(
                &publish_state,
                startup_epoch,
                test_bootstrap_state(),
                Duration::from_secs(1),
                || {
                    reached.wait();
                },
            )
        });
        before_state_lock.wait();
        drop(guard);

        let status = publication.join().unwrap().unwrap();
        assert_eq!(status.schema_version, "1");
        assert_eq!(
            lock_sidecar_bounded(&state, Duration::ZERO)
                .unwrap()
                .as_ref()
                .unwrap()
                .lifecycle,
            ManagedLifecycle::Running
        );
        drop(startup_claim);
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
    }

    #[test]
    fn cancellation_gate_prevents_later_spawn_and_publish() {
        let state = Arc::new(DesktopHostState::default());
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let cancellation_advanced = Arc::new(Barrier::new(2));
        let release_cancellation = Arc::new(Barrier::new(2));
        let spawn_attempted = Arc::new(Barrier::new(2));

        let cancellation_state = Arc::clone(&state);
        let advanced = Arc::clone(&cancellation_advanced);
        let release = Arc::clone(&release_cancellation);
        let cancellation = thread::spawn(move || {
            advance_cancellation_with(&cancellation_state, || {
                advanced.wait();
                release.wait();
            });
        });
        cancellation_advanced.wait();

        let spawn_state = Arc::clone(&state);
        let attempted = Arc::clone(&spawn_attempted);
        let spawn_called = Arc::new(AtomicBool::new(false));
        let called = Arc::clone(&spawn_called);
        let spawn = thread::spawn(move || {
            attempted.wait();
            spawn_sidecar_gated(&spawn_state, startup_epoch, || {
                called.store(true, Ordering::Release);
                Ok(spawn_test_process_group("sleep 30"))
            })
        });
        spawn_attempted.wait();
        release_cancellation.wait();
        cancellation.join().unwrap();

        let error = spawn.join().unwrap().unwrap_err();
        assert_eq!(error.code, "sidecar_start_cancelled");
        assert!(!spawn_called.load(Ordering::Acquire));

        let (mut managed, private_root, _) = managed_test_sidecar();
        managed.lifecycle = ManagedLifecycle::Starting;
        managed.status.state = "starting".to_string();
        managed.status.url = None;
        *state.sidecar.lock().unwrap() = Some(managed);
        let error = expect_host_error(publish_sidecar_gated(
            &state,
            startup_epoch,
            test_bootstrap_state(),
        ));
        assert_eq!(error.code, "sidecar_start_cancelled");
        drop(startup_claim);
        stop_sidecar_inner(&state).unwrap();
        assert!(!private_root.exists());
        assert!(state.sidecar.lock().unwrap().is_none());
    }

    #[test]
    fn cancellation_advance_is_bounded_while_spawn_result_is_blocked() {
        let state = Arc::new(DesktopHostState::default());
        let (startup_claim, startup_epoch) = StartupClaim::acquire(&state).unwrap();
        let allocated = allocate_sidecar_listener().unwrap();
        reserve_starting_sidecar(
            &state,
            ManagedSidecar {
                status: LifecycleStatus {
                    state: "starting".to_string(),
                    port: Some(allocated.port),
                    pid: None,
                    url: None,
                },
                bootstrap: None,
                lifecycle: ManagedLifecycle::Starting,
                startup_epoch,
                instance_id: [4; INSTANCE_ID_BYTES],
                monitor_started: false,
                spawn_pending: true,
                child: None,
                process_group: 0,
                session_id: 0,
                birth_identity: None,
                group_signal_authority: GroupSignalAuthority::Anchored,
                process_cleanup_confirmed: false,
                _private_launch_dir: None,
                verified_executable: None,
                _listener: allocated.listener,
            },
        )
        .unwrap();
        let spawn_entered = Arc::new(Barrier::new(2));
        let release_spawn = Arc::new(Barrier::new(2));
        let spawn_state = Arc::clone(&state);
        let entered = Arc::clone(&spawn_entered);
        let release = Arc::clone(&release_spawn);
        let spawning = thread::spawn(move || {
            spawn_sidecar_gated(&spawn_state, startup_epoch, || {
                entered.wait();
                release.wait();
                Ok(spawn_test_process_group("sleep 30"))
            })
        });
        spawn_entered.wait();
        let (cancelled_tx, cancelled_rx) = mpsc::channel();
        let cancellation_state = Arc::clone(&state);
        let cancellation = thread::spawn(move || {
            advance_cancellation(&cancellation_state);
            cancelled_tx.send(()).unwrap();
        });

        let cancellation_was_bounded = cancelled_rx
            .recv_timeout(Duration::from_millis(100))
            .is_ok();
        release_spawn.wait();
        cancellation.join().unwrap();
        let _ = spawning.join().unwrap();

        assert!(
            cancellation_was_bounded,
            "cancellation waited for the blocking spawn operation"
        );
        drop(startup_claim);
        stop_sidecar_inner(&state).unwrap();
    }

    #[test]
    fn blocked_exec_handoff_is_owned_and_stop_remains_bounded() {
        let state = Arc::new(DesktopHostState::default());
        let (reserved_tx, reserved_rx) = mpsc::channel();
        let (begin_spawn_tx, begin_spawn_rx) = mpsc::channel();
        let (mut entered_reader, entered_writer) = UnixStream::pair().unwrap();
        let (_blocker_peer, blocker_child) = UnixStream::pair().unwrap();
        entered_reader
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let startup_state = Arc::clone(&state);
        let startup = thread::spawn(move || {
            let (_startup_claim, startup_epoch) = StartupClaim::acquire(&startup_state).unwrap();
            let allocated = allocate_sidecar_listener().unwrap();
            let launch = SidecarLaunchSpec {
                program: PathBuf::from("/bin/sh"),
                args: vec!["-c".to_string(), "sleep 30".to_string()],
                current_dir: None,
                remove_env: &[],
                private_launch_dir: None,
                verified_executable: None,
            };
            let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
            let parent_liveness = prepared.take_parent_liveness_writer().unwrap();
            unsafe {
                prepared.command.pre_exec(move || {
                    let pid = libc::getpid().to_ne_bytes();
                    if libc::write(entered_writer.as_raw_fd(), pid.as_ptr().cast(), pid.len())
                        != pid.len() as isize
                    {
                        return Err(pre_exec_error());
                    }
                    let mut byte = 0_u8;
                    loop {
                        let result =
                            libc::read(blocker_child.as_raw_fd(), (&mut byte as *mut u8).cast(), 1);
                        if result == -1 && current_errno() == libc::EINTR {
                            continue;
                        }
                        return Err(pre_exec_error());
                    }
                });
            }
            reserve_starting_sidecar(
                &startup_state,
                ManagedSidecar {
                    status: LifecycleStatus {
                        state: "starting".to_string(),
                        port: Some(allocated.port),
                        pid: None,
                        url: None,
                    },
                    bootstrap: None,
                    lifecycle: ManagedLifecycle::Starting,
                    startup_epoch,
                    instance_id: [5; INSTANCE_ID_BYTES],
                    monitor_started: false,
                    spawn_pending: true,
                    child: None,
                    process_group: 0,
                    session_id: 0,
                    birth_identity: None,
                    group_signal_authority: GroupSignalAuthority::Anchored,
                    process_cleanup_confirmed: false,
                    _private_launch_dir: None,
                    verified_executable: None,
                    _listener: allocated.listener,
                },
            )
            .unwrap();
            install_parent_liveness(&startup_state, parent_liveness).unwrap();
            reserved_tx.send(()).unwrap();
            begin_spawn_rx.recv().unwrap();

            match spawn_sidecar_gated(&startup_state, startup_epoch, || prepared.spawn()) {
                Ok(()) => Ok(()),
                Err(error) => Err(fail_state_owned_startup(
                    &startup_state,
                    &OsProcessControl,
                    error,
                )),
            }
        });
        reserved_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        let sidecar_guard = state.sidecar.lock().unwrap();
        begin_spawn_tx.send(()).unwrap();
        let mut pid_bytes = [0_u8; std::mem::size_of::<libc::pid_t>()];
        entered_reader.read_exact(&mut pid_bytes).unwrap();
        let child_pid = libc::pid_t::from_ne_bytes(pid_bytes);

        let started = Instant::now();
        let error = stop_sidecar_inner_with(&state, &OsProcessControl, Duration::from_millis(30))
            .unwrap_err();
        assert_eq!(error.code, "sidecar_state_timeout");
        assert!(started.elapsed() < Duration::from_millis(500));

        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let handoff = state
                .spawn_handoff
                .lock()
                .unwrap()
                .as_ref()
                .cloned()
                .unwrap();
            let outcome = handoff.outcome.lock().unwrap();
            if let SpawnHandoffOutcome::Spawned(spawned) = &*outcome {
                if OsProcessControl.leader_exited(&spawned.child).unwrap() {
                    break;
                }
            }
            drop(outcome);
            assert!(
                Instant::now() < deadline,
                "watchdog did not terminate the blocked pre-exec child"
            );
            thread::sleep(Duration::from_millis(10));
        }
        let managed = sidecar_guard.as_ref().unwrap();
        assert!(managed.spawn_pending);
        assert!(managed.child.is_none());
        drop(sidecar_guard);

        let startup_error = startup.join().unwrap().unwrap_err();
        assert_eq!(startup_error.code, "sidecar_start_cancelled");
        assert_eq!(stop_sidecar_inner(&state).unwrap().state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(state.spawn_handoff.lock().unwrap().is_none());
        assert!(state.parent_liveness.lock().unwrap().is_none());
        assert!(wait_for_pid_exit(child_pid, Duration::from_secs(1)));
    }

    #[test]
    fn exit_is_bounded_and_parent_liveness_kills_while_state_lock_is_busy() {
        let state = DesktopHostState::default();
        let allocated = allocate_sidecar_listener().unwrap();
        let launch = SidecarLaunchSpec {
            program: PathBuf::from("/bin/sh"),
            args: vec!["-c".to_string(), "sleep 30".to_string()],
            current_dir: None,
            remove_env: &[],
            private_launch_dir: None,
            verified_executable: None,
        };
        let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let parent_liveness = prepared.take_parent_liveness_writer().unwrap();
        let child = prepared.spawn().unwrap();
        let process_group = child.id() as i32;
        install_parent_liveness(&state, parent_liveness).unwrap();
        let managed = ManagedSidecar {
            status: LifecycleStatus {
                state: "running".to_string(),
                port: Some(allocated.port),
                pid: Some(child.id()),
                url: None,
            },
            bootstrap: None,
            lifecycle: ManagedLifecycle::Running,
            startup_epoch: 0,
            instance_id: [6; INSTANCE_ID_BYTES],
            monitor_started: false,
            spawn_pending: false,
            child: Some(child),
            process_group,
            session_id: process_group,
            birth_identity: None,
            group_signal_authority: GroupSignalAuthority::Anchored,
            process_cleanup_confirmed: false,
            _private_launch_dir: None,
            verified_executable: None,
            _listener: allocated.listener,
        };
        *state.sidecar.lock().unwrap() = Some(managed);
        state.launch_state.store(
            encode_launch_state(0, LaunchPhase::Published),
            Ordering::Release,
        );
        let guard = state.sidecar.lock().unwrap();
        let started = Instant::now();

        cleanup_sidecar_on_exit_with(
            &state,
            &OsProcessControl,
            Duration::from_millis(30),
            Duration::from_millis(10),
        );

        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(state.shutdown_requested.load(Ordering::Acquire));
        assert!(state.cancellation_epoch.load(Ordering::Acquire) > 0);
        assert!(guard.is_some());
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            if OsProcessControl
                .leader_exited(guard.as_ref().unwrap().child.as_ref().unwrap())
                .unwrap()
            {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "exit hook did not kill the child"
            );
            thread::sleep(Duration::from_millis(10));
        }
        drop(guard);
        stop_sidecar_inner(&state).unwrap();
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!process_group_exists(process_group).unwrap());
    }

    #[test]
    fn child_secret_output_has_no_native_or_renderer_read_channel() {
        let allocated = allocate_sidecar_listener().unwrap();
        let script = concat!(
            "printf 'ghp_bare-secret\\nsk-proj-bare-secret\\ne30.eyJzZWNyZXQiOiJqd3QifQ.sig\\n'; ",
            "printf 'token=\\nvalue-on-next-line\\n'; ",
            "printf '\\377invalid-utf8\\n'; ",
            "printf '%s\\n' '-----BEGIN PRIVATE KEY-----'; ",
            "printf '%s\\n' 'split-pem-secret' >&2; ",
            "printf '%s\\n' '-----END PRIVATE KEY-----' >&2"
        );
        let launch = SidecarLaunchSpec {
            program: PathBuf::from("/bin/sh"),
            args: vec!["-c".to_string(), script.to_string()],
            current_dir: None,
            remove_env: &[],
            private_launch_dir: None,
            verified_executable: None,
        };
        let mut prepared = command_from_launch_spec(&launch, &allocated.listener).unwrap();
        let mut child = prepared.spawn().unwrap();
        let process_group = child.id() as i32;
        drop(child.stdin.take());

        assert!(child.stdout.is_none());
        assert!(child.stderr.is_none());
        terminate_process_group(
            &mut child,
            process_group,
            Duration::from_millis(200),
            Duration::from_millis(200),
        )
        .unwrap();
    }

    #[test]
    #[ignore = "requires a freshly generated strict Local API PyInstaller externalBin"]
    fn packaged_external_bin_native_launch_smoke() {
        let _guard = ENV_LOCK.lock().unwrap();
        let test_home = unique_test_dir();
        fs::create_dir(&test_home).unwrap();
        let test_home_text = test_home.to_str().unwrap();
        let _environment = ScopedEnvironment::set(&[
            ("HOME", test_home_text),
            (
                "_PYI_APPLICATION_HOME_DIR",
                "/tmp/attacker-controlled-extraction",
            ),
            ("_PYI_ARCHIVE_FILE", "/tmp/attacker-controlled-archive"),
            ("_PYI_PARENT_PROCESS_LEVEL", "99"),
            ("_PYI_SPLASH_IPC", "31337"),
            ("_PYI_UNKNOWN_PRIVATE_STATE", "poisoned"),
            (PYINSTALLER_RESET_ENVIRONMENT, "0"),
        ]);
        let path = PathBuf::from(
            std::env::var_os("OPENEVO_PACKAGED_SIDECAR_PATH")
                .expect("OPENEVO_PACKAGED_SIDECAR_PATH is required"),
        );
        let state = DesktopHostState::default();

        let context = start_sidecar_inner(&state, LaunchPolicy::Release, Some(&path)).unwrap();
        assert_eq!(context.schema_version, "1");
        let endpoint_port = context
            .endpoint
            .strip_prefix("http://127.0.0.1:")
            .unwrap()
            .parse::<u16>()
            .unwrap();
        let private_root = {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::Running);
            assert_eq!(managed.status.port, Some(endpoint_port));
            assert_eq!(
                managed._listener.local_addr().unwrap().port(),
                endpoint_port
            );
            let executable = managed.verified_executable.as_ref().unwrap();
            executable.validate().unwrap();
            #[cfg(target_os = "linux")]
            assert_eq!(executable.identity.links, 0);
            #[cfg(target_os = "macos")]
            assert_eq!(executable.identity.links, 1);
            let root = managed._private_launch_dir.as_ref().unwrap().path();
            #[cfg(target_os = "linux")]
            assert_eq!(fs::read_dir(root).unwrap().count(), 0);
            #[cfg(target_os = "macos")]
            assert_eq!(fs::read_dir(root).unwrap().count(), 1);
            root.to_path_buf()
        };

        stop_sidecar_inner(&state).unwrap();

        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!private_root.exists());
        assert!(path.exists());
        fs::remove_dir_all(test_home).unwrap();
    }

    fn serve_health(
        listener: TcpListener,
        instance_id: [u8; INSTANCE_ID_BYTES],
        readiness_key: [u8; READINESS_KEY_BYTES],
        proof_challenge: Option<String>,
        include_unknown_field: bool,
    ) {
        serve_health_on_listener(
            &listener,
            instance_id,
            readiness_key,
            proof_challenge,
            include_unknown_field,
        );
    }

    fn serve_health_on_listener(
        listener: &TcpListener,
        instance_id: [u8; INSTANCE_ID_BYTES],
        readiness_key: [u8; READINESS_KEY_BYTES],
        proof_challenge: Option<String>,
        include_unknown_field: bool,
    ) {
        let (mut stream, _) = listener.accept().unwrap();
        let request = read_http_request(&mut stream);
        let challenge = request
            .lines()
            .find_map(|line| line.strip_prefix("X-OpenEvo-Native-Challenge: "))
            .unwrap();
        let proof_challenge = proof_challenge.as_deref().unwrap_or(challenge);
        let instance_id = encode_hex(&instance_id);
        let mut mac = Hmac::<Sha256>::new_from_slice(&readiness_key).unwrap();
        mac.update(readiness_hmac_domain(&instance_id, proof_challenge).as_bytes());
        let proof = encode_hex(&mac.finalize().into_bytes());
        let mut body = serde_json::json!({
            "service": "openevo-sidecar",
            "status": "ok",
            "protocol": NATIVE_SIDECAR_PROTOCOL,
            "instance_id": instance_id,
            "instance_proof": proof,
        });
        if include_unknown_field {
            body.as_object_mut()
                .unwrap()
                .insert("unexpected".to_string(), serde_json::json!(true));
        }
        write_json_response(&mut stream, 200, body);
    }

    fn serve_session_probe(listener: &TcpListener, accept_missing: bool) {
        let (mut authenticated, _) = listener.accept().unwrap();
        let authenticated_request = read_http_request(&mut authenticated);
        assert!(authenticated_request
            .lines()
            .any(|line| line.starts_with(&format!("{NATIVE_SESSION_HEADER}: "))));
        write_empty_response(&mut authenticated, 204);
        drop(authenticated);

        let (mut unauthenticated, _) = listener.accept().unwrap();
        let unauthenticated_request = read_http_request(&mut unauthenticated);
        assert!(!unauthenticated_request
            .lines()
            .any(|line| line.starts_with(&format!("{NATIVE_SESSION_HEADER}: "))));
        write_empty_response(&mut unauthenticated, if accept_missing { 204 } else { 403 });
    }

    fn serve_contract_response(
        listener: &TcpListener,
        version: serde_json::Value,
        legacy_status: u16,
        requests: mpsc::Sender<String>,
    ) {
        let (mut version_stream, _) = listener.accept().unwrap();
        let version_request = read_http_request(&mut version_stream);
        let _ = requests.send(version_request);
        write_json_response(&mut version_stream, 200, version);
        drop(version_stream);

        let (mut legacy_stream, _) = listener.accept().unwrap();
        let legacy_request = read_http_request(&mut legacy_stream);
        let _ = requests.send(legacy_request);
        write_json_response(
            &mut legacy_stream,
            legacy_status,
            serde_json::json!({"detail": "not found"}),
        );
    }

    fn read_http_request(stream: &mut TcpStream) -> String {
        let mut request = Vec::new();
        let mut buffer = [0_u8; 512];
        loop {
            let count = stream.read(&mut buffer).unwrap();
            assert!(count > 0);
            request.extend_from_slice(&buffer[..count]);
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        String::from_utf8(request).unwrap()
    }

    fn read_http_request_with_body(stream: &mut TcpStream) -> (String, Vec<u8>) {
        let mut headers = Vec::new();
        let mut byte = [0_u8; 1];
        while !headers.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            headers.push(byte[0]);
            assert!(headers.len() <= 8192);
        }
        let headers = String::from_utf8(headers).unwrap();
        let content_length = headers
            .lines()
            .find_map(|line| line.strip_prefix("Content-Length: "))
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0);
        let mut body = vec![0_u8; content_length];
        stream.read_exact(&mut body).unwrap();
        (headers, body)
    }

    fn write_json_response(stream: &mut TcpStream, status: u16, body: serde_json::Value) {
        let body = serde_json::to_string(&body).unwrap();
        let reason = match status {
            200 => "OK",
            404 => "Not Found",
            _ => "Response",
        };
        let response = format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes()).unwrap();
    }

    fn write_empty_response(stream: &mut TcpStream, status: u16) {
        let response =
            format!("HTTP/1.1 {status} Response\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        stream.write_all(response.as_bytes()).unwrap();
    }

    fn valid_version_response() -> serde_json::Value {
        serde_json::json!({
            "schema_version": "1",
            "api_name": DESKTOP_LOCAL_API_NAME,
            "preferred_major": 1,
            "supported_majors": [1],
            "openapi_sha256": DESKTOP_LOCAL_API_OPENAPI_SHA256,
            "build_version": "0.1.0",
            "source_commit": "0123456789abcdef0123456789abcdef01234567",
            "build_channel": "release",
            "provider_kind": "desktop_sidecar",
            "feature_flags": ["remote_profiles", "project_validation"],
        })
    }

    fn valid_version_response_with(key: &str, value: serde_json::Value) -> serde_json::Value {
        let mut response = valid_version_response();
        response
            .as_object_mut()
            .unwrap()
            .insert(key.to_string(), value);
        response
    }

    fn read_verified_file(executable: &VerifiedExecutableFile) -> Vec<u8> {
        let size = usize::try_from(executable.identity.size).unwrap();
        let mut value = vec![0_u8; size];
        let mut offset = 0;
        while offset < size {
            let count = executable
                .file
                .read_at(&mut value[offset..], offset as u64)
                .unwrap();
            assert!(count > 0);
            offset += count;
        }
        value
    }

    fn overwrite_same_length(path: &Path, value: &[u8]) {
        let mut file = OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(path)
            .unwrap();
        file.write_all(value).unwrap();
        file.sync_all().unwrap();
    }

    fn mock_directory_identity(owner: u32) -> FileIdentity {
        FileIdentity {
            device: 1,
            inode: 2,
            mode: DIRECTORY_FILE_TYPE | 0o755,
            links: 1,
            owner,
            size: 0,
            modified_seconds: 0,
            modified_nanoseconds: 0,
            changed_seconds: 0,
            changed_nanoseconds: 0,
        }
    }

    fn spawn_test_process_group(script: &str) -> Child {
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", script])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().unwrap()
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn spawn_exiting_leader_with_descendant(pid_path: &Path) -> Child {
        let script = format!(
            "exec python3 -c 'import os,time; p=os.fork(); open(\"{}\", \"w\").write(str(p)) if p else time.sleep(30); os._exit(0) if p else None'",
            pid_path.display(),
        );
        spawn_test_process_group(&script)
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    fn wait_for_leader_exit(leader: &Child) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while !leader_exited_without_reaping(leader).unwrap() {
            assert!(Instant::now() < deadline, "leader did not exit");
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn wait_for_file(path: &Path) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while !path.exists() {
            assert!(Instant::now() < deadline, "timed out waiting for pid file");
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn wait_for_pid_exit(pid: i32, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            let result = unsafe { libc::kill(pid, 0) };
            if result == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn test_credential() -> NativeInstanceCredential {
        NativeInstanceCredential {
            instance_id: [0x1a; INSTANCE_ID_BYTES],
            readiness_key: [0x5a; READINESS_KEY_BYTES],
            session_token: [0x7c; SESSION_TOKEN_BYTES],
            handoff_token: [0x8d; HANDOFF_TOKEN_BYTES],
        }
    }

    fn test_picker_operation(action_id: &str) -> NativePickerOperation {
        NativePickerOperation {
            sidecar_instance: [0x11; INSTANCE_ID_BYTES],
            action_id: action_id.to_string(),
            cancellation_token: [0x4d; WORKSPACE_CANCELLATION_TOKEN_BYTES],
            cancelled: AtomicBool::new(false),
        }
    }

    fn test_bootstrap_state() -> SidecarBootstrapState {
        SidecarBootstrapState {
            session_credential: SessionCredential([0x7c; SESSION_TOKEN_BYTES]),
            readiness_credential: ReadinessCredential([0x5a; READINESS_KEY_BYTES]),
            handoff_credential: HandoffCredential([0x8d; HANDOFF_TOKEN_BYTES]),
            negotiated_contract: NegotiatedContractV1 {
                major: 1,
                openapi_sha256: DESKTOP_LOCAL_API_OPENAPI_SHA256.to_string(),
                provider_kind: "desktop_sidecar".to_string(),
                feature_flags: vec![
                    FeatureFlagV1::RemoteProfiles,
                    FeatureFlagV1::ProjectValidation,
                ],
            },
        }
    }

    fn clear_sidecar_env() {
        for name in RELEASE_FORBIDDEN_SIDECAR_ENV {
            std::env::remove_var(name);
        }
    }

    struct ScopedEnvironment(Vec<(OsString, Option<OsString>)>);

    impl ScopedEnvironment {
        fn set(values: &[(&str, &str)]) -> Self {
            let mut previous = Vec::with_capacity(values.len());
            for (name, value) in values {
                previous.push((OsString::from(name), std::env::var_os(name)));
                std::env::set_var(name, value);
            }
            Self(previous)
        }
    }

    impl Drop for ScopedEnvironment {
        fn drop(&mut self) {
            for (name, value) in self.0.drain(..).rev() {
                if let Some(value) = value {
                    std::env::set_var(name, value);
                } else {
                    std::env::remove_var(name);
                }
            }
        }
    }

    struct SidecarFixture {
        root: PathBuf,
        path: PathBuf,
    }

    impl SidecarFixture {
        fn executable(contents: &[u8]) -> Self {
            Self::file(contents, 0o700)
        }

        fn from_existing(source: &Path) -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let path = root.join(BUNDLED_SIDECAR_BINARY);
            fs::copy(source, &path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self { root, path }
        }

        fn non_executable() -> Self {
            Self::file(b"sidecar", 0o600)
        }

        fn writable() -> Self {
            Self::file(b"sidecar", 0o722)
        }

        fn group_writable() -> Self {
            Self::file(b"sidecar", 0o770)
        }

        fn file(contents: &[u8], mode: u32) -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let path = root.join(BUNDLED_SIDECAR_BINARY);
            fs::write(&path, contents).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            Self { root, path }
        }

        fn symlink() -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let target = root.join("target");
            fs::write(&target, b"sidecar").unwrap();
            fs::set_permissions(&target, fs::Permissions::from_mode(0o700)).unwrap();
            let path = root.join(BUNDLED_SIDECAR_BINARY);
            symlink(&target, &path).unwrap();
            Self { root, path }
        }

        fn directory() -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let path = root.join(BUNDLED_SIDECAR_BINARY);
            fs::create_dir(&path).unwrap();
            Self { root, path }
        }

        fn hard_link() -> Self {
            let fixture = Self::executable(b"sidecar");
            hard_link(&fixture.path, fixture.root.join("second-link")).unwrap();
            fixture
        }

        fn missing() -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let path = root.join(BUNDLED_SIDECAR_BINARY);
            Self { root, path }
        }

        fn through_directory_symlink() -> Self {
            let root = unique_test_dir();
            fs::create_dir(&root).unwrap();
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
            let real = root.join("real");
            fs::create_dir(&real).unwrap();
            let linked = root.join("linked");
            symlink(&real, &linked).unwrap();
            let path = linked.join(BUNDLED_SIDECAR_BINARY);
            fs::write(real.join(BUNDLED_SIDECAR_BINARY), b"sidecar").unwrap();
            fs::set_permissions(
                real.join(BUNDLED_SIDECAR_BINARY),
                fs::Permissions::from_mode(0o700),
            )
            .unwrap();
            Self { root, path }
        }

        fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for SidecarFixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn unique_test_dir() -> PathBuf {
        static NEXT_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let id = NEXT_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        std::env::temp_dir().join(format!("openevo-native-host-{}-{id}", std::process::id()))
    }

    struct ScriptedProcessControl {
        fail_wait: bool,
        fail_reap: bool,
        fail_term: bool,
        fail_kill: bool,
        group_states: Mutex<VecDeque<bool>>,
        signals: Mutex<Vec<i32>>,
        leader_reaped: AtomicBool,
        signalled_after_reap: AtomicBool,
    }

    impl ScriptedProcessControl {
        fn term_failure() -> Self {
            Self {
                fail_wait: false,
                fail_reap: false,
                fail_term: true,
                fail_kill: false,
                group_states: Mutex::new(VecDeque::from([false, false])),
                signals: Mutex::new(Vec::new()),
                leader_reaped: AtomicBool::new(false),
                signalled_after_reap: AtomicBool::new(false),
            }
        }

        fn kill_failure() -> Self {
            Self {
                fail_wait: false,
                fail_reap: false,
                fail_term: false,
                fail_kill: true,
                group_states: Mutex::new(VecDeque::from([true, false])),
                signals: Mutex::new(Vec::new()),
                leader_reaped: AtomicBool::new(false),
                signalled_after_reap: AtomicBool::new(false),
            }
        }

        fn wait_failure() -> Self {
            Self {
                fail_wait: true,
                fail_reap: false,
                fail_term: false,
                fail_kill: false,
                group_states: Mutex::new(VecDeque::new()),
                signals: Mutex::new(Vec::new()),
                leader_reaped: AtomicBool::new(false),
                signalled_after_reap: AtomicBool::new(false),
            }
        }

        fn reap_then_fail() -> Self {
            Self {
                fail_wait: false,
                fail_reap: true,
                fail_term: false,
                fail_kill: false,
                group_states: Mutex::new(VecDeque::from([false])),
                signals: Mutex::new(Vec::new()),
                leader_reaped: AtomicBool::new(false),
                signalled_after_reap: AtomicBool::new(false),
            }
        }
    }

    impl ProcessControl for ScriptedProcessControl {
        fn leader_exited(&self, _child: &Child) -> std::io::Result<bool> {
            if self.fail_wait {
                Err(std::io::Error::other("injected wait failure"))
            } else {
                Ok(true)
            }
        }

        fn reap_leader(&self, child: &mut Child) -> std::io::Result<Option<ExitStatus>> {
            self.leader_reaped.store(true, Ordering::Release);
            if self.fail_reap {
                let _ = child.kill();
                let _ = child.wait();
                Err(std::io::Error::other("injected reap failure"))
            } else {
                Ok(Some(ExitStatus::from_raw(0)))
            }
        }

        fn signal_group(&self, _process_group: i32, signal: libc::c_int) -> std::io::Result<()> {
            if self.leader_reaped.load(Ordering::Acquire) {
                self.signalled_after_reap.store(true, Ordering::Release);
            }
            self.signals.lock().unwrap().push(signal);
            if (signal == libc::SIGTERM && self.fail_term)
                || (signal == libc::SIGKILL && self.fail_kill)
            {
                Err(std::io::Error::other("injected signal failure"))
            } else {
                Ok(())
            }
        }

        fn group_has_members_except_leader(
            &self,
            _process_group: i32,
            _leader: i32,
        ) -> std::io::Result<bool> {
            Ok(self
                .group_states
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(false))
        }

        fn sleep(&self, _duration: Duration) {}
    }

    fn managed_test_sidecar() -> (ManagedSidecar, PathBuf, u16) {
        let fixture = SidecarFixture::from_existing(Path::new("/bin/sh"));
        let (verified_executable, private_launch_dir) =
            prepare_packaged_sidecar(fixture.path()).unwrap();
        let private_root = private_launch_dir.path().to_path_buf();
        let allocated = allocate_sidecar_listener().unwrap();
        let listener_port = allocated.port;
        let child = spawn_test_process_group("sleep 30");
        let process_group = child.id() as i32;
        let (_, session_id, birth_identity) =
            sidecar_process_birth_identity(process_group).unwrap();
        (
            ManagedSidecar {
                status: LifecycleStatus {
                    state: "running".to_string(),
                    port: Some(listener_port),
                    pid: Some(child.id()),
                    url: Some(format!("http://127.0.0.1:{listener_port}/openevo")),
                },
                bootstrap: Some(test_bootstrap_state()),
                lifecycle: ManagedLifecycle::Running,
                startup_epoch: 0,
                instance_id: [7; INSTANCE_ID_BYTES],
                monitor_started: false,
                spawn_pending: false,
                child: Some(child),
                process_group,
                session_id,
                birth_identity: Some(birth_identity),
                group_signal_authority: GroupSignalAuthority::Anchored,
                process_cleanup_confirmed: false,
                _private_launch_dir: Some(private_launch_dir),
                verified_executable: Some(verified_executable),
                _listener: allocated.listener,
            },
            private_root,
            listener_port,
        )
    }

    fn assert_owned_resources(managed: &ManagedSidecar, private_root: &Path, listener_port: u16) {
        assert_eq!(managed.status.state, "cleanup_pending");
        assert_managed_resources(managed, private_root, listener_port);
    }

    fn assert_managed_resources(managed: &ManagedSidecar, private_root: &Path, listener_port: u16) {
        assert!(managed._private_launch_dir.is_some());
        assert!(private_root.exists());
        managed
            .verified_executable
            .as_ref()
            .unwrap()
            .validate()
            .unwrap();
        assert_eq!(
            managed._listener.local_addr().unwrap().port(),
            listener_port
        );
        assert!(managed.child.as_ref().unwrap().id() > 0);
    }
}
