use std::collections::{HashMap, VecDeque};
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

use rand::rngs::OsRng;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const LOG_SCHEMA_VERSION_V1: &str = "1";
const LOG_SCHEMA_VERSION_V2: &str = "2";
const LOG_DIRECTORY_NAME: &str = "logs-v1";
const CURRENT_LOG_FILE: &str = "desktop.jsonl";
const LOCK_FILE: &str = ".desktop-log.lock";
const MAX_ROTATED_FILES: usize = 7;
const MAX_LOG_FILE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_MEMORY_EVENTS: usize = 512;
const MAX_TAIL_EVENTS: usize = 200;
const MAX_ATTEMPT_SUMMARIES: usize = 128;
const MAX_EVENT_CODE_BYTES: usize = 96;
const MAX_LOG_LINE_BYTES: usize = 2048;
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

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopEnvironmentSummaryV2 {
    pub schema_version: String,
    pub os_family: String,
    pub os_version: String,
    pub os_build: String,
    pub architecture: String,
    pub app_location: String,
    pub quarantine: String,
    pub translocation: String,
}

impl Default for DesktopEnvironmentSummaryV2 {
    fn default() -> Self {
        Self {
            schema_version: LOG_SCHEMA_VERSION_V2.to_string(),
            os_family: "unknown".to_string(),
            os_version: "unknown".to_string(),
            os_build: "unknown".to_string(),
            architecture: "unknown".to_string(),
            app_location: "unknown".to_string(),
            quarantine: "unknown".to_string(),
            translocation: "unknown".to_string(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopDiagnosticEventV2 {
    pub schema_version: String,
    pub sequence: u64,
    pub occurred_at: String,
    pub attempt_id: Option<String>,
    pub attempt_ordinal: Option<u64>,
    pub attempt_sequence: Option<u64>,
    pub component: String,
    pub level: String,
    pub event: String,
    pub stage: Option<String>,
    pub result: Option<String>,
    pub code: Option<String>,
    pub duration_bucket: Option<String>,
    pub product_version: String,
    pub source_commit: Option<String>,
    pub exit_code: Option<u32>,
    pub signal: Option<u32>,
    pub errno: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DesktopStartupAttemptSummaryV2 {
    pub schema_version: String,
    pub attempt_id: String,
    pub ordinal: u64,
    pub event_count: u64,
    pub last_completed_stage: Option<String>,
    pub first_failed_stage: Option<String>,
    pub duration_bucket: String,
    pub outcome: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DesktopDiagnosticLogV2 {
    pub schema_version: String,
    pub availability: String,
    pub environment: DesktopEnvironmentSummaryV2,
    pub attempts: Vec<DesktopStartupAttemptSummaryV2>,
    pub dropped_attempt_count: u64,
    pub entries: Vec<DesktopDiagnosticEventV2>,
    pub dropped_count: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesktopStartupStage {
    NativeApplication,
    BundleVerification,
    SidecarSpawn,
    DescriptorHandoff,
    Bootloader,
    EmbeddedPython,
    SidecarEntry,
    StateStore,
    LocalApi,
    RendererBootstrap,
    RendererReady,
}

impl DesktopStartupStage {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NativeApplication => "native_application",
            Self::BundleVerification => "bundle_verification",
            Self::SidecarSpawn => "sidecar_spawn",
            Self::DescriptorHandoff => "descriptor_handoff",
            Self::Bootloader => "bootloader",
            Self::EmbeddedPython => "embedded_python",
            Self::SidecarEntry => "sidecar_entry",
            Self::StateStore => "state_store",
            Self::LocalApi => "local_api",
            Self::RendererBootstrap => "renderer_bootstrap",
            Self::RendererReady => "renderer_ready",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DesktopStartupResult {
    Started,
    Completed,
    Failed,
}

impl DesktopStartupResult {
    fn as_str(self) -> &'static str {
        match self {
            Self::Started => "started",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }
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
    entries: VecDeque<DesktopDiagnosticEventV2>,
    dropped_count: u64,
    environment: DesktopEnvironmentSummaryV2,
    source_commit: Option<String>,
    current_attempt: Option<CurrentStartupAttempt>,
}

struct CurrentStartupAttempt {
    id: String,
    ordinal: u64,
    next_sequence: u64,
    started: Instant,
    terminal: bool,
    failed: bool,
}

pub struct DesktopLogStore {
    state: Mutex<DesktopLogState>,
    next_sequence: AtomicU64,
    next_attempt_ordinal: AtomicU64,
}

impl Default for DesktopLogStore {
    fn default() -> Self {
        Self {
            state: Mutex::new(DesktopLogState {
                root: None,
                persistent_attempted: false,
                entries: VecDeque::new(),
                dropped_count: 0,
                environment: DesktopEnvironmentSummaryV2::default(),
                source_commit: None,
                current_attempt: None,
            }),
            next_sequence: AtomicU64::new(1),
            next_attempt_ordinal: AtomicU64::new(1),
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
            if let Some(ordinal) = event.attempt_ordinal {
                self.next_attempt_ordinal
                    .fetch_max(ordinal.saturating_add(1), Ordering::AcqRel);
            }
            push_memory_event(&mut state, event);
        }
        state.root = Some(root);
        true
    }

    pub fn set_environment(&self, environment: DesktopEnvironmentSummaryV2) -> bool {
        if !valid_environment(&environment) {
            return false;
        }
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .environment = environment;
        true
    }

    pub fn update_release_identity(&self, build_version: &str, source_commit: &str) -> bool {
        if build_version != env!("CARGO_PKG_VERSION") || !is_source_commit(source_commit) {
            return false;
        }
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state
            .source_commit
            .as_deref()
            .is_some_and(|current| current != source_commit)
        {
            return false;
        }
        state.source_commit = Some(source_commit.to_string());
        true
    }

    pub fn begin_startup_attempt(&self) -> String {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        self.begin_startup_attempt_locked(&mut state)
    }

    pub fn ensure_startup_attempt(&self) -> String {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(current) = state
            .current_attempt
            .as_ref()
            .filter(|attempt| !attempt.terminal)
            .map(|attempt| attempt.id.clone())
        {
            return current;
        }
        self.begin_startup_attempt_locked(&mut state)
    }

    pub fn current_attempt_failed(&self) -> bool {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state
            .current_attempt
            .as_ref()
            .is_some_and(|attempt| attempt.failed)
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
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let entry = new_event(
            &mut state,
            self.next_sequence.fetch_add(1, Ordering::AcqRel),
            source,
            level,
            event,
            None,
            None,
            code,
            None,
            exit_code,
            signal,
            errno,
        );
        persist_and_push(&mut state, entry);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record_startup_stage(
        &self,
        source: DesktopLogSource,
        stage: DesktopStartupStage,
        result: DesktopStartupResult,
        code: Option<&str>,
        exit_code: Option<u32>,
        signal: Option<u32>,
        errno: Option<u64>,
    ) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.current_attempt.is_none() {
            self.begin_startup_attempt_locked(&mut state);
        }
        let duration = state
            .current_attempt
            .as_ref()
            .map(|attempt| duration_bucket(attempt.started.elapsed()));
        let entry = new_event(
            &mut state,
            self.next_sequence.fetch_add(1, Ordering::AcqRel),
            source,
            if result == DesktopStartupResult::Failed {
                DesktopLogLevel::Error
            } else {
                DesktopLogLevel::Info
            },
            "startup_stage",
            Some(stage.as_str()),
            Some(result.as_str()),
            code,
            duration,
            exit_code,
            signal,
            errno,
        );
        if let Some(attempt) = state.current_attempt.as_mut() {
            if result == DesktopStartupResult::Failed {
                attempt.failed = true;
                attempt.terminal = true;
            } else if stage == DesktopStartupStage::RendererReady
                && result == DesktopStartupResult::Completed
            {
                attempt.terminal = true;
            }
        }
        persist_and_push(&mut state, entry);
    }

    pub fn tail(&self, limit: Option<usize>) -> DesktopLogTailV1 {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        snapshot_v1(
            &state,
            limit.unwrap_or(MAX_TAIL_EVENTS).clamp(1, MAX_TAIL_EVENTS),
        )
    }

    /// A diagnostics export may retain the complete in-memory buffer, unlike the UI tail.
    pub fn export_snapshot(&self) -> DesktopDiagnosticLogV2 {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        snapshot_v2(&state, MAX_MEMORY_EVENTS)
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

    fn begin_startup_attempt_locked(&self, state: &mut DesktopLogState) -> String {
        let ordinal = self.next_attempt_ordinal.fetch_add(1, Ordering::AcqRel);
        let id = generate_attempt_id(ordinal);
        state.current_attempt = Some(CurrentStartupAttempt {
            id: id.clone(),
            ordinal,
            next_sequence: 1,
            started: Instant::now(),
            terminal: false,
            failed: false,
        });
        id
    }
}

#[allow(clippy::too_many_arguments)]
fn new_event(
    state: &mut DesktopLogState,
    sequence: u64,
    source: DesktopLogSource,
    level: DesktopLogLevel,
    event: &'static str,
    stage: Option<&str>,
    result: Option<&str>,
    code: Option<&str>,
    duration_bucket: Option<&'static str>,
    exit_code: Option<u32>,
    signal: Option<u32>,
    errno: Option<u64>,
) -> DesktopDiagnosticEventV2 {
    let (attempt_id, attempt_ordinal, attempt_sequence) = match state.current_attempt.as_mut() {
        Some(attempt) => {
            let sequence = attempt.next_sequence;
            attempt.next_sequence = attempt.next_sequence.saturating_add(1);
            (
                Some(attempt.id.clone()),
                Some(attempt.ordinal),
                Some(sequence),
            )
        }
        None => (None, None, None),
    };
    DesktopDiagnosticEventV2 {
        schema_version: LOG_SCHEMA_VERSION_V2.to_string(),
        sequence,
        occurred_at: current_timestamp(),
        attempt_id,
        attempt_ordinal,
        attempt_sequence,
        component: source.as_str().to_string(),
        level: level.as_str().to_string(),
        event: event.to_string(),
        stage: stage.map(str::to_string),
        result: result.map(str::to_string),
        code: code.filter(|value| is_safe_code(value)).map(str::to_string),
        duration_bucket: duration_bucket.map(str::to_string),
        product_version: env!("CARGO_PKG_VERSION").to_string(),
        source_commit: state.source_commit.clone(),
        exit_code,
        signal,
        errno,
    }
}

fn persist_and_push(state: &mut DesktopLogState, entry: DesktopDiagnosticEventV2) {
    if !valid_recovered_v2_event(&entry) {
        state.root = None;
        return;
    }
    if state
        .root
        .as_ref()
        .is_some_and(|root| append_event(root, &entry).is_err())
    {
        state.root = None;
    }
    push_memory_event(state, entry);
}

fn availability(state: &DesktopLogState) -> String {
    if state.root.is_some() {
        "available"
    } else if state.persistent_attempted || !state.entries.is_empty() {
        "memory_only"
    } else {
        "unavailable"
    }
    .to_string()
}

fn snapshot_v1(state: &DesktopLogState, limit: usize) -> DesktopLogTailV1 {
    let start = state.entries.len().saturating_sub(limit);
    DesktopLogTailV1 {
        schema_version: LOG_SCHEMA_VERSION_V1.to_string(),
        availability: availability(state),
        entries: state
            .entries
            .iter()
            .skip(start)
            .map(project_v1_event)
            .collect(),
        dropped_count: state.dropped_count,
    }
}

fn snapshot_v2(state: &DesktopLogState, limit: usize) -> DesktopDiagnosticLogV2 {
    let start = state.entries.len().saturating_sub(limit);
    let entries: Vec<_> = state.entries.iter().skip(start).cloned().collect();
    let (attempts, dropped_attempt_count) = summarize_attempts(
        &entries,
        state
            .current_attempt
            .as_ref()
            .map(|attempt| attempt.id.as_str()),
    );
    DesktopDiagnosticLogV2 {
        schema_version: LOG_SCHEMA_VERSION_V2.to_string(),
        availability: availability(state),
        environment: state.environment.clone(),
        attempts,
        dropped_attempt_count,
        entries,
        dropped_count: state.dropped_count,
    }
}

fn project_v1_event(event: &DesktopDiagnosticEventV2) -> DesktopLogEventV1 {
    let projected_event = if event.event == "startup_stage" {
        if event.component == "renderer" {
            "renderer_stage"
        } else {
            "sidecar_startup_diagnostic"
        }
    } else {
        event.event.as_str()
    };
    let projected_code = event.code.clone().or_else(|| {
        let stage = event.stage.as_deref()?;
        let result = event.result.as_deref()?;
        let value = format!("{stage}_{result}");
        is_safe_code(&value).then_some(value)
    });
    DesktopLogEventV1 {
        schema_version: LOG_SCHEMA_VERSION_V1.to_string(),
        sequence: event.sequence,
        occurred_at: event.occurred_at.clone(),
        source: event.component.clone(),
        level: event.level.clone(),
        event: projected_event.to_string(),
        code: projected_code,
        exit_code: event.exit_code,
        signal: event.signal,
        errno: event.errno,
    }
}

fn push_memory_event(state: &mut DesktopLogState, event: DesktopDiagnosticEventV2) {
    if state.entries.len() == MAX_MEMORY_EVENTS {
        state.entries.pop_front();
        state.dropped_count = state.dropped_count.saturating_add(1);
    }
    state.entries.push_back(event);
}

fn summarize_attempts(
    entries: &[DesktopDiagnosticEventV2],
    active_attempt_id: Option<&str>,
) -> (Vec<DesktopStartupAttemptSummaryV2>, u64) {
    let mut summaries: Vec<DesktopStartupAttemptSummaryV2> = Vec::new();
    let mut indices: HashMap<&str, usize> = HashMap::new();
    for entry in entries {
        let (Some(attempt_id), Some(ordinal)) =
            (entry.attempt_id.as_deref(), entry.attempt_ordinal)
        else {
            continue;
        };
        let index = match indices.get(attempt_id) {
            Some(index) => *index,
            None => {
                let index = summaries.len();
                indices.insert(attempt_id, index);
                summaries.push(DesktopStartupAttemptSummaryV2 {
                    schema_version: LOG_SCHEMA_VERSION_V2.to_string(),
                    attempt_id: attempt_id.to_string(),
                    ordinal,
                    event_count: 0,
                    last_completed_stage: None,
                    first_failed_stage: None,
                    duration_bucket: "unknown".to_string(),
                    outcome: "interrupted".to_string(),
                });
                index
            }
        };
        let summary = &mut summaries[index];
        summary.event_count = summary.event_count.saturating_add(1);
        if let Some(duration) = entry.duration_bucket.as_ref() {
            summary.duration_bucket = duration.clone();
        }
        if entry.result.as_deref() == Some("completed") {
            summary.last_completed_stage = entry.stage.clone();
            if entry.stage.as_deref() == Some(DesktopStartupStage::RendererReady.as_str())
                && summary.first_failed_stage.is_none()
            {
                summary.outcome = "succeeded".to_string();
            }
        } else if entry.result.as_deref() == Some("failed") && summary.first_failed_stage.is_none()
        {
            summary.first_failed_stage = entry.stage.clone();
            summary.outcome = "failed".to_string();
        }
    }
    if let Some(active) = active_attempt_id {
        if let Some(index) = indices.get(active).copied() {
            if summaries[index].outcome == "interrupted" {
                summaries[index].outcome = "active".to_string();
            }
        }
    }
    summaries.sort_by_key(|summary| summary.ordinal);
    let dropped = summaries.len().saturating_sub(MAX_ATTEMPT_SUMMARIES) as u64;
    if dropped > 0 {
        summaries.drain(..dropped as usize);
    }
    (summaries, dropped)
}

fn duration_bucket(duration: Duration) -> &'static str {
    if duration < Duration::from_millis(10) {
        "under_10ms"
    } else if duration < Duration::from_millis(100) {
        "under_100ms"
    } else if duration < Duration::from_secs(1) {
        "under_1s"
    } else if duration < Duration::from_secs(10) {
        "under_10s"
    } else if duration < Duration::from_secs(60) {
        "under_60s"
    } else {
        "at_least_60s"
    }
}

fn generate_attempt_id(ordinal: u64) -> String {
    let mut random = [0_u8; 16];
    if OsRng.try_fill_bytes(&mut random).is_err() {
        let duration = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        let mut digest = Sha256::new();
        digest.update(b"openevo-desktop-startup-attempt-v2\0");
        digest.update(std::process::id().to_be_bytes());
        digest.update(ordinal.to_be_bytes());
        digest.update(duration.as_nanos().to_be_bytes());
        random.copy_from_slice(&digest.finalize()[..16]);
    }
    let mut encoded = String::with_capacity(32);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for byte in random {
        encoded.push(HEX[(byte >> 4) as usize] as char);
        encoded.push(HEX[(byte & 0x0f) as usize] as char);
    }
    encoded
}

fn valid_environment(environment: &DesktopEnvironmentSummaryV2) -> bool {
    environment.schema_version == LOG_SCHEMA_VERSION_V2
        && matches!(
            environment.os_family.as_str(),
            "macos" | "linux" | "unknown"
        )
        && is_safe_environment_value(&environment.os_version)
        && is_safe_environment_value(&environment.os_build)
        && matches!(
            environment.architecture.as_str(),
            "arm64" | "x86_64" | "unknown"
        )
        && matches!(
            environment.app_location.as_str(),
            "applications" | "mounted_dmg" | "translocated" | "other" | "unknown"
        )
        && matches!(
            environment.quarantine.as_str(),
            "present" | "absent" | "unknown"
        )
        && matches!(
            environment.translocation.as_str(),
            "present" | "absent" | "unknown"
        )
}

fn is_safe_environment_value(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn is_source_commit(value: &str) -> bool {
    (7..=40).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
        && value.bytes().any(|byte| byte != b'0')
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

fn append_event(root: &PersistentLogRoot, event: &DesktopDiagnosticEventV2) -> std::io::Result<()> {
    validate_root_binding(root)?;
    let _lock = acquire_log_lock(root)?;
    let mut encoded = serde_json::to_vec(event)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    if encoded.len() > MAX_LOG_LINE_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "desktop log entry exceeds line budget",
        ));
    }
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
    let mut recovered = Vec::new();
    for index in (1..=MAX_ROTATED_FILES).rev() {
        recover_file(root, &rotated_log_name(index), &mut recovered, false)?;
    }
    recover_file(root, CURRENT_LOG_FILE, &mut recovered, true)?;
    recovered.sort_by_key(RecoveredLogEvent::sequence);
    if recovered
        .windows(2)
        .any(|pair| pair[0].sequence() >= pair[1].sequence())
    {
        return Err(invalid_log_event_error());
    }
    let events = migrate_recovered_events(recovered)?;
    validate_recovered_event_sequence(&events)?;
    let mut events = events;
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
    events: Vec<DesktopDiagnosticEventV2>,
    dropped_count: u64,
}

enum RecoveredLogEvent {
    V1(DesktopLogEventV1),
    V2(DesktopDiagnosticEventV2),
}

impl RecoveredLogEvent {
    fn sequence(&self) -> u64 {
        match self {
            Self::V1(event) => event.sequence,
            Self::V2(event) => event.sequence,
        }
    }
}

fn recover_file(
    root: &PersistentLogRoot,
    name: &str,
    recovered: &mut Vec<RecoveredLogEvent>,
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
        if line.len() > MAX_LOG_LINE_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "desktop log line exceeds recovery budget",
            ));
        }
        let event = match serde_json::from_slice::<DesktopDiagnosticEventV2>(line) {
            Ok(event) if valid_recovered_v2_event(&event) => RecoveredLogEvent::V2(event),
            _ => {
                let event: DesktopLogEventV1 =
                    serde_json::from_slice(line).map_err(|_| invalid_log_event_error())?;
                if !valid_recovered_v1_event(&event) {
                    return Err(invalid_log_event_error());
                }
                RecoveredLogEvent::V1(event)
            }
        };
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

fn invalid_log_event_error() -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        "desktop log event is invalid",
    )
}

fn migrate_recovered_events(
    recovered: Vec<RecoveredLogEvent>,
) -> std::io::Result<Vec<DesktopDiagnosticEventV2>> {
    let mut events = Vec::with_capacity(recovered.len());
    let mut legacy_attempt_id: Option<String> = None;
    let mut legacy_attempt_ordinal = 0_u64;
    let mut legacy_attempt_sequence = 0_u64;
    for recovered_event in recovered {
        match recovered_event {
            RecoveredLogEvent::V2(event) => events.push(event),
            RecoveredLogEvent::V1(event) => {
                if legacy_attempt_id.is_none() || event.event == "application_started" {
                    legacy_attempt_ordinal = event.sequence;
                    legacy_attempt_sequence = 0;
                    legacy_attempt_id = Some(legacy_attempt_id_for(&event)?);
                }
                legacy_attempt_sequence = legacy_attempt_sequence.saturating_add(1);
                events.push(DesktopDiagnosticEventV2 {
                    schema_version: LOG_SCHEMA_VERSION_V2.to_string(),
                    sequence: event.sequence,
                    occurred_at: event.occurred_at,
                    attempt_id: legacy_attempt_id.clone(),
                    attempt_ordinal: Some(legacy_attempt_ordinal),
                    attempt_sequence: Some(legacy_attempt_sequence),
                    component: event.source,
                    level: event.level,
                    event: event.event,
                    stage: None,
                    result: None,
                    code: event.code,
                    duration_bucket: None,
                    product_version: "legacy".to_string(),
                    source_commit: None,
                    exit_code: event.exit_code,
                    signal: event.signal,
                    errno: event.errno,
                });
            }
        }
    }
    Ok(events)
}

fn legacy_attempt_id_for(event: &DesktopLogEventV1) -> std::io::Result<String> {
    let encoded = serde_json::to_vec(event).map_err(|_| invalid_log_event_error())?;
    let mut digest = Sha256::new();
    digest.update(b"openevo-desktop-legacy-log-attempt-v2\0");
    digest.update((encoded.len() as u64).to_be_bytes());
    digest.update(encoded);
    let digest = digest.finalize();
    let mut id = String::with_capacity(32);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for byte in &digest[..16] {
        id.push(HEX[(byte >> 4) as usize] as char);
        id.push(HEX[(byte & 0x0f) as usize] as char);
    }
    Ok(id)
}

fn validate_recovered_event_sequence(events: &[DesktopDiagnosticEventV2]) -> std::io::Result<()> {
    let mut attempts: HashMap<&str, (u64, u64, &str, Option<&str>)> = HashMap::new();
    let mut ordinals: HashMap<u64, &str> = HashMap::new();
    for event in events {
        if !valid_recovered_v2_event(event) {
            return Err(invalid_log_event_error());
        }
        let (Some(attempt_id), Some(ordinal), Some(sequence)) = (
            event.attempt_id.as_deref(),
            event.attempt_ordinal,
            event.attempt_sequence,
        ) else {
            continue;
        };
        if ordinals
            .insert(ordinal, attempt_id)
            .is_some_and(|existing| existing != attempt_id)
        {
            return Err(invalid_log_event_error());
        }
        match attempts.get_mut(attempt_id) {
            Some((existing_ordinal, last_sequence, product_version, source_commit)) => {
                if *existing_ordinal != ordinal
                    || sequence <= *last_sequence
                    || *product_version != event.product_version
                    || source_commit
                        .is_some_and(|value| event.source_commit.as_deref() != Some(value))
                {
                    return Err(invalid_log_event_error());
                }
                *last_sequence = sequence;
                if source_commit.is_none() {
                    *source_commit = event.source_commit.as_deref();
                }
            }
            None => {
                attempts.insert(
                    attempt_id,
                    (
                        ordinal,
                        sequence,
                        &event.product_version,
                        event.source_commit.as_deref(),
                    ),
                );
            }
        }
    }
    Ok(())
}

fn valid_recovered_v1_event(event: &DesktopLogEventV1) -> bool {
    event.schema_version == LOG_SCHEMA_VERSION_V1
        && event.sequence > 0
        && is_valid_timestamp(&event.occurred_at)
        && matches!(
            event.source.as_str(),
            "native" | "startup" | "sidecar" | "renderer"
        )
        && matches!(event.level.as_str(), "info" | "warning" | "error")
        && is_known_event(&event.event)
        && event.event != "startup_stage"
        && event.code.as_deref().is_none_or(is_safe_code)
}

fn valid_recovered_v2_event(event: &DesktopDiagnosticEventV2) -> bool {
    let attempt_fields = (
        event.attempt_id.as_deref(),
        event.attempt_ordinal,
        event.attempt_sequence,
    );
    let attempt_valid = match attempt_fields {
        (Some(id), Some(ordinal), Some(sequence)) => {
            id.len() == 32 && id.bytes().all(is_lower_hex) && ordinal > 0 && sequence > 0
        }
        (None, None, None) => true,
        _ => false,
    };
    let stage_fields_valid = if event.event == "startup_stage" {
        event.stage.as_deref().is_some_and(is_startup_stage)
            && matches!(
                event.result.as_deref(),
                Some("started" | "completed" | "failed")
            )
            && event
                .duration_bucket
                .as_deref()
                .is_some_and(is_duration_bucket)
            && event.attempt_id.is_some()
            && ((event.result.as_deref() == Some("failed") && event.level == "error")
                || (event.result.as_deref() != Some("failed") && event.level == "info"))
    } else {
        event.stage.is_none() && event.result.is_none() && event.duration_bucket.is_none()
    };
    event.schema_version == LOG_SCHEMA_VERSION_V2
        && event.sequence > 0
        && is_valid_timestamp(&event.occurred_at)
        && attempt_valid
        && matches!(
            event.component.as_str(),
            "native" | "startup" | "sidecar" | "renderer"
        )
        && matches!(event.level.as_str(), "info" | "warning" | "error")
        && is_known_event(&event.event)
        && !matches!(
            event.event.as_str(),
            "sidecar_startup_diagnostic" | "renderer_stage"
        )
        && stage_fields_valid
        && event.code.as_deref().is_none_or(is_safe_code)
        && is_safe_product_version(&event.product_version)
        && event.source_commit.as_deref().is_none_or(is_source_commit)
}

fn is_startup_stage(value: &str) -> bool {
    matches!(
        value,
        "native_application"
            | "bundle_verification"
            | "sidecar_spawn"
            | "descriptor_handoff"
            | "bootloader"
            | "embedded_python"
            | "sidecar_entry"
            | "state_store"
            | "local_api"
            | "renderer_bootstrap"
            | "renderer_ready"
    )
}

fn is_duration_bucket(value: &str) -> bool {
    matches!(
        value,
        "under_10ms" | "under_100ms" | "under_1s" | "under_10s" | "under_60s" | "at_least_60s"
    )
}

fn is_safe_product_version(value: &str) -> bool {
    if value == "legacy" {
        return true;
    }
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'+' | b'-'))
    {
        return false;
    }
    let core_end = value.find(['+', '-']).unwrap_or(value.len());
    let core = &value[..core_end];
    let mut parts = core.split('.');
    let core_valid = (0..3).all(|_| {
        parts
            .next()
            .is_some_and(|part| !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()))
    }) && parts.next().is_none();
    core_valid && (core_end == value.len() || core_end + 1 < value.len())
}

fn is_lower_hex(byte: u8) -> bool {
    byte.is_ascii_digit() || matches!(byte, b'a'..=b'f')
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
            | "startup_stage"
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

    fn test_environment() -> DesktopEnvironmentSummaryV2 {
        DesktopEnvironmentSummaryV2 {
            schema_version: "2".to_string(),
            os_family: "macos".to_string(),
            os_version: "26.5.2".to_string(),
            os_build: "25F84".to_string(),
            architecture: "arm64".to_string(),
            app_location: "applications".to_string(),
            quarantine: "present".to_string(),
            translocation: "absent".to_string(),
        }
    }

    #[test]
    fn startup_v2_binds_attempt_sequence_stage_summary_and_environment() {
        let store = DesktopLogStore::default();
        assert!(store.set_environment(test_environment()));
        let attempt_id = store.begin_startup_attempt();
        store.record_startup_stage(
            DesktopLogSource::Native,
            DesktopStartupStage::NativeApplication,
            DesktopStartupResult::Completed,
            Some("initialized"),
            None,
            None,
            None,
        );
        let source_commit = "b".repeat(40);
        assert!(store.update_release_identity(env!("CARGO_PKG_VERSION"), &source_commit));
        assert!(!store.update_release_identity(env!("CARGO_PKG_VERSION"), &"c".repeat(40)));
        store.record_startup_stage(
            DesktopLogSource::Startup,
            DesktopStartupStage::StateStore,
            DesktopStartupResult::Failed,
            Some("provider_store_failed"),
            None,
            None,
            Some(13),
        );

        let export = store.export_snapshot();
        assert_eq!(export.schema_version, "2");
        assert_eq!(export.environment, test_environment());
        assert_eq!(export.entries.len(), 2);
        assert!(export
            .entries
            .iter()
            .all(|entry| entry.schema_version == "2"));
        assert_eq!(
            export.entries[0].attempt_id.as_deref(),
            Some(attempt_id.as_str())
        );
        assert_eq!(export.entries[0].attempt_sequence, Some(1));
        assert_eq!(export.entries[1].attempt_sequence, Some(2));
        assert_eq!(
            export.entries[0].stage.as_deref(),
            Some("native_application")
        );
        assert_eq!(export.entries[1].stage.as_deref(), Some("state_store"));
        assert_eq!(export.entries[1].result.as_deref(), Some("failed"));
        assert_eq!(export.entries[0].source_commit, None);
        assert_eq!(
            export.entries[1].source_commit.as_deref(),
            Some(source_commit.as_str())
        );
        assert!(export.entries.iter().all(|entry| {
            matches!(
                entry.duration_bucket.as_deref(),
                Some("under_10ms")
                    | Some("under_100ms")
                    | Some("under_1s")
                    | Some("under_10s")
                    | Some("under_60s")
                    | Some("at_least_60s")
            )
        }));
        assert_eq!(export.attempts.len(), 1);
        assert_eq!(export.attempts[0].attempt_id, attempt_id);
        assert_eq!(
            export.attempts[0].last_completed_stage.as_deref(),
            Some("native_application")
        );
        assert_eq!(
            export.attempts[0].first_failed_stage.as_deref(),
            Some("state_store")
        );
        assert_eq!(export.attempts[0].outcome, "failed");
    }

    #[test]
    fn startup_v2_persists_across_relaunch_and_strictly_migrates_v1() {
        let root = tempfile::tempdir().unwrap();
        let logs = root.path().join(LOG_DIRECTORY_NAME);
        fs::create_dir(&logs).unwrap();
        fs::set_permissions(&logs, fs::Permissions::from_mode(0o700)).unwrap();
        let legacy = DesktopLogEventV1 {
            schema_version: "1".to_string(),
            sequence: 7,
            occurred_at: "2026-07-22T14:55:01.123Z".to_string(),
            source: "startup".to_string(),
            level: "error".to_string(),
            event: "sidecar_pre_python_exit".to_string(),
            code: Some("sidecar_exited_during_startup".to_string()),
            exit_code: Some(255),
            signal: None,
            errno: None,
        };
        let mut encoded = serde_json::to_vec(&legacy).unwrap();
        encoded.push(b'\n');
        fs::write(logs.join(CURRENT_LOG_FILE), encoded).unwrap();
        fs::set_permissions(
            logs.join(CURRENT_LOG_FILE),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();

        let first = DesktopLogStore::default();
        assert!(first.set_environment(test_environment()));
        assert!(first.bind_app_data_root(root.path()));
        first.begin_startup_attempt();
        first.record_startup_stage(
            DesktopLogSource::Native,
            DesktopStartupStage::NativeApplication,
            DesktopStartupResult::Completed,
            None,
            None,
            None,
            None,
        );

        let second = DesktopLogStore::default();
        assert!(second.set_environment(test_environment()));
        assert!(second.bind_app_data_root(root.path()));
        let export = second.export_snapshot();
        assert_eq!(export.entries.len(), 2);
        assert!(export
            .entries
            .iter()
            .all(|entry| entry.schema_version == "2"));
        assert_eq!(export.entries[0].sequence, 7);
        assert_eq!(export.entries[0].event, "sidecar_pre_python_exit");
        assert_eq!(export.entries[1].sequence, 8);
        assert_eq!(export.entries[1].event, "startup_stage");
    }

    #[test]
    fn startup_v2_recovery_rejects_an_open_path_bearing_envelope() {
        let root = tempfile::tempdir().unwrap();
        let first = DesktopLogStore::default();
        assert!(first.bind_app_data_root(root.path()));
        first.begin_startup_attempt();
        first.record_startup_stage(
            DesktopLogSource::Native,
            DesktopStartupStage::NativeApplication,
            DesktopStartupResult::Completed,
            Some("initialized"),
            None,
            None,
            None,
        );
        drop(first);

        let log_path = root.path().join(LOG_DIRECTORY_NAME).join(CURRENT_LOG_FILE);
        let payload = fs::read_to_string(&log_path).unwrap();
        let mut event: serde_json::Value = serde_json::from_str(payload.trim()).unwrap();
        event.as_object_mut().unwrap().insert(
            "raw_path".to_string(),
            serde_json::json!("/Users/private/token=secret"),
        );
        fs::write(
            &log_path,
            format!("{}\n", serde_json::to_string(&event).unwrap()),
        )
        .unwrap();

        let recovered = DesktopLogStore::default();
        assert!(!recovered.bind_app_data_root(root.path()));
        assert!(recovered.export_snapshot().entries.is_empty());
    }

    #[test]
    fn startup_v2_rejects_open_or_secret_bearing_environment_categories() {
        let store = DesktopLogStore::default();
        let mut invalid = test_environment();
        invalid.app_location = "/Applications/Secret.app token=value".to_string();
        assert!(!store.set_environment(invalid));
        assert_eq!(store.export_snapshot().environment.app_location, "unknown");
    }
}
