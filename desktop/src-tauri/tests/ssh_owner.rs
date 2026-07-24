#![cfg(unix)]

use std::fs::File;
use std::os::fd::AsRawFd;
use std::process::Command;

const SSH_PATH: &str = "/usr/bin/ssh";
const OWNER_ARGUMENT: &str = "--openevo-system-ssh-owner-v1";

fn owner_command(ssh: &File) -> Command {
    let helper = env!("CARGO_BIN_EXE_openevo-ssh-askpass");
    let mut command = Command::new(helper);
    command
        .arg(OWNER_ARGUMENT)
        .arg(ssh.as_raw_fd().to_string())
        .arg(SSH_PATH)
        .arg("-V")
        .env_clear()
        .env("DISPLAY", "openevo-ssh-askpass")
        .env("HOME", "/private/tmp")
        .env("OPENEVO_SSH_ASKPASS_CAPABILITY", "a".repeat(64))
        .env(
            "OPENEVO_SSH_ASKPASS_SOCKET",
            "/private/tmp/openevo-test-broker",
        )
        .env("OPENEVO_SSH_CONNECTION_GENERATION", "1")
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .env("SSH_ASKPASS", helper)
        .env("SSH_ASKPASS_REQUIRE", "force");
    command
}

#[test]
fn native_owner_execs_the_exact_held_system_ssh_without_python() {
    let ssh = File::open(SSH_PATH).unwrap();
    let descriptor = ssh.as_raw_fd();
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert!(flags >= 0);
    assert_eq!(
        unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags & !libc::FD_CLOEXEC) },
        0
    );

    let output = owner_command(&ssh).output().unwrap();

    assert!(output.status.success());
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8(output.stderr)
        .unwrap()
        .starts_with("OpenSSH_"));
}

#[test]
fn native_owner_rejects_an_open_environment_without_emitting_details() {
    let ssh = File::open(SSH_PATH).unwrap();
    let descriptor = ssh.as_raw_fd();
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert!(flags >= 0);
    assert_eq!(
        unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags & !libc::FD_CLOEXEC) },
        0
    );
    let output = owner_command(&ssh)
        .env("AWS_SECRET_ACCESS_KEY", "must-not-leak")
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(126));
    assert!(output.stdout.is_empty());
    assert!(output.stderr.is_empty());
}
