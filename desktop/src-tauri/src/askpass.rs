use std::collections::HashSet;
#[cfg(target_os = "macos")]
use std::ffi::CStr;
use std::fmt;
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const MAX_PROMPT_BYTES: usize = 2_048;
const MAX_SECRET_BYTES: usize = 4_096;
const MAX_ANCESTORS: usize = 32;
const SYSTEM_SSH_PATH: &str = "/usr/bin/ssh";
const BROKER_RESPONSE_MAX_BYTES: usize = 512;
const BROKER_TIMEOUT: Duration = Duration::from_secs(3);
const ASKPASS_SOCKET_ENV: &str = "OPENEVO_SSH_ASKPASS_SOCKET";
const ASKPASS_CAPABILITY_ENV: &str = "OPENEVO_SSH_ASKPASS_CAPABILITY";
const CONNECTION_GENERATION_ENV: &str = "OPENEVO_SSH_CONNECTION_GENERATION";
const SSH_OWNER_PID_ENV: &str = "OPENEVO_SSH_OWNER_PID";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PromptKind {
    Password,
    Passphrase,
    HostConfirmation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ClassifiedPrompt {
    pub kind: PromptKind,
    pub display_detail: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AskpassError {
    InvalidPrompt,
    InvalidAuthority,
    AuthorizationDenied,
    Cancelled,
    InvalidResponse,
    OutputFailed,
}

pub struct SecretResponse(Vec<u8>);

impl SecretResponse {
    pub fn new(value: Vec<u8>) -> Self {
        Self(value)
    }
}

impl fmt::Debug for SecretResponse {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SecretResponse(<redacted>)")
    }
}

impl Drop for SecretResponse {
    fn drop(&mut self) {
        self.0.fill(0);
    }
}

#[derive(Debug)]
pub enum DialogOutcome {
    Secret(SecretResponse),
    Confirm(bool),
    Cancelled,
}

pub trait PromptDialog: Send + Sync {
    fn present(&self, prompt: &ClassifiedPrompt) -> Result<DialogOutcome, AskpassError>;
}

pub trait ProcessInspector: Send + Sync {
    fn parent_pid(&self, pid: u32) -> Option<u32>;
    fn executable_path(&self, pid: u32) -> Option<PathBuf>;
}

pub struct AuthorizationRequest<'a> {
    pub capability: &'a str,
    pub connection_generation: u64,
    pub helper_pid: u32,
    pub ssh_parent_pid: u32,
    pub prompt_kind: PromptKind,
    pub prompt_sha256: [u8; 32],
    pub prompt_bytes: usize,
}

impl fmt::Debug for AuthorizationRequest<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AuthorizationRequest")
            .field("capability", &"<redacted>")
            .field("connection_generation", &self.connection_generation)
            .field("helper_pid", &self.helper_pid)
            .field("ssh_parent_pid", &self.ssh_parent_pid)
            .field("prompt_kind", &self.prompt_kind)
            .field("prompt_sha256", &self.prompt_sha256)
            .field("prompt_bytes", &self.prompt_bytes)
            .finish()
    }
}

pub trait PromptAuthorizer: Send + Sync {
    fn authorize(&self, request: &AuthorizationRequest<'_>) -> Result<(), AskpassError>;

    fn complete(
        &self,
        _request: &AuthorizationRequest<'_>,
        _outcome: PromptCompletionOutcome,
    ) -> Result<(), AskpassError> {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PromptCompletionOutcome {
    Accepted,
    Rejected,
    Cancelled,
}

pub fn classify_prompt(prompt: &[u8]) -> Result<ClassifiedPrompt, AskpassError> {
    if prompt.is_empty() || prompt.len() > MAX_PROMPT_BYTES || prompt.contains(&0) {
        return Err(AskpassError::InvalidPrompt);
    }
    let text = std::str::from_utf8(prompt).map_err(|_| AskpassError::InvalidPrompt)?;
    let lower = text.to_ascii_lowercase();
    if lower.contains("enter passphrase for key") && lower.trim_end().ends_with(':') {
        return Ok(ClassifiedPrompt {
            kind: PromptKind::Passphrase,
            display_detail: "System OpenSSH is requesting a private-key passphrase.".to_string(),
        });
    }
    if lower.contains("password:") && lower.trim_end().ends_with(':') {
        return Ok(ClassifiedPrompt {
            kind: PromptKind::Password,
            display_detail: "System OpenSSH is requesting the server password.".to_string(),
        });
    }
    if lower.contains("the authenticity of host '")
        && lower.contains("are you sure you want to continue connecting")
        && lower.contains("yes/no")
    {
        let host = extract_confirmation_host(text).ok_or(AskpassError::InvalidPrompt)?;
        let fingerprint = extract_sha256_fingerprint(text).ok_or(AskpassError::InvalidPrompt)?;
        let algorithm = extract_host_key_algorithm(text).ok_or(AskpassError::InvalidPrompt)?;
        return Ok(ClassifiedPrompt {
            kind: PromptKind::HostConfirmation,
            display_detail: format!(
                "Host: {host}\nKey: {algorithm} {fingerprint}\n\
                 Continue only if this fingerprint is expected."
            ),
        });
    }
    Err(AskpassError::InvalidPrompt)
}

pub fn validate_ssh_process_chain(
    inspector: &dyn ProcessInspector,
    helper_pid: u32,
    owner_pid: u32,
) -> Result<u32, AskpassError> {
    if helper_pid <= 1 || owner_pid <= 1 || helper_pid == owner_pid {
        return Err(AskpassError::InvalidAuthority);
    }
    let ssh_parent_pid = inspector
        .parent_pid(helper_pid)
        .filter(|pid| *pid > 1 && *pid != helper_pid)
        .ok_or(AskpassError::InvalidAuthority)?;
    if inspector.executable_path(ssh_parent_pid).as_deref()
        != Some(std::path::Path::new(SYSTEM_SSH_PATH))
    {
        return Err(AskpassError::InvalidAuthority);
    }

    let mut observed = HashSet::new();
    let mut current = ssh_parent_pid;
    for _ in 0..MAX_ANCESTORS {
        if !observed.insert(current) {
            return Err(AskpassError::InvalidAuthority);
        }
        if current == owner_pid {
            if inspector.executable_path(current).as_deref()
                != Some(std::path::Path::new(SYSTEM_SSH_PATH))
            {
                return Err(AskpassError::InvalidAuthority);
            }
            return Ok(ssh_parent_pid);
        }
        current = inspector
            .parent_pid(current)
            .filter(|pid| *pid > 1)
            .ok_or(AskpassError::InvalidAuthority)?;
    }
    Err(AskpassError::InvalidAuthority)
}

#[allow(clippy::too_many_arguments)]
pub fn execute_askpass(
    prompt: &[u8],
    helper_pid: u32,
    owner_pid: u32,
    connection_generation: u64,
    capability: &str,
    inspector: &dyn ProcessInspector,
    authorizer: &dyn PromptAuthorizer,
    dialog: &dyn PromptDialog,
    output: &mut dyn Write,
) -> Result<(), AskpassError> {
    if connection_generation == 0
        || capability.len() != 64
        || !capability
            .bytes()
            .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
    {
        return Err(AskpassError::InvalidAuthority);
    }
    let classified = classify_prompt(prompt)?;
    let ssh_parent_pid = validate_ssh_process_chain(inspector, helper_pid, owner_pid)?;
    let prompt_digest = Sha256::digest(prompt);
    let mut prompt_sha256 = [0_u8; 32];
    prompt_sha256.copy_from_slice(&prompt_digest);
    let request = AuthorizationRequest {
        capability,
        connection_generation,
        helper_pid,
        ssh_parent_pid,
        prompt_kind: classified.kind,
        prompt_sha256,
        prompt_bytes: prompt.len(),
    };
    authorizer.authorize(&request)?;

    match dialog.present(&classified)? {
        DialogOutcome::Secret(secret)
            if matches!(
                classified.kind,
                PromptKind::Password | PromptKind::Passphrase
            ) =>
        {
            if secret.0.is_empty()
                || secret.0.len() > MAX_SECRET_BYTES
                || secret
                    .0
                    .iter()
                    .any(|value| matches!(*value, b'\0' | b'\r' | b'\n'))
            {
                return Err(AskpassError::InvalidResponse);
            }
            authorizer.complete(&request, PromptCompletionOutcome::Accepted)?;
            output
                .write_all(&secret.0)
                .and_then(|_| output.write_all(b"\n"))
                .map_err(|_| AskpassError::OutputFailed)
        }
        DialogOutcome::Confirm(answer) if classified.kind == PromptKind::HostConfirmation => {
            authorizer.complete(
                &request,
                if answer {
                    PromptCompletionOutcome::Accepted
                } else {
                    PromptCompletionOutcome::Rejected
                },
            )?;
            output
                .write_all(if answer { b"yes\n" } else { b"no\n" })
                .map_err(|_| AskpassError::OutputFailed)
        }
        DialogOutcome::Cancelled => {
            authorizer.complete(&request, PromptCompletionOutcome::Cancelled)?;
            Err(AskpassError::Cancelled)
        }
        DialogOutcome::Secret(_) | DialogOutcome::Confirm(_) => Err(AskpassError::InvalidResponse),
    }
}

fn extract_confirmation_host(prompt: &str) -> Option<&str> {
    let start = prompt.find("authenticity of host '")? + "authenticity of host '".len();
    let remaining = &prompt[start..];
    let end = remaining.find('\'')?;
    let host = &remaining[..end];
    if host.is_empty()
        || host.len() > 255
        || !host.bytes().all(|value| {
            value.is_ascii_alphanumeric()
                || matches!(
                    value,
                    b'.' | b'-' | b'_' | b':' | b'[' | b']' | b'(' | b')' | b' ' | b'%'
                )
        })
    {
        return None;
    }
    Some(host)
}

fn extract_sha256_fingerprint(prompt: &str) -> Option<&str> {
    prompt.split_ascii_whitespace().find_map(|token| {
        let candidate = token.trim_end_matches(['.', ',', ';']);
        let encoded = candidate.strip_prefix("SHA256:")?;
        if encoded.len() == 43
            && encoded
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'+' | b'/'))
        {
            Some(candidate)
        } else {
            None
        }
    })
}

fn extract_host_key_algorithm(prompt: &str) -> Option<&'static str> {
    let upper = prompt.to_ascii_uppercase();
    if upper.contains("ED25519 KEY FINGERPRINT") {
        Some("ED25519")
    } else if upper.contains("ECDSA KEY FINGERPRINT") {
        Some("ECDSA")
    } else if upper.contains("RSA KEY FINGERPRINT") {
        Some("RSA")
    } else {
        None
    }
}

struct SecretText(String);

impl SecretText {
    fn from_environment(name: &str) -> Result<Self, AskpassError> {
        let value = std::env::var(name).map_err(|_| AskpassError::InvalidAuthority)?;
        std::env::remove_var(name);
        Ok(Self(value))
    }

    fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for SecretText {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SecretText(<redacted>)")
    }
}

impl Drop for SecretText {
    fn drop(&mut self) {
        // Replacing valid UTF-8 bytes with zeroes keeps the String invariant.
        unsafe {
            self.0.as_mut_vec().fill(0);
        }
    }
}

struct DarwinProcessInspector;

impl ProcessInspector for DarwinProcessInspector {
    fn parent_pid(&self, pid: u32) -> Option<u32> {
        #[cfg(target_os = "macos")]
        {
            let pid = i32::try_from(pid).ok()?;
            let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::uninit();
            let size = i32::try_from(std::mem::size_of::<libc::proc_bsdinfo>()).ok()?;
            let result = unsafe {
                libc::proc_pidinfo(
                    pid,
                    libc::PROC_PIDTBSDINFO,
                    0,
                    info.as_mut_ptr().cast(),
                    size,
                )
            };
            if result != size {
                return None;
            }
            let info = unsafe { info.assume_init() };
            if info.pbi_pid != pid as u32 || info.pbi_ppid <= 1 {
                return None;
            }
            Some(info.pbi_ppid)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = pid;
            None
        }
    }

    fn executable_path(&self, pid: u32) -> Option<PathBuf> {
        #[cfg(target_os = "macos")]
        {
            let pid = i32::try_from(pid).ok()?;
            let mut buffer = [0_u8; 4_096];
            let length = unsafe {
                libc::proc_pidpath(
                    pid,
                    buffer.as_mut_ptr().cast(),
                    u32::try_from(buffer.len()).ok()?,
                )
            };
            if length <= 0 {
                return None;
            }
            let value = CStr::from_bytes_until_nul(&buffer).ok()?;
            Some(PathBuf::from(std::ffi::OsStr::from_bytes(value.to_bytes())))
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = pid;
            None
        }
    }
}

#[derive(Serialize)]
struct BrokerAuthorizationRequest<'a> {
    schema_version: u8,
    event: &'static str,
    capability: &'a str,
    connection_generation: u64,
    helper_pid: u32,
    ssh_parent_pid: u32,
    owner_pid: u32,
    prompt_kind: &'static str,
    prompt_sha256: String,
    prompt_bytes: usize,
}

#[derive(Serialize)]
struct BrokerCompletionRequest<'a> {
    schema_version: u8,
    event: &'static str,
    capability: &'a str,
    connection_generation: u64,
    helper_pid: u32,
    ssh_parent_pid: u32,
    owner_pid: u32,
    prompt_kind: &'static str,
    prompt_sha256: String,
    prompt_bytes: usize,
    outcome: &'static str,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BrokerAuthorizationResponse {
    schema_version: u8,
    authorized: bool,
}

struct BrokerAuthorizer {
    socket_path: PathBuf,
    owner_pid: u32,
}

impl BrokerAuthorizer {
    fn from_environment(owner_pid: u32) -> Result<Self, AskpassError> {
        let value = std::env::var_os(ASKPASS_SOCKET_ENV).ok_or(AskpassError::InvalidAuthority)?;
        let bytes = value.as_bytes();
        if bytes.is_empty()
            || bytes.len() > 103
            || bytes.contains(&0)
            || !std::path::Path::new(&value).is_absolute()
        {
            return Err(AskpassError::InvalidAuthority);
        }
        Ok(Self {
            socket_path: PathBuf::from(value),
            owner_pid,
        })
    }
}

impl PromptAuthorizer for BrokerAuthorizer {
    fn authorize(&self, request: &AuthorizationRequest<'_>) -> Result<(), AskpassError> {
        let encoded_digest = hex_digest(&request.prompt_sha256);
        let payload = BrokerAuthorizationRequest {
            schema_version: 1,
            event: "authorize",
            capability: request.capability,
            connection_generation: request.connection_generation,
            helper_pid: request.helper_pid,
            ssh_parent_pid: request.ssh_parent_pid,
            owner_pid: self.owner_pid,
            prompt_kind: prompt_kind_name(request.prompt_kind),
            prompt_sha256: encoded_digest,
            prompt_bytes: request.prompt_bytes,
        };
        self.exchange(&payload)
    }

    fn complete(
        &self,
        request: &AuthorizationRequest<'_>,
        outcome: PromptCompletionOutcome,
    ) -> Result<(), AskpassError> {
        let encoded_digest = hex_digest(&request.prompt_sha256);
        let payload = BrokerCompletionRequest {
            schema_version: 1,
            event: "complete",
            capability: request.capability,
            connection_generation: request.connection_generation,
            helper_pid: request.helper_pid,
            ssh_parent_pid: request.ssh_parent_pid,
            owner_pid: self.owner_pid,
            prompt_kind: prompt_kind_name(request.prompt_kind),
            prompt_sha256: encoded_digest,
            prompt_bytes: request.prompt_bytes,
            outcome: completion_outcome_name(outcome),
        };
        self.exchange(&payload)
    }
}

impl BrokerAuthorizer {
    fn exchange(&self, payload: &impl Serialize) -> Result<(), AskpassError> {
        let mut stream = UnixStream::connect(&self.socket_path)
            .map_err(|_| AskpassError::AuthorizationDenied)?;
        stream
            .set_read_timeout(Some(BROKER_TIMEOUT))
            .and_then(|_| stream.set_write_timeout(Some(BROKER_TIMEOUT)))
            .map_err(|_| AskpassError::AuthorizationDenied)?;
        let mut encoded =
            serde_json::to_vec(payload).map_err(|_| AskpassError::AuthorizationDenied)?;
        if encoded.len() >= BROKER_RESPONSE_MAX_BYTES {
            encoded.fill(0);
            return Err(AskpassError::AuthorizationDenied);
        }
        let write_result = stream
            .write_all(&encoded)
            .and_then(|_| stream.write_all(b"\n"));
        encoded.fill(0);
        write_result.map_err(|_| AskpassError::AuthorizationDenied)?;

        let mut response = Vec::with_capacity(128);
        let mut byte = [0_u8; 1];
        loop {
            if response.len() >= BROKER_RESPONSE_MAX_BYTES {
                return Err(AskpassError::AuthorizationDenied);
            }
            let count = stream
                .read(&mut byte)
                .map_err(|_| AskpassError::AuthorizationDenied)?;
            if count == 0 {
                return Err(AskpassError::AuthorizationDenied);
            }
            if byte[0] == b'\n' {
                break;
            }
            response.push(byte[0]);
        }
        let decoded: BrokerAuthorizationResponse =
            serde_json::from_slice(&response).map_err(|_| AskpassError::AuthorizationDenied)?;
        if decoded.schema_version != 1 || !decoded.authorized {
            return Err(AskpassError::AuthorizationDenied);
        }
        Ok(())
    }
}

fn prompt_kind_name(kind: PromptKind) -> &'static str {
    match kind {
        PromptKind::Password => "password",
        PromptKind::Passphrase => "passphrase",
        PromptKind::HostConfirmation => "host_confirmation",
    }
}

fn completion_outcome_name(outcome: PromptCompletionOutcome) -> &'static str {
    match outcome {
        PromptCompletionOutcome::Accepted => "accepted",
        PromptCompletionOutcome::Rejected => "rejected",
        PromptCompletionOutcome::Cancelled => "cancelled",
    }
}

fn hex_digest(digest: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(64);
    for value in digest {
        output.push(HEX[(value >> 4) as usize] as char);
        output.push(HEX[(value & 0x0f) as usize] as char);
    }
    output
}

struct AppKitPromptDialog;

impl PromptDialog for AppKitPromptDialog {
    fn present(&self, prompt: &ClassifiedPrompt) -> Result<DialogOutcome, AskpassError> {
        #[cfg(target_os = "macos")]
        {
            appkit::present(prompt)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = prompt;
            Err(AskpassError::InvalidAuthority)
        }
    }
}

fn parse_positive_u32_environment(name: &str) -> Result<u32, AskpassError> {
    let value = std::env::var(name).map_err(|_| AskpassError::InvalidAuthority)?;
    if value.is_empty() || value.len() > 10 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(AskpassError::InvalidAuthority);
    }
    value
        .parse::<u32>()
        .ok()
        .filter(|parsed| *parsed > 1 && *parsed <= i32::MAX as u32)
        .ok_or(AskpassError::InvalidAuthority)
}

fn parse_positive_u64_environment(name: &str) -> Result<u64, AskpassError> {
    let value = std::env::var(name).map_err(|_| AskpassError::InvalidAuthority)?;
    if value.is_empty() || value.len() > 16 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(AskpassError::InvalidAuthority);
    }
    value
        .parse::<u64>()
        .ok()
        .filter(|parsed| *parsed > 0 && *parsed < (1_u64 << 53))
        .ok_or(AskpassError::InvalidAuthority)
}

fn native_askpass() -> Result<(), AskpassError> {
    let mut arguments = std::env::args_os();
    let _program = arguments.next().ok_or(AskpassError::InvalidPrompt)?;
    let prompt = arguments.next().ok_or(AskpassError::InvalidPrompt)?;
    if arguments.next().is_some() {
        return Err(AskpassError::InvalidPrompt);
    }
    let owner_pid = parse_positive_u32_environment(SSH_OWNER_PID_ENV)?;
    let connection_generation = parse_positive_u64_environment(CONNECTION_GENERATION_ENV)?;
    let capability = SecretText::from_environment(ASKPASS_CAPABILITY_ENV)?;
    let helper_pid = u32::try_from(unsafe { libc::getpid() })
        .ok()
        .filter(|pid| *pid > 1)
        .ok_or(AskpassError::InvalidAuthority)?;
    let inspector = DarwinProcessInspector;
    let authorizer = BrokerAuthorizer::from_environment(owner_pid)?;
    let dialog = AppKitPromptDialog;
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    execute_askpass(
        prompt.as_bytes(),
        helper_pid,
        owner_pid,
        connection_generation,
        capability.expose(),
        &inspector,
        &authorizer,
        &dialog,
        &mut output,
    )
}

pub fn run_native_askpass() -> i32 {
    if native_askpass().is_ok() {
        0
    } else {
        1
    }
}

#[cfg(target_os = "macos")]
mod appkit {
    use super::{AskpassError, ClassifiedPrompt, DialogOutcome, PromptKind, SecretResponse};
    use std::ffi::{c_char, c_void, CString};

    type ObjcId = *mut c_void;
    type ObjcSel = *mut c_void;

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Point {
        x: f64,
        y: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Size {
        width: f64,
        height: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Rect {
        origin: Point,
        size: Size,
    }

    #[link(name = "objc")]
    extern "C" {
        fn objc_getClass(name: *const c_char) -> ObjcId;
        fn sel_registerName(name: *const c_char) -> ObjcSel;
        fn objc_msgSend();
    }

    #[link(name = "AppKit", kind = "framework")]
    extern "C" {}

    #[link(name = "Foundation", kind = "framework")]
    extern "C" {}

    pub(super) fn present(prompt: &ClassifiedPrompt) -> Result<DialogOutcome, AskpassError> {
        unsafe { present_inner(prompt) }
    }

    unsafe fn present_inner(prompt: &ClassifiedPrompt) -> Result<DialogOutcome, AskpassError> {
        let pool = send_id(class("NSAutoreleasePool")?, selector("new")?);
        if pool.is_null() {
            return Err(AskpassError::InvalidResponse);
        }
        let result = (|| {
            let app = send_id(class("NSApplication")?, selector("sharedApplication")?);
            if app.is_null() {
                return Err(AskpassError::InvalidResponse);
            }
            if !send_bool_result_isize(app, selector("setActivationPolicy:")?, 1) {
                return Err(AskpassError::InvalidResponse);
            }
            send_void_bool(app, selector("activateIgnoringOtherApps:")?, true);
            let alert = send_id(class("NSAlert")?, selector("new")?);
            if alert.is_null() {
                return Err(AskpassError::InvalidResponse);
            }
            let result = configure_and_run_alert(alert, prompt);
            send_void(alert, selector("release")?);
            result
        })();
        send_void(pool, selector("drain")?);
        result
    }

    unsafe fn configure_and_run_alert(
        alert: ObjcId,
        prompt: &ClassifiedPrompt,
    ) -> Result<DialogOutcome, AskpassError> {
        let title = match prompt.kind {
            PromptKind::Password => "OpenEvo SSH password",
            PromptKind::Passphrase => "OpenEvo SSH key passphrase",
            PromptKind::HostConfirmation => "OpenEvo SSH host verification",
        };
        send_void_object(alert, selector("setMessageText:")?, ns_string(title)?);
        send_void_object(
            alert,
            selector("setInformativeText:")?,
            ns_string(&prompt.display_detail)?,
        );
        send_void_isize(alert, selector("setAlertStyle:")?, 1);
        match prompt.kind {
            PromptKind::Password | PromptKind::Passphrase => {
                send_object(
                    alert,
                    selector("addButtonWithTitle:")?,
                    ns_string("Continue")?,
                );
                send_object(
                    alert,
                    selector("addButtonWithTitle:")?,
                    ns_string("Cancel")?,
                );
                let field = send_rect(
                    send_id(class("NSSecureTextField")?, selector("alloc")?),
                    selector("initWithFrame:")?,
                    Rect {
                        origin: Point { x: 0.0, y: 0.0 },
                        size: Size {
                            width: 360.0,
                            height: 24.0,
                        },
                    },
                );
                if field.is_null() {
                    return Err(AskpassError::InvalidResponse);
                }
                send_void_object(alert, selector("setAccessoryView:")?, field);
                let window = send_id(alert, selector("window")?);
                if window.is_null()
                    || !send_bool_result_object(window, selector("makeFirstResponder:")?, field)
                {
                    send_void(field, selector("release")?);
                    return Err(AskpassError::InvalidResponse);
                }
                let response = send_isize_result(alert, selector("runModal")?);
                let outcome = if response == 1000 {
                    read_secure_field(field).map(DialogOutcome::Secret)
                } else {
                    Ok(DialogOutcome::Cancelled)
                };
                send_void(field, selector("release")?);
                outcome
            }
            PromptKind::HostConfirmation => {
                for label in ["Continue", "Reject", "Cancel"] {
                    send_object(alert, selector("addButtonWithTitle:")?, ns_string(label)?);
                }
                match send_isize_result(alert, selector("runModal")?) {
                    1000 => Ok(DialogOutcome::Confirm(true)),
                    1001 => Ok(DialogOutcome::Confirm(false)),
                    _ => Ok(DialogOutcome::Cancelled),
                }
            }
        }
    }

    unsafe fn read_secure_field(field: ObjcId) -> Result<SecretResponse, AskpassError> {
        let value = send_id(field, selector("stringValue")?);
        if value.is_null() {
            return Err(AskpassError::InvalidResponse);
        }
        let pointer = send_c_string(value, selector("UTF8String")?);
        if pointer.is_null() {
            return Err(AskpassError::InvalidResponse);
        }
        let bytes = std::ffi::CStr::from_ptr(pointer).to_bytes();
        if bytes.is_empty() || bytes.len() > super::MAX_SECRET_BYTES {
            return Err(AskpassError::InvalidResponse);
        }
        Ok(SecretResponse::new(bytes.to_vec()))
    }

    unsafe fn class(name: &str) -> Result<ObjcId, AskpassError> {
        let name = CString::new(name).map_err(|_| AskpassError::InvalidResponse)?;
        let class = objc_getClass(name.as_ptr());
        (!class.is_null())
            .then_some(class)
            .ok_or(AskpassError::InvalidResponse)
    }

    unsafe fn selector(name: &str) -> Result<ObjcSel, AskpassError> {
        let name = CString::new(name).map_err(|_| AskpassError::InvalidResponse)?;
        let selector = sel_registerName(name.as_ptr());
        (!selector.is_null())
            .then_some(selector)
            .ok_or(AskpassError::InvalidResponse)
    }

    unsafe fn ns_string(value: &str) -> Result<ObjcId, AskpassError> {
        let value = CString::new(value).map_err(|_| AskpassError::InvalidResponse)?;
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, *const c_char) -> ObjcId =
            std::mem::transmute(objc_msgSend as *const ());
        let string = function(
            class("NSString")?,
            selector("stringWithUTF8String:")?,
            value.as_ptr(),
        );
        (!string.is_null())
            .then_some(string)
            .ok_or(AskpassError::InvalidResponse)
    }

    unsafe fn send_id(receiver: ObjcId, selector: ObjcSel) -> ObjcId {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel) -> ObjcId =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector)
    }

    unsafe fn send_object(receiver: ObjcId, selector: ObjcSel, value: ObjcId) -> ObjcId {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, ObjcId) -> ObjcId =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value)
    }

    unsafe fn send_void_object(receiver: ObjcId, selector: ObjcSel, value: ObjcId) {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, ObjcId) =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value);
    }

    unsafe fn send_void(receiver: ObjcId, selector: ObjcSel) {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel) =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector);
    }

    unsafe fn send_void_bool(receiver: ObjcId, selector: ObjcSel, value: bool) {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, i8) =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, i8::from(value));
    }

    unsafe fn send_void_isize(receiver: ObjcId, selector: ObjcSel, value: isize) {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, isize) =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value);
    }

    unsafe fn send_bool_result_isize(receiver: ObjcId, selector: ObjcSel, value: isize) -> bool {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, isize) -> i8 =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value) != 0
    }

    unsafe fn send_bool_result_object(receiver: ObjcId, selector: ObjcSel, value: ObjcId) -> bool {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, ObjcId) -> i8 =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value) != 0
    }

    unsafe fn send_isize_result(receiver: ObjcId, selector: ObjcSel) -> isize {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel) -> isize =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector)
    }

    unsafe fn send_rect(receiver: ObjcId, selector: ObjcSel, value: Rect) -> ObjcId {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel, Rect) -> ObjcId =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector, value)
    }

    unsafe fn send_c_string(receiver: ObjcId, selector: ObjcSel) -> *const c_char {
        let function: unsafe extern "C" fn(ObjcId, ObjcSel) -> *const c_char =
            std::mem::transmute(objc_msgSend as *const ());
        function(receiver, selector)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    struct FakeInspector {
        parents: HashMap<u32, u32>,
        paths: HashMap<u32, PathBuf>,
    }

    impl ProcessInspector for FakeInspector {
        fn parent_pid(&self, pid: u32) -> Option<u32> {
            self.parents.get(&pid).copied()
        }

        fn executable_path(&self, pid: u32) -> Option<PathBuf> {
            self.paths.get(&pid).cloned()
        }
    }

    fn direct_inspector() -> FakeInspector {
        FakeInspector {
            parents: HashMap::from([(300, 200), (200, 1)]),
            paths: HashMap::from([(200, PathBuf::from(SYSTEM_SSH_PATH))]),
        }
    }

    struct OneUseAuthorizer {
        calls: AtomicUsize,
    }

    impl OneUseAuthorizer {
        fn new() -> Self {
            Self {
                calls: AtomicUsize::new(0),
            }
        }
    }

    impl PromptAuthorizer for OneUseAuthorizer {
        fn authorize(&self, request: &AuthorizationRequest<'_>) -> Result<(), AskpassError> {
            assert_eq!(request.capability, "a".repeat(64));
            if self.calls.fetch_add(1, Ordering::SeqCst) == 0 {
                Ok(())
            } else {
                Err(AskpassError::AuthorizationDenied)
            }
        }
    }

    struct FakeDialog {
        outcomes: Mutex<Vec<DialogOutcome>>,
        calls: AtomicUsize,
    }

    impl FakeDialog {
        fn new(outcomes: Vec<DialogOutcome>) -> Self {
            Self {
                outcomes: Mutex::new(outcomes.into_iter().rev().collect()),
                calls: AtomicUsize::new(0),
            }
        }
    }

    impl PromptDialog for FakeDialog {
        fn present(&self, _prompt: &ClassifiedPrompt) -> Result<DialogOutcome, AskpassError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.outcomes
                .lock()
                .unwrap()
                .pop()
                .ok_or(AskpassError::InvalidResponse)
        }
    }

    #[test]
    fn classifies_closed_prompt_kinds_without_retaining_sensitive_prompt_text() {
        let password = classify_prompt(b"alice@lab's password: ").unwrap();
        assert_eq!(password.kind, PromptKind::Password);
        assert!(!password.display_detail.contains("alice"));

        let passphrase =
            classify_prompt(b"Enter passphrase for key '/Users/alice/.ssh/id_ed25519': ").unwrap();
        assert_eq!(passphrase.kind, PromptKind::Passphrase);
        assert!(!passphrase.display_detail.contains("/Users"));

        let confirmation = classify_prompt(
            b"The authenticity of host 'lab.example (10.0.0.2)' can't be established.\n\
              ED25519 key fingerprint is SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.\n\
              Are you sure you want to continue connecting (yes/no/[fingerprint])?",
        )
        .unwrap();
        assert_eq!(confirmation.kind, PromptKind::HostConfirmation);
        assert!(confirmation.display_detail.contains("lab.example"));
        assert!(confirmation.display_detail.contains("SHA256:"));
    }

    #[test]
    fn rejects_unknown_malformed_and_oversized_prompts() {
        assert_eq!(
            classify_prompt(b"unexpected helper question"),
            Err(AskpassError::InvalidPrompt)
        );
        assert_eq!(
            classify_prompt(&vec![b'x'; MAX_PROMPT_BYTES + 1]),
            Err(AskpassError::InvalidPrompt)
        );
        assert_eq!(
            classify_prompt(b"password: \0hidden"),
            Err(AskpassError::InvalidPrompt)
        );
        assert_eq!(
            classify_prompt(b"password: \xff"),
            Err(AskpassError::InvalidPrompt)
        );
    }

    #[test]
    fn validates_direct_and_proxyjump_descendant_process_shapes() {
        assert_eq!(
            validate_ssh_process_chain(&direct_inspector(), 300, 200),
            Ok(200)
        );

        let proxy = FakeInspector {
            parents: HashMap::from([(500, 410), (410, 405), (405, 400), (400, 1)]),
            paths: HashMap::from([
                (410, PathBuf::from(SYSTEM_SSH_PATH)),
                (405, PathBuf::from("/bin/sh")),
                (400, PathBuf::from(SYSTEM_SSH_PATH)),
            ]),
        };
        assert_eq!(validate_ssh_process_chain(&proxy, 500, 400), Ok(410));
    }

    #[test]
    fn rejects_wrong_direct_parent_missing_owner_and_process_cycles() {
        let wrong_parent = FakeInspector {
            parents: HashMap::from([(300, 200), (200, 1)]),
            paths: HashMap::from([(200, PathBuf::from("/tmp/fake-ssh"))]),
        };
        assert_eq!(
            validate_ssh_process_chain(&wrong_parent, 300, 200),
            Err(AskpassError::InvalidAuthority)
        );
        assert_eq!(
            validate_ssh_process_chain(&direct_inspector(), 300, 999),
            Err(AskpassError::InvalidAuthority)
        );
        let cycle = FakeInspector {
            parents: HashMap::from([(300, 200), (200, 201), (201, 200)]),
            paths: HashMap::from([(200, PathBuf::from(SYSTEM_SSH_PATH))]),
        };
        assert_eq!(
            validate_ssh_process_chain(&cycle, 300, 999),
            Err(AskpassError::InvalidAuthority)
        );
    }

    fn execute_with(
        authorizer: &dyn PromptAuthorizer,
        dialog: &dyn PromptDialog,
        output: &mut Vec<u8>,
    ) -> Result<(), AskpassError> {
        execute_askpass(
            b"password: ",
            300,
            200,
            7,
            &"a".repeat(64),
            &direct_inspector(),
            authorizer,
            dialog,
            output,
        )
    }

    #[test]
    fn writes_secure_secret_only_to_askpass_stdout() {
        let secret = b"never-log-this-password".to_vec();
        let response = SecretResponse::new(secret.clone());
        assert_eq!(format!("{response:?}"), "SecretResponse(<redacted>)");
        let authorizer = OneUseAuthorizer::new();
        let dialog = FakeDialog::new(vec![DialogOutcome::Secret(response)]);
        let mut output = Vec::new();

        execute_with(&authorizer, &dialog, &mut output).unwrap();

        assert_eq!(output, [secret, b"\n".to_vec()].concat());
    }

    #[test]
    fn confirmation_writes_exact_yes_or_no_and_cancellation_writes_nothing() {
        for (outcome, expected) in [
            (DialogOutcome::Confirm(true), b"yes\n".as_slice()),
            (DialogOutcome::Confirm(false), b"no\n".as_slice()),
        ] {
            let authorizer = OneUseAuthorizer::new();
            let dialog = FakeDialog::new(vec![outcome]);
            let mut output = Vec::new();
            execute_askpass(
                b"The authenticity of host 'lab' can't be established.\n\
                  ED25519 key fingerprint is SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.\n\
                  Are you sure you want to continue connecting (yes/no/[fingerprint])?",
                300,
                200,
                7,
                &"a".repeat(64),
                &direct_inspector(),
                &authorizer,
                &dialog,
                &mut output,
            )
            .unwrap();
            assert_eq!(output, expected);
        }

        let authorizer = OneUseAuthorizer::new();
        let dialog = FakeDialog::new(vec![DialogOutcome::Cancelled]);
        let mut output = Vec::new();
        assert_eq!(
            execute_with(&authorizer, &dialog, &mut output),
            Err(AskpassError::Cancelled)
        );
        assert!(output.is_empty());
    }

    #[test]
    fn reports_bounded_prompt_completion_before_releasing_any_response() {
        struct CompletionAuthorizer {
            completions: Mutex<Vec<PromptCompletionOutcome>>,
            deny_completion: bool,
        }

        impl PromptAuthorizer for CompletionAuthorizer {
            fn authorize(&self, _request: &AuthorizationRequest<'_>) -> Result<(), AskpassError> {
                Ok(())
            }

            fn complete(
                &self,
                _request: &AuthorizationRequest<'_>,
                outcome: PromptCompletionOutcome,
            ) -> Result<(), AskpassError> {
                self.completions.lock().unwrap().push(outcome);
                if self.deny_completion {
                    Err(AskpassError::AuthorizationDenied)
                } else {
                    Ok(())
                }
            }
        }

        let accepted = CompletionAuthorizer {
            completions: Mutex::new(Vec::new()),
            deny_completion: false,
        };
        let dialog = FakeDialog::new(vec![DialogOutcome::Secret(SecretResponse::new(
            b"secret".to_vec(),
        ))]);
        let mut output = Vec::new();
        execute_with(&accepted, &dialog, &mut output).unwrap();
        assert_eq!(
            *accepted.completions.lock().unwrap(),
            vec![PromptCompletionOutcome::Accepted]
        );
        assert_eq!(output, b"secret\n");

        let cancelled = CompletionAuthorizer {
            completions: Mutex::new(Vec::new()),
            deny_completion: false,
        };
        let dialog = FakeDialog::new(vec![DialogOutcome::Cancelled]);
        let mut output = Vec::new();
        assert_eq!(
            execute_with(&cancelled, &dialog, &mut output),
            Err(AskpassError::Cancelled)
        );
        assert_eq!(
            *cancelled.completions.lock().unwrap(),
            vec![PromptCompletionOutcome::Cancelled]
        );
        assert!(output.is_empty());

        let denied = CompletionAuthorizer {
            completions: Mutex::new(Vec::new()),
            deny_completion: true,
        };
        let dialog = FakeDialog::new(vec![DialogOutcome::Secret(SecretResponse::new(
            b"must-not-leave".to_vec(),
        ))]);
        let mut output = Vec::new();
        assert_eq!(
            execute_with(&denied, &dialog, &mut output),
            Err(AskpassError::AuthorizationDenied)
        );
        assert!(output.is_empty());
    }

    #[test]
    fn one_use_authorization_blocks_repeated_prompt_before_dialog() {
        let authorizer = OneUseAuthorizer::new();
        let dialog = FakeDialog::new(vec![
            DialogOutcome::Secret(SecretResponse::new(b"first".to_vec())),
            DialogOutcome::Secret(SecretResponse::new(b"second".to_vec())),
        ]);
        let mut first = Vec::new();
        execute_with(&authorizer, &dialog, &mut first).unwrap();
        let mut second = Vec::new();
        assert_eq!(
            execute_with(&authorizer, &dialog, &mut second),
            Err(AskpassError::AuthorizationDenied)
        );
        assert_eq!(dialog.calls.load(Ordering::SeqCst), 1);
        assert!(second.is_empty());
    }

    #[test]
    fn rejects_invalid_generation_capability_and_secret_response_bytes() {
        let authorizer = OneUseAuthorizer::new();
        let dialog = FakeDialog::new(vec![DialogOutcome::Secret(SecretResponse::new(
            b"unused".to_vec(),
        ))]);
        let mut output = Vec::new();
        assert_eq!(
            execute_askpass(
                b"password: ",
                300,
                200,
                0,
                &"a".repeat(64),
                &direct_inspector(),
                &authorizer,
                &dialog,
                &mut output,
            ),
            Err(AskpassError::InvalidAuthority)
        );
        assert_eq!(authorizer.calls.load(Ordering::SeqCst), 0);
        assert_eq!(dialog.calls.load(Ordering::SeqCst), 0);

        for capability in ["a".repeat(63), "A".repeat(64), "g".repeat(64)] {
            let mut output = Vec::new();
            assert_eq!(
                execute_askpass(
                    b"password: ",
                    300,
                    200,
                    7,
                    &capability,
                    &direct_inspector(),
                    &authorizer,
                    &dialog,
                    &mut output,
                ),
                Err(AskpassError::InvalidAuthority)
            );
            assert!(output.is_empty());
        }

        for secret in [
            Vec::new(),
            vec![b'x'; MAX_SECRET_BYTES + 1],
            b"line\nbreak".to_vec(),
            b"carriage\rreturn".to_vec(),
            b"nul\0byte".to_vec(),
        ] {
            let authorizer = OneUseAuthorizer::new();
            let dialog = FakeDialog::new(vec![DialogOutcome::Secret(SecretResponse::new(secret))]);
            let mut output = Vec::new();
            assert_eq!(
                execute_with(&authorizer, &dialog, &mut output),
                Err(AskpassError::InvalidResponse)
            );
            assert!(output.is_empty());
        }
    }

    #[test]
    fn passes_exact_connection_generation_and_prompt_identity_to_authorizer() {
        type AuthorizationObservation = (u64, PromptKind, usize, [u8; 32]);
        struct RecordingAuthorizer(Mutex<Vec<AuthorizationObservation>>);

        impl PromptAuthorizer for RecordingAuthorizer {
            fn authorize(&self, request: &AuthorizationRequest<'_>) -> Result<(), AskpassError> {
                self.0.lock().unwrap().push((
                    request.connection_generation,
                    request.prompt_kind,
                    request.prompt_bytes,
                    request.prompt_sha256,
                ));
                Ok(())
            }
        }

        let prompt = b"password: ";
        let authorizer = RecordingAuthorizer(Mutex::new(Vec::new()));
        let dialog = FakeDialog::new(vec![DialogOutcome::Secret(SecretResponse::new(
            b"secret".to_vec(),
        ))]);
        let mut output = Vec::new();
        execute_askpass(
            prompt,
            300,
            200,
            91,
            &"a".repeat(64),
            &direct_inspector(),
            &authorizer,
            &dialog,
            &mut output,
        )
        .unwrap();

        let observed = authorizer.0.lock().unwrap();
        assert_eq!(observed.len(), 1);
        assert_eq!(observed[0].0, 91);
        assert_eq!(observed[0].1, PromptKind::Password);
        assert_eq!(observed[0].2, prompt.len());
        assert_eq!(observed[0].3.as_slice(), Sha256::digest(prompt).as_slice());
    }

    #[test]
    fn concurrent_reuse_allows_exactly_one_dialog_and_response() {
        let authorizer = Arc::new(OneUseAuthorizer::new());
        let dialog = Arc::new(FakeDialog::new(vec![
            DialogOutcome::Secret(SecretResponse::new(b"first".to_vec())),
            DialogOutcome::Secret(SecretResponse::new(b"second".to_vec())),
        ]));
        let results = std::thread::scope(|scope| {
            let mut handles = Vec::new();
            for _ in 0..2 {
                let authorizer = Arc::clone(&authorizer);
                let dialog = Arc::clone(&dialog);
                handles.push(scope.spawn(move || {
                    let mut output = Vec::new();
                    let result = execute_with(&*authorizer, &*dialog, &mut output);
                    (result, output)
                }));
            }
            handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .collect::<Vec<_>>()
        });

        assert_eq!(
            results.iter().filter(|(result, _)| result.is_ok()).count(),
            1
        );
        assert_eq!(
            results
                .iter()
                .filter(|(result, output)| {
                    *result == Err(AskpassError::AuthorizationDenied) && output.is_empty()
                })
                .count(),
            1
        );
        assert_eq!(dialog.calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn broker_payload_is_closed_and_never_contains_prompt_or_response_text() {
        let directory = tempfile::tempdir().unwrap();
        let socket_path = directory.path().join("broker.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut request)
                .unwrap();
            stream
                .write_all(b"{\"schema_version\":1,\"authorized\":true}\n")
                .unwrap();
            request
        });
        let prompt = b"alice@private.example's password: ";
        let digest = Sha256::digest(prompt);
        let mut prompt_sha256 = [0_u8; 32];
        prompt_sha256.copy_from_slice(&digest);
        let capability = "a".repeat(64);
        let authorizer = BrokerAuthorizer {
            socket_path,
            owner_pid: 200,
        };

        authorizer
            .authorize(&AuthorizationRequest {
                capability: &capability,
                connection_generation: 91,
                helper_pid: 300,
                ssh_parent_pid: 250,
                prompt_kind: PromptKind::Password,
                prompt_sha256,
                prompt_bytes: prompt.len(),
            })
            .unwrap();

        let request = server.join().unwrap();
        let value: serde_json::Value = serde_json::from_str(&request).unwrap();
        let keys = value
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<HashSet<_>>();
        assert_eq!(
            keys,
            HashSet::from([
                "capability".to_string(),
                "connection_generation".to_string(),
                "event".to_string(),
                "helper_pid".to_string(),
                "owner_pid".to_string(),
                "prompt_bytes".to_string(),
                "prompt_kind".to_string(),
                "prompt_sha256".to_string(),
                "schema_version".to_string(),
                "ssh_parent_pid".to_string(),
            ])
        );
        assert!(!request.contains("alice"));
        assert!(!request.contains("private.example"));
        assert!(!request.contains("never-send-an-ssh-secret"));
    }

    #[test]
    fn broker_completion_payload_contains_only_prompt_identity_and_outcome() {
        let directory = tempfile::tempdir().unwrap();
        let socket_path = directory.path().join("broker.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let server = std::thread::spawn(move || {
            let mut requests = Vec::new();
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = String::new();
                BufReader::new(stream.try_clone().unwrap())
                    .read_line(&mut request)
                    .unwrap();
                stream
                    .write_all(b"{\"schema_version\":1,\"authorized\":true}\n")
                    .unwrap();
                requests.push(request);
            }
            requests
        });
        let prompt = b"password: ";
        let digest = Sha256::digest(prompt);
        let mut prompt_sha256 = [0_u8; 32];
        prompt_sha256.copy_from_slice(&digest);
        let capability = "b".repeat(64);
        let authorizer = BrokerAuthorizer {
            socket_path,
            owner_pid: 200,
        };
        let request = AuthorizationRequest {
            capability: &capability,
            connection_generation: 92,
            helper_pid: 300,
            ssh_parent_pid: 250,
            prompt_kind: PromptKind::Password,
            prompt_sha256,
            prompt_bytes: prompt.len(),
        };

        authorizer.authorize(&request).unwrap();
        authorizer
            .complete(&request, PromptCompletionOutcome::Cancelled)
            .unwrap();

        let requests = server.join().unwrap();
        let authorization: serde_json::Value = serde_json::from_str(&requests[0]).unwrap();
        let completion: serde_json::Value = serde_json::from_str(&requests[1]).unwrap();
        assert_eq!(authorization["event"], "authorize");
        assert_eq!(completion["event"], "complete");
        assert_eq!(completion["outcome"], "cancelled");
        assert_eq!(completion.as_object().unwrap().len(), 11);
        assert!(!requests[1].contains("password:"));
        assert!(!requests[1].contains("response"));
        assert!(!requests[1].contains("secret"));
    }
}
