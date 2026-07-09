use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::Manager;

const BUNDLED_SIDECAR_BINARY: &str = "openevo-desktop-sidecar";
const SIDECAR_STARTUP_TIMEOUT: Duration = Duration::from_secs(15);
const SIDECAR_HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(100);
const SIDECAR_HEALTH_CONNECT_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Clone, serde::Serialize)]
struct SidecarStatus {
    state: String,
    port: Option<u16>,
    pid: Option<u32>,
    url: Option<String>,
    command: Option<String>,
}

#[derive(Clone, serde::Serialize)]
struct TunnelStatus {
    id: String,
    local_port: u16,
    remote_host: String,
    remote_port: u16,
    state: String,
}

#[derive(Clone, serde::Serialize)]
struct KeychainReference {
    service: String,
    account: String,
}

struct ManagedSidecar {
    status: SidecarStatus,
    child: Child,
}

#[derive(Default)]
struct DesktopHostState {
    sidecar: Mutex<Option<ManagedSidecar>>,
    tunnels: Mutex<Vec<TunnelStatus>>,
    logs: Arc<Mutex<Vec<String>>>,
}

fn allocate_port() -> Result<u16, String> {
    TcpListener::bind("127.0.0.1:0")
        .map_err(|error| format!("failed to allocate local port: {error}"))?
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|error| format!("failed to read allocated port: {error}"))
}

fn stopped_sidecar_status() -> SidecarStatus {
    SidecarStatus {
        state: "stopped".to_string(),
        port: None,
        pid: None,
        url: None,
        command: None,
    }
}

fn bundled_sidecar_path(app: Option<&tauri::AppHandle>) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(app) = app {
        if let Ok(resource_dir) = app.path().resource_dir() {
            candidates.push(resource_dir.join(BUNDLED_SIDECAR_BINARY));
            candidates.push(resource_dir.join("binaries").join(BUNDLED_SIDECAR_BINARY));
        }
    }
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(exe_dir.join(BUNDLED_SIDECAR_BINARY));
        }
    }
    candidates.into_iter().find(|path| path.is_file())
}

fn sidecar_command(app: Option<&tauri::AppHandle>, port: u16) -> (String, Vec<String>) {
    if let Ok(command_line) = std::env::var("OPENEVO_DESKTOP_SIDECAR_COMMAND") {
        return (
            "sh".to_string(),
            vec![
                "-c".to_string(),
                command_line.replace("{port}", &port.to_string()),
            ],
        );
    }
    if let Some(sidecar_path) = bundled_sidecar_path(app) {
        return (
            sidecar_path.to_string_lossy().into_owned(),
            vec![
                "--host".to_string(),
                "127.0.0.1".to_string(),
                "--port".to_string(),
                port.to_string(),
            ],
        );
    }
    (
        "python3".to_string(),
        vec![
            "-m".to_string(),
            "desktop.server.launcher".to_string(),
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            port.to_string(),
        ],
    )
}

fn check_sidecar_health(port: u16) -> Result<(), String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, SIDECAR_HEALTH_CONNECT_TIMEOUT)
        .map_err(|error| format!("health connect failed: {error}"))?;
    stream
        .set_read_timeout(Some(SIDECAR_HEALTH_CONNECT_TIMEOUT))
        .map_err(|error| format!("failed to set health read timeout: {error}"))?;
    stream
        .set_write_timeout(Some(SIDECAR_HEALTH_CONNECT_TIMEOUT))
        .map_err(|error| format!("failed to set health write timeout: {error}"))?;
    stream
        .write_all(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .map_err(|error| format!("health request failed: {error}"))?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| format!("health response failed: {error}"))?;
    if response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200") {
        Ok(())
    } else {
        Err("health endpoint did not return HTTP 200".to_string())
    }
}

fn wait_for_sidecar_ready(
    child: &mut Child,
    port: u16,
    logs: &Arc<Mutex<Vec<String>>>,
    display: &str,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(exit_status) = child
            .try_wait()
            .map_err(|error| format!("failed to inspect sidecar process: {error}"))?
        {
            let message =
                format!("sidecar `{display}` exited during startup with status {exit_status}");
            push_log_to(logs, &message)?;
            return Err(message);
        }
        let health_error = match check_sidecar_health(port) {
            Ok(()) => {
                push_log_to(logs, format!("sidecar `{display}` health check passed"))?;
                return Ok(());
            }
            Err(error) => error,
        };
        if Instant::now() >= deadline {
            let message = format!(
                "OpenEvo Desktop sidecar did not become ready within {}s: {health_error}",
                timeout.as_secs()
            );
            push_log_to(logs, &message)?;
            if let Err(error) = child.kill() {
                push_log_to(
                    logs,
                    format!("failed to stop unready sidecar `{display}`: {error}"),
                )?;
            } else {
                let _ = child.wait();
            }
            return Err(message);
        }
        thread::sleep(SIDECAR_HEALTH_POLL_INTERVAL);
    }
}

fn command_display(program: &str, args: &[String]) -> String {
    if args.is_empty() {
        return program.to_string();
    }
    format!("{program} {}", args.join(" "))
}

fn push_log(state: &DesktopHostState, message: impl Into<String>) -> Result<(), String> {
    push_log_to(&state.logs, message)
}

fn push_log_to(logs: &Arc<Mutex<Vec<String>>>, message: impl Into<String>) -> Result<(), String> {
    logs.lock()
        .map_err(|_| "log state lock poisoned".to_string())?
        .push(message.into());
    Ok(())
}

fn capture_process_output(
    logs: Arc<Mutex<Vec<String>>>,
    label: &'static str,
    stream: impl Read + Send + 'static,
) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            match line {
                Ok(line) => {
                    let _ = push_log_to(&logs, format!("sidecar {label}: {line}"));
                }
                Err(error) => {
                    let _ = push_log_to(&logs, format!("failed to read sidecar {label}: {error}"));
                    break;
                }
            }
        }
    });
}

#[tauri::command]
fn host_status(state: tauri::State<'_, DesktopHostState>) -> Result<SidecarStatus, String> {
    let mut sidecar = state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())?;
    let Some(managed) = sidecar.as_mut() else {
        return Ok(stopped_sidecar_status());
    };
    match managed.child.try_wait() {
        Ok(Some(exit_status)) => {
            let mut status = managed.status.clone();
            status.state = "exited".to_string();
            status.url = None;
            let command = status
                .command
                .clone()
                .unwrap_or_else(|| "sidecar".to_string());
            *sidecar = None;
            drop(sidecar);
            push_log(
                &state,
                format!("{command} exited with status {exit_status}"),
            )?;
            Ok(status)
        }
        Ok(None) => Ok(managed.status.clone()),
        Err(error) => Err(format!("failed to inspect sidecar process: {error}")),
    }
}

#[tauri::command]
fn start_sidecar(
    app: tauri::AppHandle,
    state: tauri::State<'_, DesktopHostState>,
) -> Result<SidecarStatus, String> {
    let mut sidecar = state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())?;
    if let Some(managed) = sidecar.as_mut() {
        match managed.child.try_wait() {
            Ok(None) => return Ok(managed.status.clone()),
            Ok(Some(exit_status)) => {
                let command = managed
                    .status
                    .command
                    .clone()
                    .unwrap_or_else(|| "sidecar".to_string());
                *sidecar = None;
                push_log(
                    &state,
                    format!("{command} exited before restart with status {exit_status}"),
                )?;
            }
            Err(error) => return Err(format!("failed to inspect sidecar process: {error}")),
        }
    }

    let port = allocate_port()?;
    let (program, args) = sidecar_command(Some(&app), port);
    let display = command_display(&program, &args);
    let mut command = Command::new(&program);
    command
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Ok(workdir) = std::env::var("OPENEVO_DESKTOP_SIDECAR_WORKDIR") {
        command.current_dir(workdir);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start OpenEvo Desktop sidecar: {error}"))?;
    if let Some(stdout) = child.stdout.take() {
        capture_process_output(state.logs.clone(), "stdout", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        capture_process_output(state.logs.clone(), "stderr", stderr);
    }

    wait_for_sidecar_ready(
        &mut child,
        port,
        &state.logs,
        &display,
        SIDECAR_STARTUP_TIMEOUT,
    )?;

    let status = SidecarStatus {
        state: "running".to_string(),
        port: Some(port),
        pid: Some(child.id()),
        url: Some(format!("http://127.0.0.1:{port}/openevo")),
        command: Some(display.clone()),
    };
    *sidecar = Some(ManagedSidecar {
        status: status.clone(),
        child,
    });
    push_log(
        &state,
        format!("started sidecar `{display}` on port {port}"),
    )?;
    Ok(status)
}

#[tauri::command]
fn stop_sidecar(state: tauri::State<'_, DesktopHostState>) -> Result<SidecarStatus, String> {
    let mut sidecar = state
        .sidecar
        .lock()
        .map_err(|_| "sidecar state lock poisoned".to_string())?;
    let Some(managed) = sidecar.as_mut() else {
        return Ok(stopped_sidecar_status());
    };
    let command = managed
        .status
        .command
        .clone()
        .unwrap_or_else(|| "sidecar".to_string());
    match managed.child.try_wait() {
        Ok(Some(exit_status)) => {
            push_log(
                &state,
                format!("sidecar `{command}` already exited with status {exit_status}"),
            )?;
            *sidecar = None;
        }
        Ok(None) => {
            if let Err(error) = managed.child.kill() {
                push_log(&state, format!("failed to stop `{command}`: {error}"))?;
                return Err(format!("failed to stop sidecar process: {error}"));
            }
            match managed.child.wait() {
                Ok(exit_status) => {
                    push_log(
                        &state,
                        format!("stopped sidecar `{command}` with status {exit_status}"),
                    )?;
                    *sidecar = None;
                }
                Err(error) => {
                    push_log(
                        &state,
                        format!("failed to wait for stopped sidecar `{command}`: {error}"),
                    )?;
                    return Err(format!("failed to wait for sidecar process: {error}"));
                }
            }
        }
        Err(error) => return Err(format!("failed to inspect sidecar process: {error}")),
    }
    Ok(stopped_sidecar_status())
}

#[tauri::command]
fn create_ssh_tunnel(
    state: tauri::State<'_, DesktopHostState>,
    remote_host: String,
    remote_port: u16,
) -> Result<TunnelStatus, String> {
    let remote_host = remote_host.trim().to_string();
    if remote_host.is_empty() {
        return Err("remote_host must be a non-empty string".to_string());
    }
    if remote_port == 0 {
        return Err("remote_port must be greater than zero".to_string());
    }
    let tunnel = TunnelStatus {
        id: format!("{remote_host}:{remote_port}"),
        local_port: allocate_port()?,
        remote_host,
        remote_port,
        state: "reserved".to_string(),
    };
    state
        .tunnels
        .lock()
        .map_err(|_| "tunnel state lock poisoned".to_string())?
        .push(tunnel.clone());
    Ok(tunnel)
}

#[tauri::command]
fn keychain_reference(service: String, account: String) -> KeychainReference {
    KeychainReference { service, account }
}

#[tauri::command]
fn app_logs(state: tauri::State<'_, DesktopHostState>) -> Result<Vec<String>, String> {
    Ok(state
        .logs
        .lock()
        .map_err(|_| "log state lock poisoned".to_string())?
        .clone())
}

fn main() {
    tauri::Builder::default()
        .manage(DesktopHostState::default())
        .invoke_handler(tauri::generate_handler![
            host_status,
            start_sidecar,
            stop_sidecar,
            create_ssh_tunnel,
            keychain_reference,
            app_logs,
        ])
        .run(tauri::generate_context!())
        .expect("error while running OpenEvo Desktop");
}

#[cfg(test)]
mod tests {
    use super::{
        allocate_port, check_sidecar_health, sidecar_command, wait_for_sidecar_ready,
        SIDECAR_HEALTH_CONNECT_TIMEOUT,
    };
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::Duration;

    #[test]
    fn allocate_port_returns_non_zero_port() {
        assert!(allocate_port().unwrap() > 0);
    }

    #[test]
    fn sidecar_command_targets_local_launcher() {
        let (program, args) = sidecar_command(None, 49152);

        assert_eq!(program, "python3");
        assert_eq!(
            args,
            vec![
                "-m",
                "desktop.server.launcher",
                "--host",
                "127.0.0.1",
                "--port",
                "49152",
            ]
        );
    }

    #[test]
    fn sidecar_command_allows_env_override() {
        std::env::set_var("OPENEVO_DESKTOP_SIDECAR_COMMAND", "custom --port {port}");

        let (program, args) = sidecar_command(None, 49153);

        std::env::remove_var("OPENEVO_DESKTOP_SIDECAR_COMMAND");
        assert_eq!(program, "sh");
        assert_eq!(args, vec!["-c", "custom --port 49153"]);
    }

    #[test]
    fn check_sidecar_health_accepts_http_200() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 128];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                .unwrap();
        });

        assert!(check_sidecar_health(port).is_ok());
    }

    #[test]
    fn wait_for_sidecar_ready_waits_for_delayed_health_server() {
        let port = allocate_port().unwrap();
        thread::spawn(move || {
            thread::sleep(SIDECAR_HEALTH_CONNECT_TIMEOUT * 2);
            let listener = TcpListener::bind(("127.0.0.1", port)).unwrap();
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 128];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                .unwrap();
        });
        let mut child = Command::new("sh")
            .args(["-c", "sleep 5"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let logs = Arc::new(Mutex::new(Vec::new()));

        let result = wait_for_sidecar_ready(
            &mut child,
            port,
            &logs,
            "test-sidecar",
            Duration::from_secs(2),
        );
        let _ = child.kill();
        let _ = child.wait();

        assert!(result.is_ok());
    }
}
