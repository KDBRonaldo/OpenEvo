use std::collections::VecDeque;
use std::ffi::CString;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

const LOG_SCHEMA_VERSION: &str = "1";
const LOG_DIRECTORY_NAME: &str = "logs-v1";
const CURRENT_LOG_FILE: &str = "desktop.jsonl";
const LOCK_FILE: &str = ".desktop-log.lock";
const MAX_ROTATED_FILES: usize = 7;
const MAX_LOG_FILE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_MEMORY_EVENTS: usize = 512;
const MAX_TAIL_EVENTS: usize = 200;
const MAX_EVENT_CODE_BYTES: usize = 96;
const FALLBACK_TIMESTAMP: &str = "1970-01-01T00:00:00.000Z";
const LOCK_ACQUIRE_TIMEOUT: Duration = Duration::from_millis(50);
const LOCK_RETRY_INTERVAL: Duration = Duration::from_millis(5);

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopLogEventV1 {
    pub schema_version: String,
    pub sequence: u64,
    pub occurred_at: String,
    pub source: String,
    pub level: String,
    pub event: String,
    pub code: Option<String>,
    pub exit_code: Option<u32>,
    pub signal: Option<u32>,
    pub errno: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DesktopLogTailV1 {
    pub schema_version: String,
    pub availability: String,
    pub entries: Vec<DesktopLogEventV1>,
    pub dropped_count: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesktopLogSource {
    Native,
    Startup,
    Sidecar,
    Renderer,
}

impl DesktopLogSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::Native => "native",
            Self::Startup => "startup",
            Self::Sidecar => "sidecar",
            Self::Renderer => "renderer",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesktopLogLevel {
    Info,
    Warning,
    Error,
}

impl DesktopLogLevel {
    fn as_str(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warning => "warning",
            Self::Error => "error",
        }
    }
}

struct PersistentLogRoot {
    path: PathBuf,
    directory: File,
    device: u64,
    inode: u64,
}

struct DesktopLogState {
    root: Option<PersistentLogRoot>,
    persistent_attempted: bool,
    entries: VecDeque<DesktopLogEventV1>,
    dropped_count: u64,
}

pub struct DesktopLogStore {
    state: Mutex<DesktopLogState>,
    next_sequence: AtomicU64,
}

impl Default for DesktopLogStore {
    fn default() -> Self {
        Self {
            state: Mutex::new(DesktopLogState {
                root: None,
                persistent_attempted: false,
                entries: VecDeque::new(),
                dropped_count: 0,
            }),
            next_sequence: AtomicU64::new(1),
        }
    }
}

impl DesktopLogStore {
    pub fn bind_app_data_root(&self, app_data_root: &Path) -> bool {
        let path = app_data_root.join(LOG_DIRECTORY_NAME);
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.root.as_ref().is_some_and(|root| root.path == path)
            && state
                .root
                .as_ref()
                .is_some_and(|root| validate_root_binding(root).is_ok())
        {
            return true;
        }

        state.root = None;
        state.persistent_attempted = true;
        let root = match prepare_log_root(&path) {
            Ok(root) => root,
            Err(_) => return false,
        };
        let recovery = match acquire_log_lock(&root).and_then(|_lock| recover_events(&root)) {
            Ok(recovery) => recovery,
            Err(_) => return false,
        };
        state.dropped_count = state.dropped_count.saturating_add(recovery.dropped_count);
        for event in recovery.events {
            self.next_sequence
                .fetch_max(event.sequence.saturating_add(1), Ordering::AcqRel);
            push_memory_event(&mut state, event);
        }
        state.root = Some(root);
        true
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record(
        &self,
        source: DesktopLogSource,
        level: DesktopLogLevel,
        event: &'static str,
        code: Option<&str>,
        exit_code: Option<u32>,
        signal: Option<u32>,
        errno: Option<u64>,
    ) {
        if !is_known_event(event) {
            return;
        }
        let entry = DesktopLogEventV1 {
            schema_version: LOG_SCHEMA_VERSION.to_string(),
            sequence: self.next_sequence.fetch_add(1, Ordering::AcqRel),
            occurred_at: current_timestamp(),
            source: source.as_str().to_string(),
            level: level.as_str().to_string(),
            event: event.to_string(),
            code: code.filter(|value| is_safe_code(value)).map(str::to_string),
            exit_code,
            signal,
            errno,
        };
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state
            .root
            .as_ref()
            .is_some_and(|root| append_event(root, &entry).is_err())
        {
            state.root = None;
        }
        push_memory_event(&mut state, entry);
    }

    pub fn tail(&self, limit: Option<usize>) -> DesktopLogTailV1 {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        snapshot(
            &state,
            limit.unwrap_or(MAX_TAIL_EVENTS).clamp(1, MAX_TAIL_EVENTS),
        )
    }

    /// A diagnostics export may retain the complete in-memory buffer, unlike the UI tail.
    pub fn export_snapshot(&self) -> DesktopLogTailV1 {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        snapshot(&state, MAX_MEMORY_EVENTS)
    }

    pub fn persistent_root(&self) -> Option<PathBuf> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state
            .root
            .as_ref()
            .is_some_and(|root| validate_root_binding(root).is_err())
        {
            state.root = None;
        }
        state.root.as_ref().map(|root| root.path.clone())
    }
}

fn snapshot(state: &DesktopLogState, limit: usize) -> DesktopLogTailV1 {
    let start = state.entries.len().saturating_sub(limit);
    DesktopLogTailV1 {
        schema_version: LOG_SCHEMA_VERSION.to_string(),
        availability: if state.root.is_some() {
            "available"
        } else if state.persistent_attempted || !state.entries.is_empty() {
            "memory_only"
        } else {
            "unavailable"
        }
        .to_string(),
        entries: state.entries.iter().skip(start).cloned().collect(),
        dropped_count: state.dropped_count,
    }
}

fn push_memory_event(state: &mut DesktopLogState, event: DesktopLogEventV1) {
    if state.entries.len() == MAX_MEMORY_EVENTS {
        state.entries.pop_front();
        state.dropped_count = state.dropped_count.saturating_add(1);
    }
    state.entries.push_back(event);
}

fn prepare_log_root(path: &Path) -> std::io::Result<PersistentLogRoot> {
    fs::create_dir_all(path)?;
    let directory = open_directory_nofollow(path)?;
    set_mode(&directory, 0o700)?;
    let metadata = directory.metadata()?;
    validate_root_metadata(&metadata)?;
    let root = PersistentLogRoot {
        path: path.to_path_buf(),
        device: metadata.dev(),
        inode: metadata.ino(),
        directory,
    };
    validate_root_binding(&root)?;
    Ok(root)
}

fn open_directory_nofollow(path: &Path) -> std::io::Result<File> {
    let path = c_path(path)?;
    let descriptor = unsafe {
        libc::open(
            path.as_ptr(),
            libc::O_RDONLY
                | libc::O_DIRECTORY
                | libc::O_NOFOLLOW
                | libc::O_NONBLOCK
                | libc::O_CLOEXEC,
        )
    };
    file_from_descriptor(descriptor)
}

fn validate_root_binding(root: &PersistentLogRoot) -> std::io::Result<()> {
    let held = root.directory.metadata()?;
    validate_root_metadata(&held)?;
    if held.dev() != root.device || held.ino() != root.inode {
        return unsafe_error("desktop log root FD binding changed");
    }
    let bound = fs::symlink_metadata(&root.path)?;
    validate_root_metadata(&bound)?;
    if bound.dev() != root.device || bound.ino() != root.inode {
        return unsafe_error("desktop log root path binding changed");
    }
    Ok(())
}

fn validate_root_metadata(metadata: &fs::Metadata) -> std::io::Result<()> {
    if !metadata.file_type().is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return unsafe_error("unsafe desktop log root");
    }
    Ok(())
}

fn append_event(root: &PersistentLogRoot, event: &DesktopLogEventV1) -> std::io::Result<()> {
    validate_root_binding(root)?;
    let _lock = acquire_log_lock(root)?;
    let mut encoded = serde_json::to_vec(event)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    encoded.push(b'\n');
    let current_size = named_log_size(root, CURRENT_LOG_FILE)?;
    if current_size
        .is_some_and(|size| size.saturating_add(encoded.len() as u64) > MAX_LOG_FILE_BYTES)
    {
        rotate_logs(root)?;
    }
    let mut file = open_final_file(
        root,
        CURRENT_LOG_FILE,
        libc::O_WRONLY | libc::O_APPEND,
        true,
    )?;
    let size = file.metadata()?.len();
    if size.saturating_add(encoded.len() as u64) > MAX_LOG_FILE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "desktop log entry exceeds file budget",
        ));
    }
    file.write_all(&encoded)?;
    file.sync_data()?;
    validate_root_binding(root)
}

fn acquire_log_lock(root: &PersistentLogRoot) -> std::io::Result<LogLock> {
    validate_root_binding(root)?;
    let file = open_final_file(root, LOCK_FILE, libc::O_RDWR, true)?;
    let deadline = Instant::now() + LOCK_ACQUIRE_TIMEOUT;
    loop {
        let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if result == 0 {
            validate_root_binding(root)?;
            return Ok(LogLock(file));
        }
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EWOULDBLOCK) {
            return Err(error);
        }
        if Instant::now() >= deadline {
            return Err(std::io::Error::new(
                std::io::ErrorKind::WouldBlock,
                "desktop log lock acquisition timed out",
            ));
        }
        thread::sleep(LOCK_RETRY_INTERVAL);
    }
}

struct LogLock(File);

impl Drop for LogLock {
    fn drop(&mut self) {
        let _ = unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

fn rotate_logs(root: &PersistentLogRoot) -> std::io::Result<()> {
    validate_root_binding(root)?;
    remove_existing_log(root, &rotated_log_name(MAX_ROTATED_FILES))?;
    for index in (1..MAX_ROTATED_FILES).rev() {
        rename_existing_log(root, &rotated_log_name(index), &rotated_log_name(index + 1))?;
    }
    rename_existing_log(root, CURRENT_LOG_FILE, &rotated_log_name(1))?;
    validate_root_binding(root)
}

fn remove_existing_log(root: &PersistentLogRoot, name: &str) -> std::io::Result<()> {
    if !named_log_exists(root, name)? {
        return Ok(());
    }
    let name = c_name(name)?;
    let result = unsafe { libc::unlinkat(root.directory.as_raw_fd(), name.as_ptr(), 0) };
    if result == 0 {
        validate_root_binding(root)
    } else {
        Err(std::io::Error::last_os_error())
    }
}

fn rename_existing_log(
    root: &PersistentLogRoot,
    source: &str,
    target: &str,
) -> std::io::Result<()> {
    if !named_log_exists(root, source)? {
        return Ok(());
    }
    remove_existing_log(root, target)?;
    let source = c_name(source)?;
    let target = c_name(target)?;
    let result = unsafe {
        libc::renameat(
            root.directory.as_raw_fd(),
            source.as_ptr(),
            root.directory.as_raw_fd(),
            target.as_ptr(),
        )
    };
    if result != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let _ = open_final_file(
        root,
        target.to_str().unwrap_or_default(),
        libc::O_RDONLY,
        false,
    )?;
    validate_root_binding(root)
}

fn named_log_size(root: &PersistentLogRoot, name: &str) -> std::io::Result<Option<u64>> {
    match open_final_file(root, name, libc::O_RDONLY, false) {
        Ok(file) => Ok(Some(file.metadata()?.len())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn named_log_exists(root: &PersistentLogRoot, name: &str) -> std::io::Result<bool> {
    Ok(named_log_size(root, name)?.is_some())
}

fn open_final_file(
    root: &PersistentLogRoot,
    name: &str,
    access: libc::c_int,
    create: bool,
) -> std::io::Result<File> {
    validate_root_binding(root)?;
    let name = c_name(name)?;
    let mut flags = access | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC;
    if create {
        flags |= libc::O_CREAT;
    }
    let descriptor =
        unsafe { libc::openat(root.directory.as_raw_fd(), name.as_ptr(), flags, 0o600) };
    let file = file_from_descriptor(descriptor)?;
    validate_log_file(&file.metadata()?)?;
    validate_root_binding(root)?;
    Ok(file)
}

fn recover_events(root: &PersistentLogRoot) -> std::io::Result<Recovery> {
    validate_root_binding(root)?;
    let mut events = Vec::new();
    for index in (1..=MAX_ROTATED_FILES).rev() {
        recover_file(root, &rotated_log_name(index), &mut events, false)?;
    }
    recover_file(root, CURRENT_LOG_FILE, &mut events, true)?;
    events.sort_by_key(|event| event.sequence);
    let dropped_count = events.len().saturating_sub(MAX_MEMORY_EVENTS) as u64;
    if dropped_count > 0 {
        events.drain(..dropped_count as usize);
    }
    validate_root_binding(root)?;
    Ok(Recovery {
        events,
        dropped_count,
    })
}

struct Recovery {
    events: Vec<DesktopLogEventV1>,
    dropped_count: u64,
}

fn recover_file(
    root: &PersistentLogRoot,
    name: &str,
    recovered: &mut Vec<DesktopLogEventV1>,
    allow_partial_tail: bool,
) -> std::io::Result<()> {
    let mut file = match open_final_file(root, name, libc::O_RDONLY, false) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    let metadata = file.metadata()?;
    if metadata.len() > MAX_LOG_FILE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "desktop log file exceeds recovery budget",
        ));
    }
    let mut payload = Vec::with_capacity(metadata.len() as usize);
    file.read_to_end(&mut payload)?;
    if payload.len() as u64 != metadata.len() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "desktop log file changed during recovery",
        ));
    }
    let complete_end = if allow_partial_tail && !payload.ends_with(b"\n") {
        payload
            .iter()
            .rposition(|byte| *byte == b'\n')
            .map_or(0, |index| index + 1)
    } else {
        payload.len()
    };
    for line in payload[..complete_end].split(|byte| *byte == b'\n') {
        if line.is_empty() {
            continue;
        }
        if line.len() > 1024 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "desktop log line exceeds recovery budget",
            ));
        }
        let event: DesktopLogEventV1 = serde_json::from_slice(line)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        if !valid_recovered_event(&event) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "desktop log event is invalid",
            ));
        }
        recovered.push(event);
    }
    validate_root_binding(root)
}

fn validate_log_file(metadata: &fs::Metadata) -> std::io::Result<()> {
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
        || metadata.permissions().mode() & 0o777 != 0o600
    {
        return unsafe_error("unsafe desktop log file");
    }
    Ok(())
}

fn set_mode(file: &File, mode: libc::mode_t) -> std::io::Result<()> {
    if unsafe { libc::fchmod(file.as_raw_fd(), mode) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

fn c_path(path: &Path) -> std::io::Result<CString> {
    CString::new(path.as_os_str().as_encoded_bytes())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "path contains NUL"))
}

fn c_name(name: &str) -> std::io::Result<CString> {
    if name.is_empty() || name.contains('/') {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid desktop log file name",
        ));
    }
    CString::new(name)
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "name contains NUL"))
}

fn file_from_descriptor(descriptor: libc::c_int) -> std::io::Result<File> {
    if descriptor < 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(descriptor) })
    }
}

fn unsafe_error(message: &'static str) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::PermissionDenied,
        message,
    ))
}

fn rotated_log_name(index: usize) -> String {
    format!("desktop.{index}.jsonl")
}

fn valid_recovered_event(event: &DesktopLogEventV1) -> bool {
    event.schema_version == LOG_SCHEMA_VERSION
        && event.sequence > 0
        && is_valid_timestamp(&event.occurred_at)
        && matches!(
            event.source.as_str(),
            "native" | "startup" | "sidecar" | "renderer"
        )
        && matches!(event.level.as_str(), "info" | "warning" | "error")
        && is_known_event(&event.event)
        && event.code.as_deref().is_none_or(is_safe_code)
}

fn is_valid_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 24
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'.'
        || bytes[23] != b'Z'
        || bytes.iter().enumerate().any(|(index, byte)| {
            !matches!(index, 4 | 7 | 10 | 13 | 16 | 19 | 23) && !byte.is_ascii_digit()
        })
    {
        return false;
    }
    let number = |start: usize, end: usize| -> Option<u32> {
        std::str::from_utf8(&bytes[start..end]).ok()?.parse().ok()
    };
    matches!(number(5, 7), Some(1..=12))
        && matches!(number(8, 10), Some(1..=31))
        && matches!(number(11, 13), Some(0..=23))
        && matches!(number(14, 16), Some(0..=59))
        && matches!(number(17, 19), Some(0..=59))
        && number(20, 23).is_some()
}

fn is_known_event(event: &str) -> bool {
    matches!(
        event,
        "application_started"
            | "app_translocation_detected"
            | "sidecar_start_requested"
            | "sidecar_start_succeeded"
            | "sidecar_start_failed"
            | "sidecar_startup_diagnostic"
            | "sidecar_exited_before_ready"
            | "sidecar_pre_python_exit"
            | "sidecar_unstructured_output_discarded"
            | "sidecar_stop_requested"
            | "sidecar_stop_succeeded"
            | "sidecar_stop_failed"
            | "sidecar_runtime_exited"
            | "renderer_stage"
            | "log_directory_revealed"
            | "diagnostics_exported"
    )
}

fn is_safe_code(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_EVENT_CODE_BYTES
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'/' | b'-')
        })
}

fn current_timestamp() -> String {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let seconds = duration.as_secs().min(i64::MAX as u64) as libc::time_t;
    let mut value = std::mem::MaybeUninit::<libc::tm>::uninit();
    let converted = unsafe { libc::gmtime_r(&seconds, value.as_mut_ptr()) };
    if converted.is_null() {
        return FALLBACK_TIMESTAMP.to_string();
    }
    let value = unsafe { value.assume_init() };
    let timestamp = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        value.tm_year + 1900,
        value.tm_mon + 1,
        value.tm_mday,
        value.tm_hour,
        value.tm_min,
        value.tm_sec,
        duration.subsec_millis()
    );
    if is_valid_timestamp(&timestamp) {
        timestamp
    } else {
        FALLBACK_TIMESTAMP.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record_started(store: &DesktopLogStore) {
        store.record(
            DesktopLogSource::Native,
            DesktopLogLevel::Info,
            "application_started",
            Some("v0_1_8"),
            None,
            None,
            None,
        );
    }

    #[test]
    fn memory_tail_is_bounded_and_closed() {
        let store = DesktopLogStore::default();
        store.record(
            DesktopLogSource::Startup,
            DesktopLogLevel::Error,
            "sidecar_pre_python_exit",
            Some("code_255"),
            Some(255),
            None,
            None,
        );
        let tail = store.tail(Some(200));
        assert_eq!(tail.availability, "memory_only");
        assert_eq!(tail.entries.len(), 1);
        assert_eq!(tail.entries[0].exit_code, Some(255));
    }

    #[test]
    fn timestamp_contract_has_an_iso_fallback_shared_with_the_renderer() {
        assert!(is_valid_timestamp(FALLBACK_TIMESTAMP));
        assert!(is_valid_timestamp("2026-07-22T14:55:01.123Z"));
        assert!(!is_valid_timestamp("unix-ms:123"));
        assert!(!is_valid_timestamp("2026-13-22T14:55:01.123Z"));
    }

    #[test]
    fn export_snapshot_retains_more_than_ui_tail() {
        let store = DesktopLogStore::default();
        for _ in 0..MAX_TAIL_EVENTS + 1 {
            record_started(&store);
        }
        assert_eq!(store.tail(None).entries.len(), MAX_TAIL_EVENTS);
        assert_eq!(store.export_snapshot().entries.len(), MAX_TAIL_EVENTS + 1);
    }

    #[test]
    fn persistent_store_recovers_previous_events() {
        let root = tempfile::tempdir().unwrap();
        let first = DesktopLogStore::default();
        assert!(first.bind_app_data_root(root.path()));
        record_started(&first);
        let second = DesktopLogStore::default();
        assert!(second.bind_app_data_root(root.path()));
        assert_eq!(second.tail(None).entries.len(), 1);
    }

    #[test]
    fn recovery_counts_events_discarded_beyond_memory_capacity() {
        let root = tempfile::tempdir().unwrap();
        let first = DesktopLogStore::default();
        assert!(first.bind_app_data_root(root.path()));
        for _ in 0..=MAX_MEMORY_EVENTS {
            record_started(&first);
        }
        let second = DesktopLogStore::default();
        assert!(second.bind_app_data_root(root.path()));
        let snapshot = second.export_snapshot();
        assert_eq!(snapshot.entries.len(), MAX_MEMORY_EVENTS);
        assert_eq!(snapshot.dropped_count, 1);
    }

    #[test]
    fn recovery_ignores_only_an_incomplete_current_tail() {
        let root = tempfile::tempdir().unwrap();
        let first = DesktopLogStore::default();
        assert!(first.bind_app_data_root(root.path()));
        record_started(&first);
        let log_path = root.path().join(LOG_DIRECTORY_NAME).join(CURRENT_LOG_FILE);
        let mut file = fs::OpenOptions::new().append(true).open(log_path).unwrap();
        file.write_all(b"{\"schema_version\":\"1\"").unwrap();

        let second = DesktopLogStore::default();
        assert!(second.bind_app_data_root(root.path()));
        let tail = second.tail(None);
        assert_eq!(tail.entries.len(), 1);
        assert_eq!(tail.entries[0].event, "application_started");
    }

    #[test]
    fn root_replacement_disables_persistence_without_writing_to_replacement() {
        let app_data = tempfile::tempdir().unwrap();
        let store = DesktopLogStore::default();
        assert!(store.bind_app_data_root(app_data.path()));
        let logs = app_data.path().join(LOG_DIRECTORY_NAME);
        let old_logs = app_data.path().join("old-logs");
        fs::rename(&logs, &old_logs).unwrap();
        fs::create_dir(&logs).unwrap();
        fs::set_permissions(&logs, fs::Permissions::from_mode(0o700)).unwrap();

        record_started(&store);
        assert_eq!(store.tail(None).availability, "memory_only");
        assert!(store.persistent_root().is_none());
        assert!(!logs.join(CURRENT_LOG_FILE).exists());
    }

    #[test]
    fn unsafe_log_root_falls_back_to_memory() {
        let root = tempfile::tempdir().unwrap();
        let logs = root.path().join(LOG_DIRECTORY_NAME);
        std::os::unix::fs::symlink(root.path(), &logs).unwrap();
        let store = DesktopLogStore::default();
        assert!(!store.bind_app_data_root(root.path()));
        store.record(
            DesktopLogSource::Native,
            DesktopLogLevel::Warning,
            "sidecar_start_failed",
            Some("log_store_unavailable"),
            None,
            None,
            None,
        );
        assert_eq!(store.tail(None).availability, "memory_only");
    }
}
