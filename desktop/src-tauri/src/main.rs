use std::ffi::CString;
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard, TryLockError};
use std::thread;
use std::time::{Duration, Instant};

use hmac::{Hmac, Mac};
use rand::rngs::OsRng;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::Manager;
use tempfile::TempDir;

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
compile_error!("OpenEvo Desktop native sidecar FD execution supports only Linux and macOS");

const BUNDLED_SIDECAR_BINARY: &str = "openevo-desktop-sidecar";
const NATIVE_SIDECAR_PROTOCOL: &str = "openevo-native-sidecar-v1";
const NATIVE_EXECUTABLE_FD_ENV: &str = "OPENEVO_NATIVE_EXECUTABLE_FD";
const INHERITED_LISTENER_FD: libc::c_int = 3;
const INHERITED_EXECUTABLE_FD: libc::c_int = 4;
const INSTANCE_ID_BYTES: usize = 16;
const READINESS_KEY_BYTES: usize = 32;
const NATIVE_INSTANCE_FRAME_MAX_BYTES: usize = 256;
const SIDECAR_STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const SIDECAR_HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(100);
const SIDECAR_HEALTH_CONNECT_TIMEOUT: Duration = Duration::from_millis(250);
const SIDECAR_HEALTH_RESPONSE_MAX_BYTES: usize = 4096;
const SIDECAR_TERM_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_KILL_TIMEOUT: Duration = Duration::from_secs(1);
const SIDECAR_STOP_POLL_INTERVAL: Duration = Duration::from_millis(20);
const SIDECAR_STATE_LOCK_TIMEOUT: Duration = Duration::from_secs(3);
const SIDECAR_EXIT_EMERGENCY_TERM_GRACE: Duration = Duration::from_millis(250);
const RELEASE_FORBIDDEN_SIDECAR_ENV: [&str; 6] = [
    "OPENEVO_DESKTOP_SIDECAR_COMMAND",
    "OPENEVO_DESKTOP_SIDECAR_PROGRAM",
    "OPENEVO_DESKTOP_SIDECAR_ARGS_JSON",
    "OPENEVO_DESKTOP_SIDECAR_WORKDIR",
    "OPENEVO_DESKTOP_BACKEND_BASE_URL",
    NATIVE_EXECUTABLE_FD_ENV,
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

const FILE_TYPE_MASK: u32 = libc::S_IFMT as u32;
const DIRECTORY_FILE_TYPE: u32 = libc::S_IFDIR as u32;
const REGULAR_FILE_TYPE: u32 = libc::S_IFREG as u32;
const SYMLINK_FILE_TYPE: u32 = libc::S_IFLNK as u32;

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
            let _ = fs::remove_dir(self.path());
        }
    }
}

impl VerifiedExecutableFile {
    fn validate(&self) -> HostResult<()> {
        let identity = file_identity(&self.file).map_err(|_| private_sidecar_error())?;
        let access_mode = unsafe { libc::fcntl(self.file.as_raw_fd(), libc::F_GETFL) };
        if identity != self.identity
            || identity.links != 0
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
}

impl PreparedCommand {
    fn spawn(&mut self) -> std::io::Result<Child> {
        self.command.spawn()
    }
}

struct AllocatedSidecarListener {
    listener: TcpListener,
    port: u16,
}

struct NativeInstanceCredential {
    instance_id: [u8; INSTANCE_ID_BYTES],
    readiness_key: [u8; READINESS_KEY_BYTES],
}

#[derive(Serialize)]
struct NativeInstanceFrame<'a> {
    protocol: &'static str,
    instance_id: &'a str,
    readiness_key: &'a str,
}

impl NativeInstanceCredential {
    fn generate() -> HostResult<Self> {
        let mut instance_id = [0_u8; INSTANCE_ID_BYTES];
        let mut readiness_key = [0_u8; READINESS_KEY_BYTES];
        OsRng
            .try_fill_bytes(&mut instance_id)
            .map_err(|_| instance_credential_error())?;
        OsRng
            .try_fill_bytes(&mut readiness_key)
            .map_err(|_| instance_credential_error())?;
        Ok(Self {
            instance_id,
            readiness_key,
        })
    }

    fn instance_id_hex(&self) -> String {
        encode_hex(&self.instance_id)
    }

    fn write_to_child(&self, child: &mut Child) -> HostResult<()> {
        let mut stdin = child.stdin.take().ok_or_else(instance_channel_error)?;
        let instance_id = self.instance_id_hex();
        let readiness_key = encode_hex(&self.readiness_key);
        let frame = NativeInstanceFrame {
            protocol: NATIVE_SIDECAR_PROTOCOL,
            instance_id: &instance_id,
            readiness_key: &readiness_key,
        };
        let mut encoded = serde_json::to_vec(&frame).map_err(|_| instance_channel_error())?;
        encoded.push(b'\n');
        if encoded.len() > NATIVE_INSTANCE_FRAME_MAX_BYTES {
            return Err(instance_channel_error());
        }
        stdin
            .write_all(&encoded)
            .map_err(|_| instance_channel_error())?;
        stdin.flush().map_err(|_| instance_channel_error())?;
        encoded.fill(0);
        Ok(())
    }
}

impl Drop for NativeInstanceCredential {
    fn drop(&mut self) {
        self.instance_id.fill(0);
        self.readiness_key.fill(0);
    }
}

#[derive(Clone, Debug, serde::Serialize)]
struct SidecarStatus {
    state: String,
    port: Option<u16>,
    pid: Option<u32>,
    url: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ManagedLifecycle {
    Starting,
    Running,
    CleanupPending,
}

struct ManagedSidecar {
    status: SidecarStatus,
    lifecycle: ManagedLifecycle,
    child: Child,
    process_group: i32,
    _private_launch_dir: Option<PrivateLaunchDirectory>,
    verified_executable: Option<VerifiedExecutableFile>,
    _listener: TcpListener,
}

impl ManagedSidecar {
    fn mark_cleanup_pending(&mut self) {
        self.lifecycle = ManagedLifecycle::CleanupPending;
        self.status.state = "cleanup_pending".to_string();
        self.status.url = None;
    }
}

struct DesktopHostState {
    sidecar: Mutex<Option<ManagedSidecar>>,
    cancellation_epoch: AtomicU64,
    shutdown_requested: AtomicBool,
    emergency_process_group: AtomicI32,
}

impl Default for DesktopHostState {
    fn default() -> Self {
        Self {
            sidecar: Mutex::new(None),
            cancellation_epoch: AtomicU64::new(0),
            shutdown_requested: AtomicBool::new(false),
            emergency_process_group: AtomicI32::new(0),
        }
    }
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

trait ProcessControl {
    fn try_wait(&self, child: &mut Child) -> std::io::Result<Option<ExitStatus>>;
    fn signal_group(&self, process_group: i32, signal: libc::c_int) -> std::io::Result<()>;
    fn group_exists(&self, process_group: i32) -> std::io::Result<bool>;
    fn sleep(&self, duration: Duration);
}

#[derive(Clone, Copy)]
struct OsProcessControl;

impl ProcessControl for OsProcessControl {
    fn try_wait(&self, child: &mut Child) -> std::io::Result<Option<ExitStatus>> {
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

    fn group_exists(&self, process_group: i32) -> std::io::Result<bool> {
        let result = unsafe { libc::kill(-process_group, 0) };
        if result == 0 {
            return Ok(true);
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::ESRCH) => Ok(false),
            Some(libc::EPERM) => Ok(true),
            _ => Err(error),
        }
    }

    fn sleep(&self, duration: Duration) {
        thread::sleep(duration);
    }
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

fn stopped_sidecar_status() -> SidecarStatus {
    SidecarStatus {
        state: "stopped".to_string(),
        port: None,
        pid: None,
        url: None,
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
    port: u16,
) -> HostResult<SidecarLaunchSpec> {
    let source = bundled_path.ok_or_else(bundled_sidecar_missing_error)?;
    let (verified_executable, private_launch_dir) = prepare_packaged_sidecar(source)?;
    Ok(SidecarLaunchSpec {
        program: fd_execution_path(),
        args: local_sidecar_args(port),
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
    let expected_owner = trusted_app_executable_owner()?;
    let (parent, name) = open_trusted_source_parent(path)?;
    let initial_identity = source_identity_at(&parent, &name)?;
    validate_packaged_source_identity(&initial_identity, expected_owner)?;
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
    let initial_digest = hash_file_at(&source, initial_identity.size)
        .map_err(|_| packaged_sidecar_identity_error())?;

    let private_dir = PrivateLaunchDirectory::create()?;
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
    if unsafe { libc::unlinkat(private_directory.as_raw_fd(), private_name.as_ptr(), 0) } == -1 {
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
    if final_writer_identity != final_reader_identity
        || final_reader_identity.links != 0
        || final_reader_identity.owner != unsafe { libc::geteuid() }
        || final_reader_identity.size != initial_identity.size
        || final_reader_identity.mode & 0o777 != 0o500
        || target_digest != copied_digest
    {
        return Err(private_sidecar_error());
    }
    drop(writer);
    let verified = VerifiedExecutableFile {
        file: reader,
        identity: final_reader_identity,
        digest: copied_digest,
    };
    verified.validate()?;
    private_dir.validate()?;
    Ok((verified, private_dir))
}

fn trusted_app_executable_owner() -> HostResult<u32> {
    #[cfg(target_os = "linux")]
    let executable = File::open("/proc/self/exe").map_err(|_| packaged_owner_error())?;
    #[cfg(target_os = "macos")]
    let executable = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(std::env::current_exe().map_err(|_| packaged_owner_error())?)
        .map_err(|_| packaged_owner_error())?;
    let identity = file_identity(&executable).map_err(|_| packaged_owner_error())?;
    let effective_user = unsafe { libc::geteuid() };
    if identity.mode & FILE_TYPE_MASK != REGULAR_FILE_TYPE {
        return Err(packaged_owner_error());
    }
    validate_app_owner(identity.owner, effective_user)?;
    Ok(identity.owner)
}

fn validate_app_owner(app_owner: u32, effective_user: u32) -> HostResult<()> {
    if app_owner != 0 && app_owner != effective_user {
        return Err(packaged_owner_error());
    }
    Ok(())
}

fn validate_source_owner(source_owner: u32, app_owner: u32, effective_user: u32) -> HostResult<()> {
    validate_app_owner(app_owner, effective_user)?;
    if source_owner != app_owner {
        return Err(packaged_owner_error());
    }
    Ok(())
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
                current = next;
            }
            _ => return Err(packaged_path_error()),
        }
    }
    Ok(current)
}

fn validate_trusted_directory(identity: &FileIdentity) -> HostResult<()> {
    validate_trusted_directory_for_user(identity, unsafe { libc::geteuid() })
}

fn validate_trusted_directory_for_user(
    identity: &FileIdentity,
    effective_user: u32,
) -> HostResult<()> {
    let is_directory = identity.mode & FILE_TYPE_MASK == DIRECTORY_FILE_TYPE;
    let trusted_owner = identity.owner == 0 || identity.owner == effective_user;
    if !is_directory || !trusted_owner {
        return Err(packaged_path_error());
    }
    Ok(())
}

fn validate_packaged_source_identity(identity: &FileIdentity, app_owner: u32) -> HostResult<()> {
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
    validate_source_owner(identity.owner, app_owner, unsafe { libc::geteuid() })?;
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
        "The packaged OpenEvo Desktop sidecar owner does not match the trusted application owner.",
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

fn local_sidecar_args(port: u16) -> Vec<String> {
    vec![
        "--host".to_string(),
        "127.0.0.1".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--listener-fd".to_string(),
        INHERITED_LISTENER_FD.to_string(),
        "--native-instance-stdin".to_string(),
    ]
}

#[cfg(debug_assertions)]
fn normalized_backend_base_url(value: &str) -> Option<String> {
    let trimmed = value.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
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
    args.extend(local_sidecar_args(port));
    if let Ok(value) = std::env::var("OPENEVO_DESKTOP_BACKEND_BASE_URL") {
        if let Some(url) = normalized_backend_base_url(&value) {
            args.extend(["--backend-base-url".to_string(), url]);
        }
    }
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

fn command_from_launch_spec(
    launch: &SidecarLaunchSpec,
    listener: &TcpListener,
) -> HostResult<PreparedCommand> {
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
    let mut command = Command::new(&launch.program);
    command
        .args(&launch.args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    for name in launch.remove_env {
        command.env_remove(name);
    }
    command.env_remove(NATIVE_EXECUTABLE_FD_ENV);
    if launch.verified_executable.is_some() {
        command.env(
            NATIVE_EXECUTABLE_FD_ENV,
            INHERITED_EXECUTABLE_FD.to_string(),
        );
    }
    if let Some(workdir) = &launch.current_dir {
        command.current_dir(workdir);
    }
    let listener_fd = listener_guard.as_raw_fd();
    unsafe {
        command.pre_exec(move || {
            if libc::setpgid(0, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::dup2(listener_fd, INHERITED_LISTENER_FD) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            clear_close_on_exec(INHERITED_LISTENER_FD)?;
            if let (Some(source_fd), Some(expected)) = (executable_fd, expected_identity.as_ref()) {
                if libc::dup2(source_fd, INHERITED_EXECUTABLE_FD) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                clear_close_on_exec(INHERITED_EXECUTABLE_FD)?;
                let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
                if libc::fstat(INHERITED_EXECUTABLE_FD, stat.as_mut_ptr()) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                let actual = file_identity_from_stat(&stat.assume_init());
                if &actual != expected {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::PermissionDenied,
                        "verified executable identity changed before exec",
                    ));
                }
            }
            Ok(())
        });
    }
    Ok(PreparedCommand {
        command,
        _listener_guard: listener_guard,
        _executable_guard: executable_guard,
    })
}

unsafe fn clear_close_on_exec(fd: RawFd) -> std::io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } == -1 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn check_sidecar_health(port: u16, credential: &NativeInstanceCredential) -> HostResult<()> {
    let mut challenge = [0_u8; READINESS_KEY_BYTES];
    OsRng
        .try_fill_bytes(&mut challenge)
        .map_err(|_| sidecar_health_error())?;
    check_sidecar_health_with_challenge(port, credential, &encode_hex(&challenge))
}

fn check_sidecar_health_with_challenge(
    port: u16,
    credential: &NativeInstanceCredential,
    challenge: &str,
) -> HostResult<()> {
    if challenge.len() != 64 || !challenge.bytes().all(is_lower_hex) {
        return Err(sidecar_health_error());
    }
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, SIDECAR_HEALTH_CONNECT_TIMEOUT)
        .map_err(|_| sidecar_health_error())?;
    stream
        .set_read_timeout(Some(SIDECAR_HEALTH_CONNECT_TIMEOUT))
        .map_err(|_| sidecar_health_error())?;
    stream
        .set_write_timeout(Some(SIDECAR_HEALTH_CONNECT_TIMEOUT))
        .map_err(|_| sidecar_health_error())?;
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-OpenEvo-Native-Challenge: {challenge}\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|_| sidecar_health_error())?;
    let mut response = Vec::new();
    stream
        .take((SIDECAR_HEALTH_RESPONSE_MAX_BYTES + 1) as u64)
        .read_to_end(&mut response)
        .map_err(|_| sidecar_health_error())?;
    if response.len() > SIDECAR_HEALTH_RESPONSE_MAX_BYTES {
        return Err(sidecar_health_error());
    }
    let response = std::str::from_utf8(&response).map_err(|_| sidecar_health_error())?;
    let (headers, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(sidecar_health_error)?;
    let status_line = headers.lines().next().ok_or_else(sidecar_health_error)?;
    if status_line != "HTTP/1.1 200 OK" && status_line != "HTTP/1.0 200 OK" {
        return Err(sidecar_health_error());
    }
    let health: NativeHealthResponse =
        serde_json::from_str(body).map_err(|_| sidecar_health_error())?;
    let instance_id = credential.instance_id_hex();
    if health.service != "openevo-sidecar"
        || health.status != "ok"
        || health.protocol != NATIVE_SIDECAR_PROTOCOL
        || health.instance_id != instance_id
    {
        return Err(sidecar_health_error());
    }
    let proof = decode_hex_32(&health.instance_proof).ok_or_else(sidecar_health_error)?;
    let mut mac = HmacSha256::new_from_slice(&credential.readiness_key)
        .map_err(|_| sidecar_health_error())?;
    mac.update(readiness_hmac_domain(&instance_id, challenge).as_bytes());
    mac.verify_slice(&proof).map_err(|_| sidecar_health_error())
}

fn readiness_hmac_domain(instance_id: &str, challenge: &str) -> String {
    format!("{NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{challenge}")
}

fn wait_for_sidecar_ready(
    child: &mut Child,
    port: u16,
    credential: &NativeInstanceCredential,
    timeout: Duration,
    is_cancelled: impl Fn() -> bool,
) -> HostResult<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if is_cancelled() {
            return Err(NativeHostError::new(
                "sidecar_start_cancelled",
                "OpenEvo Desktop cancelled sidecar startup.",
            ));
        }
        if child
            .try_wait()
            .map_err(|_| sidecar_inspection_error())?
            .is_some()
        {
            return Err(NativeHostError::new(
                "sidecar_exited_during_startup",
                "The OpenEvo Desktop sidecar exited before it became ready.",
            ));
        }
        if check_sidecar_health(port, credential).is_ok() {
            return Ok(());
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

#[cfg(test)]
fn terminate_process_group(
    child: &mut Child,
    process_group: i32,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<Option<ExitStatus>> {
    terminate_process_group_with(
        &OsProcessControl,
        child,
        process_group,
        term_timeout,
        kill_timeout,
    )
}

fn terminate_process_group_with<C: ProcessControl>(
    control: &C,
    child: &mut Child,
    process_group: i32,
    term_timeout: Duration,
    kill_timeout: Duration,
) -> HostResult<Option<ExitStatus>> {
    if process_group <= 0 {
        return Err(sidecar_stop_error());
    }
    let mut control_failed = false;
    let mut exit_status = match control.try_wait(child) {
        Ok(status) => status,
        Err(_) => {
            control_failed = true;
            None
        }
    };
    if control.signal_group(process_group, libc::SIGTERM).is_err() {
        control_failed = true;
    }
    let terminated = match wait_for_process_group_exit_with(
        control,
        child,
        process_group,
        &mut exit_status,
        term_timeout,
    ) {
        Ok(exited) => exited,
        Err(_) => {
            control_failed = true;
            false
        }
    };
    if terminated && !control_failed {
        return Ok(exit_status);
    }
    if control.signal_group(process_group, libc::SIGKILL).is_err() {
        control_failed = true;
    }
    let killed = match wait_for_process_group_exit_with(
        control,
        child,
        process_group,
        &mut exit_status,
        kill_timeout,
    ) {
        Ok(exited) => exited,
        Err(_) => {
            control_failed = true;
            false
        }
    };
    if killed && !control_failed {
        return Ok(exit_status);
    }
    Err(sidecar_stop_error())
}

fn wait_for_process_group_exit_with<C: ProcessControl>(
    control: &C,
    child: &mut Child,
    process_group: i32,
    exit_status: &mut Option<ExitStatus>,
    timeout: Duration,
) -> std::io::Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        if exit_status.is_none() {
            *exit_status = control.try_wait(child)?;
        }
        if exit_status.is_some() && !control.group_exists(process_group)? {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        control.sleep(SIDECAR_STOP_POLL_INTERVAL);
    }
}

#[cfg(test)]
fn process_group_exists(process_group: i32) -> HostResult<bool> {
    OsProcessControl
        .group_exists(process_group)
        .map_err(|_| sidecar_inspection_error())
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
    match terminate_process_group_with(
        control,
        &mut managed.child,
        managed.process_group,
        term_timeout,
        kill_timeout,
    ) {
        Ok(_) => Ok(()),
        Err(error) => {
            managed.mark_cleanup_pending();
            Err(error)
        }
    }
}

fn remove_cleaned_sidecar(state: &DesktopHostState, sidecar: &mut Option<ManagedSidecar>) {
    if let Some(managed) = sidecar.take() {
        state
            .emergency_process_group
            .compare_exchange(
                managed.process_group,
                0,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .ok();
        drop(managed);
    }
}

fn fail_startup_and_cleanup(
    state: &DesktopHostState,
    sidecar: &mut Option<ManagedSidecar>,
    startup_error: NativeHostError,
) -> NativeHostError {
    let Some(managed) = sidecar.as_mut() else {
        return startup_error;
    };
    match cleanup_managed_sidecar(managed) {
        Ok(()) => {
            remove_cleaned_sidecar(state, sidecar);
            startup_error
        }
        Err(error) => error,
    }
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
    emergency_term_grace: Duration,
) {
    state.shutdown_requested.store(true, Ordering::Release);
    state.cancellation_epoch.fetch_add(1, Ordering::AcqRel);
    let process_group = state.emergency_process_group.load(Ordering::Acquire);
    if process_group > 0 {
        let _ = control.signal_group(process_group, libc::SIGTERM);
        control.sleep(emergency_term_grace);
        let _ = control.signal_group(process_group, libc::SIGKILL);
    }
    let Ok(mut sidecar) = lock_sidecar_bounded(state, lock_timeout) else {
        return;
    };
    let Some(managed) = sidecar.as_mut() else {
        return;
    };
    if cleanup_managed_sidecar_with(control, managed).is_ok() {
        remove_cleaned_sidecar(state, &mut sidecar);
    }
}

fn lock_sidecar_bounded(
    state: &DesktopHostState,
    timeout: Duration,
) -> HostResult<MutexGuard<'_, Option<ManagedSidecar>>> {
    let deadline = Instant::now() + timeout;
    loop {
        match state.sidecar.try_lock() {
            Ok(sidecar) => return Ok(sidecar),
            Err(TryLockError::Poisoned(_)) => return Err(sidecar_state_error()),
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
    state.shutdown_requested.load(Ordering::Acquire)
        || state.cancellation_epoch.load(Ordering::Acquire) != initial_epoch
}

fn host_status_inner(state: &DesktopHostState) -> HostResult<SidecarStatus> {
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let Some(managed) = sidecar.as_mut() else {
        return Ok(stopped_sidecar_status());
    };
    if managed.lifecycle == ManagedLifecycle::CleanupPending {
        return Ok(managed.status.clone());
    }
    match managed.child.try_wait() {
        Ok(Some(_)) => {
            managed.status.state = "exited".to_string();
            managed.status.url = None;
            if cleanup_managed_sidecar(managed).is_ok() {
                let status = managed.status.clone();
                remove_cleaned_sidecar(state, &mut sidecar);
                Ok(status)
            } else {
                Ok(managed.status.clone())
            }
        }
        Ok(None) => Ok(managed.status.clone()),
        Err(_) => Err(sidecar_inspection_error()),
    }
}

fn start_sidecar_inner(
    state: &DesktopHostState,
    policy: LaunchPolicy,
    bundled_path: Option<&Path>,
) -> HostResult<SidecarStatus> {
    if state.shutdown_requested.load(Ordering::Acquire) {
        return Err(NativeHostError::new(
            "sidecar_host_shutting_down",
            "OpenEvo Desktop is shutting down its native sidecar host.",
        ));
    }
    let startup_epoch = state.cancellation_epoch.load(Ordering::Acquire);
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    if let Some(managed) = sidecar.as_mut() {
        if managed.lifecycle == ManagedLifecycle::CleanupPending {
            return Err(sidecar_stop_error());
        }
        match managed.child.try_wait() {
            Ok(None) => return Ok(managed.status.clone()),
            Ok(Some(_)) => {
                if cleanup_managed_sidecar(managed).is_err() {
                    return Err(sidecar_stop_error());
                }
                remove_cleaned_sidecar(state, &mut sidecar);
            }
            Err(_) => return Err(sidecar_inspection_error()),
        }
    }

    let allocated = allocate_sidecar_listener()?;
    let mut launch = sidecar_launch_spec(policy, bundled_path, allocated.port)?;
    if startup_cancelled(state, startup_epoch) {
        return Err(NativeHostError::new(
            "sidecar_start_cancelled",
            "OpenEvo Desktop cancelled sidecar startup.",
        ));
    }
    if let Some(executable) = launch.verified_executable.as_ref() {
        executable.validate()?;
    }
    let credential = NativeInstanceCredential::generate()?;
    let mut prepared = command_from_launch_spec(&launch, &allocated.listener)?;
    let child = prepared.spawn().map_err(|_| {
        NativeHostError::new(
            "sidecar_spawn_failed",
            "OpenEvo Desktop could not start its local sidecar.",
        )
    })?;
    let process_group = i32::try_from(child.id()).expect("OS child pid fits in pid_t");
    state
        .emergency_process_group
        .store(process_group, Ordering::Release);
    let status = SidecarStatus {
        state: "starting".to_string(),
        port: Some(allocated.port),
        pid: Some(child.id()),
        url: None,
    };
    *sidecar = Some(ManagedSidecar {
        status,
        lifecycle: ManagedLifecycle::Starting,
        child,
        process_group,
        _private_launch_dir: launch.private_launch_dir.take(),
        verified_executable: launch.verified_executable.take(),
        _listener: allocated.listener,
    });
    let managed = sidecar.as_mut().expect("manager owns spawned sidecar");
    if let Some(executable) = managed.verified_executable.as_ref() {
        if let Err(error) = executable.validate() {
            return Err(fail_startup_and_cleanup(state, &mut sidecar, error));
        }
    }
    if let Err(error) = credential.write_to_child(&mut managed.child) {
        return Err(fail_startup_and_cleanup(state, &mut sidecar, error));
    }
    if let Err(error) = wait_for_sidecar_ready(
        &mut managed.child,
        allocated.port,
        &credential,
        SIDECAR_STARTUP_TIMEOUT,
        || startup_cancelled(state, startup_epoch),
    ) {
        return Err(fail_startup_and_cleanup(state, &mut sidecar, error));
    }
    managed.lifecycle = ManagedLifecycle::Running;
    managed.status.state = "running".to_string();
    managed.status.url = Some(format!("http://127.0.0.1:{}/openevo", allocated.port));
    Ok(managed.status.clone())
}

fn stop_sidecar_inner(state: &DesktopHostState) -> HostResult<SidecarStatus> {
    state.cancellation_epoch.fetch_add(1, Ordering::AcqRel);
    let mut sidecar = lock_sidecar_bounded(state, SIDECAR_STATE_LOCK_TIMEOUT)?;
    let Some(managed) = sidecar.as_mut() else {
        return Ok(stopped_sidecar_status());
    };
    cleanup_managed_sidecar(managed)?;
    remove_cleaned_sidecar(state, &mut sidecar);
    Ok(stopped_sidecar_status())
}

#[tauri::command]
fn host_status(state: tauri::State<'_, DesktopHostState>) -> HostResult<SidecarStatus> {
    host_status_inner(&state)
}

#[tauri::command]
fn start_sidecar(
    _app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
) -> HostResult<SidecarStatus> {
    let bundled_path = bundled_sidecar_path();
    start_sidecar_inner(&state, active_launch_policy(), bundled_path.as_deref())
}

#[tauri::command]
fn stop_sidecar(state: tauri::State<'_, DesktopHostState>) -> HostResult<SidecarStatus> {
    stop_sidecar_inner(&state)
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
        .manage(DesktopHostState::default())
        .invoke_handler(tauri::generate_handler![
            host_status,
            start_sidecar,
            stop_sidecar
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
    use std::fs::{self, hard_link, OpenOptions};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::os::unix::process::{CommandExt, ExitStatusExt};
    use std::path::{Path, PathBuf};
    use std::process::{Child, Command, ExitStatus, Stdio};
    use std::sync::Mutex;
    use std::thread;
    use std::time::{Duration, Instant};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn allocated_listener_keeps_the_selected_port_reserved() {
        let allocated = allocate_sidecar_listener().unwrap();

        let competing_bind = TcpListener::bind(("127.0.0.1", allocated.port));

        assert!(competing_bind.is_err());
    }

    #[test]
    fn sidecar_status_has_no_command_path_credential_or_log_surface() {
        let status = serde_json::to_value(stopped_sidecar_status()).unwrap();

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
    fn release_policy_executes_only_a_verified_private_copy() {
        let _guard = ENV_LOCK.lock().unwrap();
        clear_sidecar_env();
        let fixture = SidecarFixture::executable(b"packaged-sidecar-v1");
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_PROGRAM", "/tmp/untrusted");
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_ARGS_JSON", r#"["--steal"]"#);
        std::env::set_var("OPENEVO_DESKTOP_BACKEND_BASE_URL", "https://secret.invalid");

        let spec = sidecar_launch_spec(LaunchPolicy::Release, Some(fixture.path()), 49152).unwrap();
        clear_sidecar_env();

        assert_eq!(spec.program, fd_execution_path());
        let executable = spec.verified_executable.as_ref().unwrap();
        executable.validate().unwrap();
        assert_eq!(read_verified_file(executable), b"packaged-sidecar-v1");
        assert_eq!(executable.identity.links, 0);
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
        assert_eq!(fs::read_dir(private_root).unwrap().count(), 0);
        assert_eq!(
            spec.args,
            vec![
                "--host",
                "127.0.0.1",
                "--port",
                "49152",
                "--listener-fd",
                "3",
                "--native-instance-stdin",
            ]
        );
        assert!(spec.current_dir.is_none());
        assert_eq!(spec.remove_env, RELEASE_FORBIDDEN_SIDECAR_ENV);
    }

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
    fn packaged_source_owner_must_match_a_root_or_euid_app_owner() {
        assert!(validate_source_owner(501, 501, 501).is_ok());
        assert!(validate_source_owner(0, 0, 501).is_ok());

        assert_eq!(
            validate_source_owner(502, 501, 501).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );
        assert_eq!(
            validate_source_owner(0, 501, 501).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );
        assert_eq!(
            validate_source_owner(502, 502, 501).unwrap_err().code,
            "bundled_sidecar_owner_invalid"
        );
    }

    #[test]
    fn trusted_bundle_components_allow_root_or_euid_and_reject_other_owners() {
        let mut identity = mock_directory_identity(0);
        assert!(validate_trusted_directory_for_user(&identity, 501).is_ok());
        identity.owner = 501;
        identity.mode = DIRECTORY_FILE_TYPE | 0o777;
        assert!(validate_trusted_directory_for_user(&identity, 501).is_ok());
        identity.owner = 502;
        assert_eq!(
            validate_trusted_directory_for_user(&identity, 501)
                .unwrap_err()
                .code,
            "bundled_sidecar_path_untrusted"
        );
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
    fn macos_release_executes_the_inherited_fd_through_devfs() {
        assert_eq!(fd_execution_path(), PathBuf::from("/dev/fd/4"));
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
        std::env::set_var("OPENEVO_DESKTOP_BACKEND_BASE_URL", "http://127.0.0.1:8765/");

        let spec = sidecar_launch_spec(LaunchPolicy::Debug, None, 49156).unwrap();
        clear_sidecar_env();

        assert_eq!(spec.program, PathBuf::from("/tmp/dev-sidecar"));
        assert_eq!(
            spec.args,
            vec![
                "--config",
                "dev profile.json",
                "--host",
                "127.0.0.1",
                "--port",
                "49156",
                "--listener-fd",
                "3",
                "--native-instance-stdin",
                "--backend-base-url",
                "http://127.0.0.1:8765",
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
            assert_eq!(
                *control.signals.lock().unwrap(),
                [libc::SIGTERM, libc::SIGKILL]
            );
            cleanup_managed_sidecar(&mut managed).unwrap();
            drop(managed);
            assert!(!private_root.exists());
        }
    }

    #[test]
    fn cleanup_pending_blocks_restart_and_explicit_stop_retries_cleanup() {
        let state = DesktopHostState::default();
        let (managed, private_root, listener_port) = managed_test_sidecar();
        state
            .emergency_process_group
            .store(managed.process_group, Ordering::Release);
        *state.sidecar.lock().unwrap() = Some(managed);
        let control = ScriptedProcessControl::term_failure();
        {
            let mut sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_mut().unwrap();
            cleanup_managed_sidecar_with_bounds(&control, managed, Duration::ZERO, Duration::ZERO)
                .unwrap_err();
            assert_owned_resources(managed, &private_root, listener_port);
        }

        let error = start_sidecar_inner(&state, LaunchPolicy::Release, None).unwrap_err();
        assert_eq!(error.code, "sidecar_stop_failed_owned");
        assert!(private_root.exists());

        assert_eq!(stop_sidecar_inner(&state).unwrap().state, "stopped");
        assert!(state.sidecar.lock().unwrap().is_none());
        assert_eq!(state.emergency_process_group.load(Ordering::Acquire), 0);
        assert!(!private_root.exists());
    }

    #[test]
    fn exit_kills_via_atomic_process_group_when_the_state_lock_is_busy() {
        let state = DesktopHostState::default();
        let (managed, private_root, _) = managed_test_sidecar();
        let process_group = managed.process_group;
        state
            .emergency_process_group
            .store(process_group, Ordering::Release);
        *state.sidecar.lock().unwrap() = Some(managed);
        let mut guard = state.sidecar.lock().unwrap();
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
            if guard.as_mut().unwrap().child.try_wait().unwrap().is_some() {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "exit hook did not kill the child"
            );
            thread::sleep(Duration::from_millis(10));
        }
        assert!(!process_group_exists(process_group).unwrap());
        drop(guard);
        stop_sidecar_inner(&state).unwrap();
        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!private_root.exists());
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
    #[ignore = "requires a freshly generated PyInstaller externalBin"]
    fn packaged_external_bin_native_launch_smoke() {
        let path = PathBuf::from(
            std::env::var_os("OPENEVO_PACKAGED_SIDECAR_PATH")
                .expect("OPENEVO_PACKAGED_SIDECAR_PATH is required"),
        );
        let state = DesktopHostState::default();

        let status = start_sidecar_inner(&state, LaunchPolicy::Release, Some(&path)).unwrap();
        assert_eq!(status.state, "running");
        let private_root = {
            let sidecar = state.sidecar.lock().unwrap();
            let managed = sidecar.as_ref().unwrap();
            assert_eq!(managed.lifecycle, ManagedLifecycle::Running);
            assert_eq!(managed.status.port, status.port);
            assert_eq!(
                managed._listener.local_addr().unwrap().port(),
                status.port.unwrap()
            );
            let executable = managed.verified_executable.as_ref().unwrap();
            executable.validate().unwrap();
            assert_eq!(executable.identity.links, 0);
            let root = managed._private_launch_dir.as_ref().unwrap().path();
            assert_eq!(fs::read_dir(root).unwrap().count(), 0);
            root.to_path_buf()
        };

        stop_sidecar_inner(&state).unwrap();

        assert!(state.sidecar.lock().unwrap().is_none());
        assert!(!private_root.exists());
        assert!(path.exists());
    }

    fn serve_health(
        listener: TcpListener,
        instance_id: [u8; INSTANCE_ID_BYTES],
        readiness_key: [u8; READINESS_KEY_BYTES],
        proof_challenge: Option<String>,
        include_unknown_field: bool,
    ) {
        let (mut stream, _) = listener.accept().unwrap();
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
        let request = std::str::from_utf8(&request).unwrap();
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
        let body = serde_json::to_string(&body).unwrap();
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes()).unwrap();
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
                if libc::setpgid(0, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().unwrap()
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
        }
    }

    fn clear_sidecar_env() {
        for name in RELEASE_FORBIDDEN_SIDECAR_ENV {
            std::env::remove_var(name);
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
        fail_term: bool,
        fail_kill: bool,
        group_states: Mutex<VecDeque<bool>>,
        signals: Mutex<Vec<i32>>,
    }

    impl ScriptedProcessControl {
        fn term_failure() -> Self {
            Self {
                fail_wait: false,
                fail_term: true,
                fail_kill: false,
                group_states: Mutex::new(VecDeque::from([false, false])),
                signals: Mutex::new(Vec::new()),
            }
        }

        fn kill_failure() -> Self {
            Self {
                fail_wait: false,
                fail_term: false,
                fail_kill: true,
                group_states: Mutex::new(VecDeque::from([true, false])),
                signals: Mutex::new(Vec::new()),
            }
        }

        fn wait_failure() -> Self {
            Self {
                fail_wait: true,
                fail_term: false,
                fail_kill: false,
                group_states: Mutex::new(VecDeque::new()),
                signals: Mutex::new(Vec::new()),
            }
        }
    }

    impl ProcessControl for ScriptedProcessControl {
        fn try_wait(&self, _child: &mut Child) -> std::io::Result<Option<ExitStatus>> {
            if self.fail_wait {
                Err(std::io::Error::other("injected wait failure"))
            } else {
                Ok(Some(ExitStatus::from_raw(0)))
            }
        }

        fn signal_group(&self, _process_group: i32, signal: libc::c_int) -> std::io::Result<()> {
            self.signals.lock().unwrap().push(signal);
            if (signal == libc::SIGTERM && self.fail_term)
                || (signal == libc::SIGKILL && self.fail_kill)
            {
                Err(std::io::Error::other("injected signal failure"))
            } else {
                Ok(())
            }
        }

        fn group_exists(&self, _process_group: i32) -> std::io::Result<bool> {
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
        (
            ManagedSidecar {
                status: SidecarStatus {
                    state: "running".to_string(),
                    port: Some(listener_port),
                    pid: Some(child.id()),
                    url: Some(format!("http://127.0.0.1:{listener_port}/openevo")),
                },
                lifecycle: ManagedLifecycle::Running,
                child,
                process_group,
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
        assert!(managed.child.id() > 0);
    }
}
