use std::ffi::CString;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use rand::rngs::OsRng;
use rand::RngCore;

#[cfg(target_os = "macos")]
use super::macos_trusted_path_alias;
use super::{
    encode_hex, file_identity, file_identity_from_stat, open_directory, openat_file,
    validate_anchored_extended_acl, FileIdentity, HostResult, NativeHostError, DIRECTORY_FILE_TYPE,
    FILE_TYPE_MASK, REGULAR_FILE_TYPE, STICKY_MODE_BIT,
};

type ErrorFactory = fn() -> NativeHostError;

#[derive(Clone, Copy)]
pub(crate) struct PrivateJsonJournalPolicy {
    pub(crate) file_name: &'static str,
    pub(crate) temp_prefix: &'static str,
    pub(crate) lock_file_name: &'static str,
    pub(crate) max_bytes: usize,
    pub(crate) lock_timeout: Duration,
    pub(crate) lock_poll_interval: Duration,
    pub(crate) unavailable_error: ErrorFactory,
    pub(crate) too_large_error: ErrorFactory,
    pub(crate) conflict_error: ErrorFactory,
}

#[derive(Clone, Copy)]
pub(crate) struct PrivateJsonJournal {
    policy: PrivateJsonJournalPolicy,
}

pub(crate) struct PrivateJsonJournalRoot {
    journal: PrivateJsonJournal,
    path: PathBuf,
    directory: File,
    device: u64,
    inode: u64,
}

struct PrivateJsonJournalProcessLock {
    root: PrivateJsonJournalRoot,
    name: CString,
    file: File,
    identity: FileIdentity,
}

struct PrivateJsonJournalTemp<'a> {
    journal: PrivateJsonJournal,
    directory: &'a File,
    name: CString,
    file: File,
    published: bool,
}

impl PrivateJsonJournal {
    pub(crate) const fn new(policy: PrivateJsonJournalPolicy) -> Self {
        Self { policy }
    }

    pub(crate) fn read(&self, path: &Path) -> HostResult<Option<String>> {
        let Some(root) = self.open_root(path, false)? else {
            return Ok(None);
        };
        self.read_from_root(&root, None)
    }

    pub(crate) fn write(&self, path: &Path, value: Option<&str>) -> HostResult<()> {
        match value {
            Some(value) => self.write_some(path, value),
            None => self.clear(path),
        }
    }

    pub(crate) fn write_some_with<F>(
        &self,
        path: &Path,
        value: &str,
        after_directory_sync: F,
    ) -> HostResult<()>
    where
        F: FnOnce(&mut File) -> HostResult<()>,
    {
        if value.len() > self.policy.max_bytes {
            return Err(self.too_large_error());
        }
        let root = self
            .open_root(path, true)?
            .ok_or_else(|| self.unavailable_error())?;
        root.validate()?;
        let target_name = self.target_name()?;
        let previous_identity = self.identity_at_optional(&root.directory, &target_name)?;
        if let Some(identity) = previous_identity.as_ref() {
            self.validate_file_identity(identity)?;
            self.read_from_root(&root, Some(identity))?;
        }

        let mut temp = self.create_temp(&root)?;
        temp.file
            .write_all(value.as_bytes())
            .map_err(|_| self.unavailable_error())?;
        temp.file.flush().map_err(|_| self.unavailable_error())?;
        temp.file.sync_all().map_err(|_| self.unavailable_error())?;
        let temp_identity = file_identity(&temp.file).map_err(|_| self.unavailable_error())?;
        self.validate_file_identity(&temp_identity)?;
        if temp_identity.size != value.len() as u64
            || self
                .identity_at_optional(&root.directory, &temp.name)?
                .as_ref()
                != Some(&temp_identity)
            || self.identity_at_optional(&root.directory, &target_name)? != previous_identity
        {
            return Err(self.unavailable_error());
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
            return Err(self.unavailable_error());
        }
        temp.published = true;
        root.directory
            .sync_all()
            .map_err(|_| self.unavailable_error())?;
        after_directory_sync(&mut temp.file)?;

        let published_identity = file_identity(&temp.file).map_err(|_| self.unavailable_error())?;
        self.validate_file_identity(&published_identity)?;
        if self
            .read_from_root(&root, Some(&published_identity))?
            .as_deref()
            != Some(value)
        {
            return Err(self.unavailable_error());
        }
        Ok(())
    }

    pub(crate) fn transaction<T>(
        &self,
        thread_lock: &Mutex<()>,
        path: &Path,
        operation: impl FnOnce() -> HostResult<T>,
    ) -> HostResult<T> {
        let _thread_guard = thread_lock.lock().map_err(|_| self.unavailable_error())?;
        let process_lock = PrivateJsonJournalProcessLock::acquire(*self, path)?;
        let result = operation();
        process_lock.validate()?;
        result
    }

    pub(crate) fn compare_and_swap(
        &self,
        thread_lock: &Mutex<()>,
        path: &Path,
        expected_value: Option<&str>,
        value: Option<&str>,
    ) -> HostResult<()> {
        self.transaction(thread_lock, path, || {
            if expected_value.is_some_and(|expected| expected.len() > self.policy.max_bytes)
                || value.is_some_and(|new_value| new_value.len() > self.policy.max_bytes)
            {
                return Err(self.too_large_error());
            }
            if self.read(path)?.as_deref() != expected_value {
                return Err(self.conflict_error());
            }
            self.write(path, value)
        })
    }

    pub(crate) fn open_root(
        &self,
        path: &Path,
        create: bool,
    ) -> HostResult<Option<PrivateJsonJournalRoot>> {
        let trusted_path = trusted_private_journal_root_path(path);
        let path = trusted_path.as_path();
        if !path.is_absolute() {
            return Err(self.unavailable_error());
        }
        let mut names = Vec::new();
        for component in path.components() {
            match component {
                Component::RootDir => {}
                Component::Normal(name) => names.push(name),
                _ => return Err(self.unavailable_error()),
            }
        }
        if names.is_empty() {
            return Err(self.unavailable_error());
        }

        let mut current = open_directory(Path::new("/")).map_err(|_| self.unavailable_error())?;
        self.validate_parent(&current)?;
        for (index, name) in names.iter().enumerate() {
            let name = CString::new(name.as_bytes()).map_err(|_| self.unavailable_error())?;
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
                    let created =
                        unsafe { libc::mkdirat(current.as_raw_fd(), name.as_ptr(), 0o700) };
                    if created == -1 {
                        let mkdir_error = std::io::Error::last_os_error();
                        if mkdir_error.kind() != std::io::ErrorKind::AlreadyExists {
                            return Err(self.unavailable_error());
                        }
                    } else {
                        current.sync_all().map_err(|_| self.unavailable_error())?;
                    }
                    openat_file(
                        current.as_raw_fd(),
                        &name,
                        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                        0,
                    )
                    .map_err(|_| self.unavailable_error())?
                }
                Err(_) => return Err(self.unavailable_error()),
            };
            if is_root {
                self.validate_root_identity(
                    &file_identity(&next).map_err(|_| self.unavailable_error())?,
                )?;
            } else {
                self.validate_parent(&next)?;
            }
            validate_anchored_extended_acl(&next).map_err(|_| self.unavailable_error())?;
            current = next;
        }
        let identity = file_identity(&current).map_err(|_| self.unavailable_error())?;
        Ok(Some(PrivateJsonJournalRoot {
            journal: *self,
            path: trusted_path,
            directory: current,
            device: identity.device,
            inode: identity.inode,
        }))
    }

    fn write_some(&self, path: &Path, value: &str) -> HostResult<()> {
        self.write_some_with(path, value, |_| Ok(()))
    }

    fn clear(&self, path: &Path) -> HostResult<()> {
        let Some(root) = self.open_root(path, false)? else {
            return Ok(());
        };
        root.validate()?;
        let name = self.target_name()?;
        let Some(path_identity) = self.identity_at_optional(&root.directory, &name)? else {
            return Ok(());
        };
        self.validate_file_identity(&path_identity)?;
        let file = openat_file(
            root.directory.as_raw_fd(),
            &name,
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            0,
        )
        .map_err(|_| self.unavailable_error())?;
        let opened_identity = file_identity(&file).map_err(|_| self.unavailable_error())?;
        if opened_identity != path_identity {
            return Err(self.unavailable_error());
        }
        self.validate_file_identity(&opened_identity)?;
        validate_anchored_extended_acl(&file).map_err(|_| self.unavailable_error())?;
        root.validate()?;
        if self.identity_at_optional(&root.directory, &name)?.as_ref() != Some(&opened_identity) {
            return Err(self.unavailable_error());
        }
        if unsafe { libc::unlinkat(root.directory.as_raw_fd(), name.as_ptr(), 0) } == -1 {
            return Err(self.unavailable_error());
        }
        root.directory
            .sync_all()
            .map_err(|_| self.unavailable_error())?;
        let unlinked_identity = file_identity(&file).map_err(|_| self.unavailable_error())?;
        if !same_identity_after_unlink(&unlinked_identity, &opened_identity)
            || self.identity_at_optional(&root.directory, &name)?.is_some()
        {
            return Err(self.unavailable_error());
        }
        root.validate()
    }

    fn create_temp<'a>(
        &self,
        root: &'a PrivateJsonJournalRoot,
    ) -> HostResult<PrivateJsonJournalTemp<'a>> {
        for _ in 0..64 {
            let mut random = [0_u8; 16];
            OsRng
                .try_fill_bytes(&mut random)
                .map_err(|_| self.unavailable_error())?;
            let name = CString::new(format!(
                "{}{}",
                self.policy.temp_prefix,
                encode_hex(&random)
            ))
            .map_err(|_| self.unavailable_error())?;
            let file = match openat_file(
                root.directory.as_raw_fd(),
                &name,
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            ) {
                Ok(file) => file,
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(_) => return Err(self.unavailable_error()),
            };
            let temp = PrivateJsonJournalTemp {
                journal: *self,
                directory: &root.directory,
                name,
                file,
                published: false,
            };
            temp.file
                .set_permissions(fs::Permissions::from_mode(0o600))
                .map_err(|_| self.unavailable_error())?;
            let identity = file_identity(&temp.file).map_err(|_| self.unavailable_error())?;
            self.validate_file_identity(&identity)?;
            if self
                .identity_at_optional(&root.directory, &temp.name)?
                .as_ref()
                != Some(&identity)
            {
                return Err(self.unavailable_error());
            }
            return Ok(temp);
        }
        Err(self.unavailable_error())
    }

    fn read_from_root(
        &self,
        root: &PrivateJsonJournalRoot,
        expected_identity: Option<&FileIdentity>,
    ) -> HostResult<Option<String>> {
        root.validate()?;
        let name = self.target_name()?;
        let Some(path_identity) = self.identity_at_optional(&root.directory, &name)? else {
            return if expected_identity.is_none() {
                Ok(None)
            } else {
                Err(self.unavailable_error())
            };
        };
        self.validate_read_identity(&path_identity)?;
        if expected_identity.is_some_and(|expected| expected != &path_identity) {
            return Err(self.unavailable_error());
        }
        let mut file = openat_file(
            root.directory.as_raw_fd(),
            &name,
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            0,
        )
        .map_err(|_| self.unavailable_error())?;
        let opened_identity = file_identity(&file).map_err(|_| self.unavailable_error())?;
        if opened_identity != path_identity {
            return Err(self.unavailable_error());
        }
        self.validate_read_identity(&opened_identity)?;
        validate_anchored_extended_acl(&file).map_err(|_| self.unavailable_error())?;

        let capacity =
            usize::try_from(opened_identity.size).map_err(|_| self.unavailable_error())?;
        let mut bytes = Vec::with_capacity(capacity);
        (&mut file)
            .take((self.policy.max_bytes + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| self.unavailable_error())?;
        if bytes.len() != capacity || bytes.len() > self.policy.max_bytes {
            return Err(self.unavailable_error());
        }
        let final_open_identity = file_identity(&file).map_err(|_| self.unavailable_error())?;
        let final_path_identity = self
            .identity_at_optional(&root.directory, &name)?
            .ok_or_else(|| self.unavailable_error())?;
        if final_open_identity != opened_identity || final_path_identity != opened_identity {
            return Err(self.unavailable_error());
        }
        root.validate()?;
        String::from_utf8(bytes)
            .map(Some)
            .map_err(|_| self.unavailable_error())
    }

    fn validate_parent(&self, directory: &File) -> HostResult<()> {
        let identity = file_identity(directory).map_err(|_| self.unavailable_error())?;
        let effective_user = unsafe { libc::geteuid() };
        let root_sticky_boundary = identity.owner == 0 && identity.mode & STICKY_MODE_BIT != 0;
        if identity.mode & FILE_TYPE_MASK != DIRECTORY_FILE_TYPE
            || (identity.owner != 0 && identity.owner != effective_user)
            || (identity.mode & 0o022 != 0 && !root_sticky_boundary)
        {
            return Err(self.unavailable_error());
        }
        validate_anchored_extended_acl(directory).map_err(|_| self.unavailable_error())
    }

    fn validate_root_identity(&self, identity: &FileIdentity) -> HostResult<()> {
        if identity.mode & FILE_TYPE_MASK != DIRECTORY_FILE_TYPE
            || identity.owner != unsafe { libc::geteuid() }
            || identity.mode & 0o777 != 0o700
        {
            return Err(self.unavailable_error());
        }
        Ok(())
    }

    fn validate_file_identity(&self, identity: &FileIdentity) -> HostResult<()> {
        if identity.mode & FILE_TYPE_MASK != REGULAR_FILE_TYPE
            || identity.owner != unsafe { libc::geteuid() }
            || identity.mode & 0o777 != 0o600
            || identity.links != 1
        {
            return Err(self.unavailable_error());
        }
        Ok(())
    }

    fn validate_lock_identity(&self, identity: &FileIdentity) -> HostResult<()> {
        self.validate_file_identity(identity)?;
        if identity.size != 0 {
            return Err(self.unavailable_error());
        }
        Ok(())
    }

    fn validate_read_identity(&self, identity: &FileIdentity) -> HostResult<()> {
        self.validate_file_identity(identity)?;
        if identity.size > self.policy.max_bytes as u64 {
            return Err(self.unavailable_error());
        }
        Ok(())
    }

    fn identity_at_optional(
        &self,
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
            Err(self.unavailable_error())
        }
    }

    fn target_name(&self) -> HostResult<CString> {
        CString::new(self.policy.file_name).map_err(|_| self.unavailable_error())
    }

    fn lock_name(&self) -> HostResult<CString> {
        CString::new(self.policy.lock_file_name).map_err(|_| self.unavailable_error())
    }

    fn unavailable_error(&self) -> NativeHostError {
        (self.policy.unavailable_error)()
    }

    fn too_large_error(&self) -> NativeHostError {
        (self.policy.too_large_error)()
    }

    fn conflict_error(&self) -> NativeHostError {
        (self.policy.conflict_error)()
    }
}

impl PrivateJsonJournalRoot {
    fn validate(&self) -> HostResult<()> {
        let reopened = self
            .journal
            .open_root(&self.path, false)?
            .ok_or_else(|| self.journal.unavailable_error())?;
        let held = file_identity(&self.directory).map_err(|_| self.journal.unavailable_error())?;
        let current =
            file_identity(&reopened.directory).map_err(|_| self.journal.unavailable_error())?;
        if held.device != self.device
            || held.inode != self.inode
            || current.device != self.device
            || current.inode != self.inode
        {
            return Err(self.journal.unavailable_error());
        }
        self.journal.validate_root_identity(&held)
    }
}

impl PrivateJsonJournalProcessLock {
    fn acquire(journal: PrivateJsonJournal, path: &Path) -> HostResult<Self> {
        let root = journal
            .open_root(path, true)?
            .ok_or_else(|| journal.unavailable_error())?;
        root.validate()?;
        let name = journal.lock_name()?;
        let previous_identity = journal.identity_at_optional(&root.directory, &name)?;
        if let Some(identity) = previous_identity.as_ref() {
            journal.validate_lock_identity(identity)?;
        }
        let file = openat_file(
            root.directory.as_raw_fd(),
            &name,
            libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            0o600,
        )
        .map_err(|_| journal.unavailable_error())?;
        let identity = file_identity(&file).map_err(|_| journal.unavailable_error())?;
        journal.validate_lock_identity(&identity)?;
        validate_anchored_extended_acl(&file).map_err(|_| journal.unavailable_error())?;
        if previous_identity.is_some_and(|previous| previous != identity)
            || journal
                .identity_at_optional(&root.directory, &name)?
                .as_ref()
                != Some(&identity)
        {
            return Err(journal.unavailable_error());
        }
        root.validate()?;

        let deadline = Instant::now() + journal.policy.lock_timeout;
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
                        return Err(journal.unavailable_error());
                    }
                    thread::sleep(
                        journal
                            .policy
                            .lock_poll_interval
                            .min(deadline.saturating_duration_since(now)),
                    );
                }
                _ => return Err(journal.unavailable_error()),
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
        let journal = self.root.journal;
        let open_identity = file_identity(&self.file).map_err(|_| journal.unavailable_error())?;
        journal.validate_lock_identity(&open_identity)?;
        validate_anchored_extended_acl(&self.file).map_err(|_| journal.unavailable_error())?;
        if open_identity != self.identity
            || journal
                .identity_at_optional(&self.root.directory, &self.name)?
                .as_ref()
                != Some(&self.identity)
        {
            return Err(journal.unavailable_error());
        }
        Ok(())
    }
}

impl Drop for PrivateJsonJournalProcessLock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

impl Drop for PrivateJsonJournalTemp<'_> {
    fn drop(&mut self) {
        if self.published {
            return;
        }
        let Ok(open_identity) = file_identity(&self.file) else {
            return;
        };
        let Ok(Some(path_identity)) = self
            .journal
            .identity_at_optional(self.directory, &self.name)
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

fn same_identity_after_unlink(actual: &FileIdentity, expected: &FileIdentity) -> bool {
    if expected.links != 1 || actual.links != 0 {
        return false;
    }
    let mut normalized = actual.clone();
    normalized.links = expected.links;
    normalized.changed_seconds = expected.changed_seconds;
    normalized.changed_nanoseconds = expected.changed_nanoseconds;
    &normalized == expected
}

#[cfg(target_os = "linux")]
fn trusted_private_journal_root_path(path: &Path) -> PathBuf {
    path.to_path_buf()
}

#[cfg(target_os = "macos")]
fn trusted_private_journal_root_path(path: &Path) -> PathBuf {
    macos_trusted_path_alias(path)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::sync::{Arc, Barrier};

    use super::*;

    const TEST_MAX_BYTES: usize = 1024 * 1024;

    fn test_error() -> NativeHostError {
        NativeHostError::new(
            "private_json_journal_unavailable",
            "Private journal is unavailable.",
        )
    }

    fn test_too_large_error() -> NativeHostError {
        NativeHostError::new(
            "private_json_journal_too_large",
            "Private journal is too large.",
        )
    }

    fn test_conflict_error() -> NativeHostError {
        NativeHostError::new(
            "private_json_journal_conflict",
            "Private journal changed before update.",
        )
    }

    fn journal() -> PrivateJsonJournal {
        PrivateJsonJournal::new(PrivateJsonJournalPolicy {
            file_name: ".private-json-journal",
            temp_prefix: ".private-json-journal.tmp.",
            lock_file_name: ".private-json-journal.lock",
            max_bytes: TEST_MAX_BYTES,
            lock_timeout: Duration::from_secs(3),
            lock_poll_interval: Duration::from_millis(10),
            unavailable_error: test_error,
            too_large_error: test_too_large_error,
            conflict_error: test_conflict_error,
        })
    }

    fn root(temp: &tempfile::TempDir) -> PathBuf {
        fs::canonicalize(temp.path()).unwrap().join("journal")
    }

    #[test]
    fn private_json_journal_missing_read_and_exact_cas_roundtrip() {
        let temp = tempfile::tempdir().unwrap();
        let root = root(&temp);
        let lock = Mutex::new(());
        let journal = journal();

        assert_eq!(journal.read(&root).unwrap(), None);
        journal
            .compare_and_swap(&lock, &root, None, Some("{\"revision\":1}"))
            .unwrap();
        assert_eq!(
            journal.read(&root).unwrap().as_deref(),
            Some("{\"revision\":1}")
        );
        assert_eq!(
            journal
                .compare_and_swap(&lock, &root, None, Some("stale"))
                .unwrap_err()
                .code,
            "private_json_journal_conflict"
        );
        journal
            .compare_and_swap(&lock, &root, Some("{\"revision\":1}"), None)
            .unwrap();
        assert_eq!(journal.read(&root).unwrap(), None);
    }

    #[test]
    fn private_json_journal_enforces_one_mebibyte_utf8_budget() {
        let temp = tempfile::tempdir().unwrap();
        let root = root(&temp);
        let lock = Mutex::new(());
        let journal = journal();
        let exact = "x".repeat(TEST_MAX_BYTES);
        journal
            .compare_and_swap(&lock, &root, None, Some(&exact))
            .unwrap();
        let oversized = format!("{exact}x");
        assert_eq!(
            journal
                .compare_and_swap(&lock, &root, Some(&exact), Some(&oversized))
                .unwrap_err()
                .code,
            "private_json_journal_too_large"
        );
        assert_eq!(
            journal.read(&root).unwrap().as_deref(),
            Some(exact.as_str())
        );
    }

    #[test]
    fn private_json_journal_rejects_no_follow_root_traversal() {
        let temp = tempfile::tempdir().unwrap();
        let actual = fs::canonicalize(temp.path()).unwrap().join("actual");
        fs::create_dir(&actual).unwrap();
        fs::set_permissions(&actual, fs::Permissions::from_mode(0o700)).unwrap();
        let linked = fs::canonicalize(temp.path()).unwrap().join("linked");
        symlink(&actual, &linked).unwrap();

        assert_eq!(
            journal().read(&linked).unwrap_err().code,
            "private_json_journal_unavailable"
        );
    }

    #[test]
    fn private_json_journal_serializes_independent_thread_and_process_locks() {
        let temp = tempfile::tempdir().unwrap();
        let root = root(&temp);
        let journal = journal();
        let first_lock = Arc::new(Mutex::new(()));
        let second_lock = Arc::new(Mutex::new(()));
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));

        let holder_root = root.clone();
        let holder_lock = Arc::clone(&first_lock);
        let holder_entered = Arc::clone(&entered);
        let holder_release = Arc::clone(&release);
        let holder = thread::spawn(move || {
            journal
                .transaction(&holder_lock, &holder_root, || {
                    holder_entered.wait();
                    holder_release.wait();
                    Ok(())
                })
                .unwrap();
        });
        entered.wait();

        let contender_root = root.clone();
        let contender_lock = Arc::clone(&second_lock);
        let (tx, rx) = std::sync::mpsc::channel();
        let contender = thread::spawn(move || {
            let result = journal.transaction(&contender_lock, &contender_root, || Ok(()));
            tx.send(result).unwrap();
        });
        assert!(rx.recv_timeout(Duration::from_millis(100)).is_err());
        release.wait();
        rx.recv_timeout(Duration::from_secs(2)).unwrap().unwrap();
        holder.join().unwrap();
        contender.join().unwrap();
    }

    #[test]
    fn private_json_journal_rechecks_authoritative_bytes_after_publish() {
        let temp = tempfile::tempdir().unwrap();
        let root = root(&temp);
        let journal = journal();

        let error = journal
            .write_some_with(&root, "durable", |file| {
                file.set_len(0).map_err(|_| test_error())?;
                file.sync_all().map_err(|_| test_error())
            })
            .unwrap_err();
        assert_eq!(error.code, "private_json_journal_unavailable");
        assert_eq!(journal.read(&root).unwrap().as_deref(), Some(""));
    }
}
