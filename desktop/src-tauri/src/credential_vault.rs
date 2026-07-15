use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rand::rngs::OsRng;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use zeroize::{Zeroize, Zeroizing};

#[cfg(target_os = "macos")]
const KEYCHAIN_SERVICE: &str = "org.openevo.desktop.ssh";
const REGISTRY_FILENAME: &str = "native-credentials-v1.json";
const REGISTRY_MAX_BYTES: u64 = 256 * 1024;
const MAX_RECORDS: usize = 256;
const MAX_SECRET_BYTES: usize = 16 * 1024;
const MAX_PRIVATE_KEY_BYTES: u64 = 1024 * 1024;

#[derive(Debug)]
pub(crate) struct CredentialVaultError;

type VaultResult<T> = Result<T, CredentialVaultError>;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CredentialRecord {
    pub(crate) profile_id: String,
    pub(crate) authentication_kind: String,
    pub(crate) private_key_path: Option<String>,
    pub(crate) password_account: Option<String>,
    pub(crate) passphrase_account: Option<String>,
}

#[derive(Debug)]
pub(crate) struct CredentialBundle {
    pub(crate) record: CredentialRecord,
    pub(crate) password: Option<Zeroizing<Vec<u8>>>,
    pub(crate) private_key: Option<Zeroizing<Vec<u8>>>,
    pub(crate) passphrase: Option<Zeroizing<Vec<u8>>>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RegistryDocument {
    schema_version: String,
    records: Vec<CredentialRecord>,
}

pub(crate) struct CredentialManager {
    root: PathBuf,
    records: Mutex<HashMap<String, CredentialRecord>>,
    vault: PlatformCredentialVault,
}

impl CredentialManager {
    pub(crate) fn open(root: PathBuf) -> VaultResult<Self> {
        prepare_private_root(&root)?;
        let records = read_registry(&root)?;
        Ok(Self {
            root,
            records: Mutex::new(records),
            vault: PlatformCredentialVault::new(),
        })
    }

    pub(crate) fn records(&self) -> VaultResult<Vec<CredentialRecord>> {
        let records = self.records.lock().map_err(|_| CredentialVaultError)?;
        let mut values = records.values().cloned().collect::<Vec<_>>();
        values.sort_by(|left, right| left.profile_id.cmp(&right.profile_id));
        Ok(values)
    }

    pub(crate) fn configure_password(
        &self,
        profile_id: &str,
        mut secret: Zeroizing<Vec<u8>>,
    ) -> VaultResult<()> {
        validate_profile_id(profile_id)?;
        validate_secret(&secret)?;
        let mut records = self.records.lock().map_err(|_| CredentialVaultError)?;
        let mut record = records
            .get(profile_id)
            .cloned()
            .unwrap_or_else(|| empty_record(profile_id, "native_password"));
        if record.authentication_kind != "native_password" {
            delete_record_accounts(&self.vault, &record)?;
            record = empty_record(profile_id, "native_password");
        }
        let (account, created_account) = match record.password_account.as_deref() {
            Some(account) if self.vault.exists(account)? => {
                self.vault.update(account, &secret)?;
                (account.to_string(), false)
            }
            _ => {
                let account = random_account()?;
                self.vault.create(&account, &secret)?;
                (account, true)
            }
        };
        record.password_account = Some(account.clone());
        record.private_key_path = None;
        record.passphrase_account = None;
        records.insert(profile_id.to_string(), record);
        if let Err(error) = write_registry(&self.root, &records) {
            if created_account {
                let _ = self.vault.delete(&account);
            }
            return Err(error);
        }
        secret.zeroize();
        Ok(())
    }

    pub(crate) fn configure_private_key(
        &self,
        profile_id: &str,
        selected_path: &Path,
    ) -> VaultResult<()> {
        validate_profile_id(profile_id)?;
        let canonical = selected_path
            .canonicalize()
            .map_err(|_| CredentialVaultError)?;
        let _ = read_private_key(&canonical)?;
        let path = canonical.to_str().ok_or(CredentialVaultError)?.to_string();
        if path.len() > 4096 || path.chars().any(char::is_control) {
            return Err(CredentialVaultError);
        }
        let mut records = self.records.lock().map_err(|_| CredentialVaultError)?;
        let mut record = records
            .get(profile_id)
            .cloned()
            .unwrap_or_else(|| empty_record(profile_id, "native_private_key"));
        if record.authentication_kind != "native_private_key" {
            delete_record_accounts(&self.vault, &record)?;
            record = empty_record(profile_id, "native_private_key");
        }
        record.private_key_path = Some(path);
        record.password_account = None;
        records.insert(profile_id.to_string(), record);
        write_registry(&self.root, &records)
    }

    pub(crate) fn configure_passphrase(
        &self,
        profile_id: &str,
        secret: Zeroizing<Vec<u8>>,
    ) -> VaultResult<()> {
        validate_profile_id(profile_id)?;
        validate_secret(&secret)?;
        let mut records = self.records.lock().map_err(|_| CredentialVaultError)?;
        let mut record = records
            .get(profile_id)
            .cloned()
            .ok_or(CredentialVaultError)?;
        if record.authentication_kind != "native_private_key" || record.private_key_path.is_none() {
            return Err(CredentialVaultError);
        }
        let (account, created_account) = match record.passphrase_account.as_deref() {
            Some(account) if self.vault.exists(account)? => {
                self.vault.update(account, &secret)?;
                (account.to_string(), false)
            }
            _ => {
                let account = random_account()?;
                self.vault.create(&account, &secret)?;
                (account, true)
            }
        };
        record.passphrase_account = Some(account.clone());
        records.insert(profile_id.to_string(), record);
        if let Err(error) = write_registry(&self.root, &records) {
            if created_account {
                let _ = self.vault.delete(&account);
            }
            return Err(error);
        }
        Ok(())
    }

    pub(crate) fn clear_slot(&self, profile_id: &str, slot_kind: &str) -> VaultResult<()> {
        validate_profile_id(profile_id)?;
        let mut records = self.records.lock().map_err(|_| CredentialVaultError)?;
        let Some(mut record) = records.get(profile_id).cloned() else {
            return Ok(());
        };
        match slot_kind {
            "ssh_password" => {
                if let Some(account) = record.password_account.take() {
                    self.vault.delete(&account)?;
                }
            }
            "ssh_private_key" => {
                record.private_key_path = None;
                if let Some(account) = record.passphrase_account.take() {
                    self.vault.delete(&account)?;
                }
            }
            "ssh_private_key_passphrase" => {
                if let Some(account) = record.passphrase_account.take() {
                    self.vault.delete(&account)?;
                }
            }
            _ => return Err(CredentialVaultError),
        }
        records.insert(profile_id.to_string(), record);
        write_registry(&self.root, &records)
    }

    pub(crate) fn remove_profile(&self, profile_id: &str) -> VaultResult<()> {
        validate_profile_id(profile_id)?;
        let mut records = self.records.lock().map_err(|_| CredentialVaultError)?;
        if let Some(record) = records.remove(profile_id) {
            if let Some(account) = record.password_account {
                self.vault.delete(&account)?;
            }
            if let Some(account) = record.passphrase_account {
                self.vault.delete(&account)?;
            }
            write_registry(&self.root, &records)?;
        }
        Ok(())
    }

    pub(crate) fn bundle(&self, profile_id: &str) -> VaultResult<CredentialBundle> {
        validate_profile_id(profile_id)?;
        let record = self
            .records
            .lock()
            .map_err(|_| CredentialVaultError)?
            .get(profile_id)
            .cloned()
            .ok_or(CredentialVaultError)?;
        let password = match record.password_account.as_deref() {
            Some(account) if self.vault.exists(account)? => Some(self.vault.read(account)?),
            _ => None,
        };
        let passphrase = match record.passphrase_account.as_deref() {
            Some(account) if self.vault.exists(account)? => Some(self.vault.read(account)?),
            _ => None,
        };
        let private_key = record
            .private_key_path
            .as_deref()
            .and_then(|path| read_private_key(Path::new(path)).ok());
        Ok(CredentialBundle {
            record,
            password,
            private_key,
            passphrase,
        })
    }
}

fn empty_record(profile_id: &str, authentication_kind: &str) -> CredentialRecord {
    CredentialRecord {
        profile_id: profile_id.to_string(),
        authentication_kind: authentication_kind.to_string(),
        private_key_path: None,
        password_account: None,
        passphrase_account: None,
    }
}

fn delete_record_accounts(
    vault: &PlatformCredentialVault,
    record: &CredentialRecord,
) -> VaultResult<()> {
    if let Some(account) = record.password_account.as_deref() {
        vault.delete(account)?;
    }
    if let Some(account) = record.passphrase_account.as_deref() {
        vault.delete(account)?;
    }
    Ok(())
}

fn prepare_private_root(root: &Path) -> VaultResult<()> {
    fs::create_dir_all(root).map_err(|_| CredentialVaultError)?;
    fs::set_permissions(root, fs::Permissions::from_mode(0o700))
        .map_err(|_| CredentialVaultError)?;
    let metadata = fs::symlink_metadata(root).map_err(|_| CredentialVaultError)?;
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(CredentialVaultError);
    }
    Ok(())
}

fn read_registry(root: &Path) -> VaultResult<HashMap<String, CredentialRecord>> {
    let path = root.join(REGISTRY_FILENAME);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(HashMap::new()),
        Err(_) => return Err(CredentialVaultError),
    };
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
        || metadata.len() > REGISTRY_MAX_BYTES
        || metadata.permissions().mode() & 0o777 != 0o600
    {
        return Err(CredentialVaultError);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| CredentialVaultError)?;
    let mut payload = Vec::with_capacity(metadata.len() as usize);
    file.take(REGISTRY_MAX_BYTES + 1)
        .read_to_end(&mut payload)
        .map_err(|_| CredentialVaultError)?;
    if payload.len() as u64 > REGISTRY_MAX_BYTES {
        return Err(CredentialVaultError);
    }
    let document: RegistryDocument =
        serde_json::from_slice(&payload).map_err(|_| CredentialVaultError)?;
    if document.schema_version != "1" || document.records.len() > MAX_RECORDS {
        return Err(CredentialVaultError);
    }
    let mut records = HashMap::new();
    for record in document.records {
        validate_record(&record)?;
        if records.insert(record.profile_id.clone(), record).is_some() {
            return Err(CredentialVaultError);
        }
    }
    Ok(records)
}

fn write_registry(root: &Path, records: &HashMap<String, CredentialRecord>) -> VaultResult<()> {
    if records.len() > MAX_RECORDS {
        return Err(CredentialVaultError);
    }
    let mut values = records.values().cloned().collect::<Vec<_>>();
    values.sort_by(|left, right| left.profile_id.cmp(&right.profile_id));
    for record in &values {
        validate_record(record)?;
    }
    let payload = serde_json::to_vec(&RegistryDocument {
        schema_version: "1".to_string(),
        records: values,
    })
    .map_err(|_| CredentialVaultError)?;
    if payload.len() as u64 > REGISTRY_MAX_BYTES {
        return Err(CredentialVaultError);
    }
    let mut nonce = [0_u8; 16];
    OsRng
        .try_fill_bytes(&mut nonce)
        .map_err(|_| CredentialVaultError)?;
    let temp = root.join(format!(".native-credentials-{}.tmp", hex(&nonce)));
    let final_path = root.join(REGISTRY_FILENAME);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&temp)
        .map_err(|_| CredentialVaultError)?;
    let result = (|| {
        file.write_all(&payload).map_err(|_| CredentialVaultError)?;
        file.sync_all().map_err(|_| CredentialVaultError)?;
        if fs::symlink_metadata(&final_path).is_ok_and(|metadata| !metadata.is_file()) {
            return Err(CredentialVaultError);
        }
        fs::rename(&temp, &final_path).map_err(|_| CredentialVaultError)?;
        File::open(root)
            .and_then(|directory| directory.sync_all())
            .map_err(|_| CredentialVaultError)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn read_private_key(path: &Path) -> VaultResult<Zeroizing<Vec<u8>>> {
    let metadata = fs::symlink_metadata(path).map_err(|_| CredentialVaultError)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_PRIVATE_KEY_BYTES {
        return Err(CredentialVaultError);
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| CredentialVaultError)?;
    let mut payload = Zeroizing::new(Vec::with_capacity(metadata.len() as usize));
    file.take(MAX_PRIVATE_KEY_BYTES + 1)
        .read_to_end(&mut payload)
        .map_err(|_| CredentialVaultError)?;
    if payload.len() as u64 != metadata.len() || payload.contains(&0) {
        return Err(CredentialVaultError);
    }
    Ok(payload)
}

fn validate_record(record: &CredentialRecord) -> VaultResult<()> {
    validate_profile_id(&record.profile_id)?;
    if !matches!(
        record.authentication_kind.as_str(),
        "native_password" | "native_private_key"
    ) {
        return Err(CredentialVaultError);
    }
    for account in [&record.password_account, &record.passphrase_account]
        .into_iter()
        .flatten()
    {
        if account.len() != 64 || !account.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(CredentialVaultError);
        }
    }
    if record.authentication_kind == "native_password"
        && (record.private_key_path.is_some() || record.passphrase_account.is_some())
    {
        return Err(CredentialVaultError);
    }
    if record.authentication_kind == "native_private_key" && record.password_account.is_some() {
        return Err(CredentialVaultError);
    }
    if record.private_key_path.as_ref().is_some_and(|path| {
        path.len() > 4096 || !path.starts_with('/') || path.chars().any(char::is_control)
    }) {
        return Err(CredentialVaultError);
    }
    Ok(())
}

fn validate_profile_id(profile_id: &str) -> VaultResult<()> {
    if profile_id.is_empty()
        || profile_id.len() > 256
        || profile_id.trim() != profile_id
        || profile_id.chars().any(char::is_control)
    {
        return Err(CredentialVaultError);
    }
    Ok(())
}

fn validate_secret(secret: &[u8]) -> VaultResult<()> {
    if secret.is_empty() || secret.len() > MAX_SECRET_BYTES || secret.contains(&0) {
        return Err(CredentialVaultError);
    }
    Ok(())
}

fn random_account() -> VaultResult<String> {
    let mut value = [0_u8; 32];
    OsRng
        .try_fill_bytes(&mut value)
        .map_err(|_| CredentialVaultError)?;
    Ok(hex(&value))
}

fn hex(value: &[u8]) -> String {
    const TABLE: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(value.len() * 2);
    for byte in value {
        encoded.push(TABLE[(byte >> 4) as usize] as char);
        encoded.push(TABLE[(byte & 0x0f) as usize] as char);
    }
    encoded
}

struct PlatformCredentialVault {
    #[cfg(all(not(target_os = "macos"), test))]
    fake: Mutex<HashMap<String, Zeroizing<Vec<u8>>>>,
}

impl PlatformCredentialVault {
    fn new() -> Self {
        Self {
            #[cfg(all(not(target_os = "macos"), test))]
            fake: Mutex::new(HashMap::new()),
        }
    }

    #[cfg(target_os = "macos")]
    fn create(&self, account: &str, secret: &[u8]) -> VaultResult<()> {
        if self.exists(account)? {
            return Err(CredentialVaultError);
        }
        security_framework::passwords::set_generic_password(KEYCHAIN_SERVICE, account, secret)
            .map_err(|_| CredentialVaultError)
    }

    #[cfg(target_os = "macos")]
    fn update(&self, account: &str, secret: &[u8]) -> VaultResult<()> {
        if !self.exists(account)? {
            return Err(CredentialVaultError);
        }
        security_framework::passwords::set_generic_password(KEYCHAIN_SERVICE, account, secret)
            .map_err(|_| CredentialVaultError)
    }

    #[cfg(target_os = "macos")]
    fn read(&self, account: &str) -> VaultResult<Zeroizing<Vec<u8>>> {
        security_framework::passwords::get_generic_password(KEYCHAIN_SERVICE, account)
            .map(Zeroizing::new)
            .map_err(|_| CredentialVaultError)
    }

    #[cfg(target_os = "macos")]
    fn exists(&self, account: &str) -> VaultResult<bool> {
        match security_framework::passwords::get_generic_password(KEYCHAIN_SERVICE, account) {
            Ok(mut value) => {
                value.zeroize();
                Ok(true)
            }
            Err(error) if error.code() == -25300 => Ok(false),
            Err(_) => Err(CredentialVaultError),
        }
    }

    #[cfg(target_os = "macos")]
    fn delete(&self, account: &str) -> VaultResult<()> {
        if !self.exists(account)? {
            return Ok(());
        }
        security_framework::passwords::delete_generic_password(KEYCHAIN_SERVICE, account)
            .map_err(|_| CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), test))]
    fn create(&self, account: &str, secret: &[u8]) -> VaultResult<()> {
        let mut fake = self.fake.lock().map_err(|_| CredentialVaultError)?;
        if fake.contains_key(account) {
            return Err(CredentialVaultError);
        }
        fake.insert(account.to_string(), Zeroizing::new(secret.to_vec()));
        Ok(())
    }

    #[cfg(all(not(target_os = "macos"), test))]
    fn update(&self, account: &str, secret: &[u8]) -> VaultResult<()> {
        let mut fake = self.fake.lock().map_err(|_| CredentialVaultError)?;
        let value = fake.get_mut(account).ok_or(CredentialVaultError)?;
        value.zeroize();
        **value = secret.to_vec();
        Ok(())
    }

    #[cfg(all(not(target_os = "macos"), test))]
    fn read(&self, account: &str) -> VaultResult<Zeroizing<Vec<u8>>> {
        self.fake
            .lock()
            .map_err(|_| CredentialVaultError)?
            .get(account)
            .map(|value| Zeroizing::new(value.to_vec()))
            .ok_or(CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), test))]
    fn exists(&self, account: &str) -> VaultResult<bool> {
        Ok(self
            .fake
            .lock()
            .map_err(|_| CredentialVaultError)?
            .contains_key(account))
    }

    #[cfg(all(not(target_os = "macos"), test))]
    fn delete(&self, account: &str) -> VaultResult<()> {
        if let Some(mut value) = self
            .fake
            .lock()
            .map_err(|_| CredentialVaultError)?
            .remove(account)
        {
            value.zeroize();
        }
        Ok(())
    }

    #[cfg(all(not(target_os = "macos"), not(test)))]
    fn create(&self, _account: &str, _secret: &[u8]) -> VaultResult<()> {
        Err(CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), not(test)))]
    fn update(&self, _account: &str, _secret: &[u8]) -> VaultResult<()> {
        Err(CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), not(test)))]
    fn read(&self, _account: &str) -> VaultResult<Zeroizing<Vec<u8>>> {
        Err(CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), not(test)))]
    fn exists(&self, _account: &str) -> VaultResult<bool> {
        Err(CredentialVaultError)
    }

    #[cfg(all(not(target_os = "macos"), not(test)))]
    fn delete(&self, _account: &str) -> VaultResult<()> {
        Err(CredentialVaultError)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strict_fake_vault_requires_create_before_update_and_zeroizes_delete() {
        let vault = PlatformCredentialVault::new();
        assert!(vault.update("account", b"second").is_err());
        vault.create("account", b"first").unwrap();
        assert!(vault.create("account", b"duplicate").is_err());
        assert!(vault.exists("account").unwrap());
        vault.update("account", b"second").unwrap();
        assert_eq!(&*vault.read("account").unwrap(), b"second");
        vault.delete("account").unwrap();
        assert!(!vault.exists("account").unwrap());
    }

    #[test]
    fn private_registry_rehydrates_keychain_refs_without_persisting_secret() {
        let temporary = tempfile::tempdir().unwrap();
        let manager = CredentialManager::open(temporary.path().to_path_buf()).unwrap();
        manager
            .configure_password("profile-a", Zeroizing::new(b"canary-password".to_vec()))
            .unwrap();
        let document = fs::read(temporary.path().join(REGISTRY_FILENAME)).unwrap();
        assert!(!document
            .windows(15)
            .any(|value| value == b"canary-password"));
        let bundle = manager.bundle("profile-a").unwrap();
        assert_eq!(
            bundle.password.as_deref().map(Vec::as_slice),
            Some(b"canary-password".as_slice())
        );
    }

    #[test]
    fn configuring_a_new_authentication_kind_removes_old_vault_accounts() {
        let temporary = tempfile::tempdir().unwrap();
        let manager = CredentialManager::open(temporary.path().join("registry")).unwrap();
        manager
            .configure_password("profile-a", Zeroizing::new(b"canary-password".to_vec()))
            .unwrap();
        let password_account = manager.records().unwrap()[0]
            .password_account
            .clone()
            .unwrap();
        let key_path = temporary.path().join("selected-key");
        fs::write(&key_path, b"selected-private-key").unwrap();

        manager
            .configure_private_key("profile-a", &key_path)
            .unwrap();

        assert!(!manager.vault.exists(&password_account).unwrap());
        let bundle = manager.bundle("profile-a").unwrap();
        assert_eq!(bundle.record.authentication_kind, "native_private_key");
        assert_eq!(
            bundle.private_key.as_deref().map(Vec::as_slice),
            Some(b"selected-private-key".as_slice())
        );
    }

    #[test]
    fn unavailable_selected_key_rehydrates_as_an_empty_bundle() {
        let temporary = tempfile::tempdir().unwrap();
        let manager = CredentialManager::open(temporary.path().join("registry")).unwrap();
        let key_path = temporary.path().join("selected-key");
        fs::write(&key_path, b"selected-private-key").unwrap();
        manager
            .configure_private_key("profile-a", &key_path)
            .unwrap();
        fs::remove_file(&key_path).unwrap();

        let bundle = manager.bundle("profile-a").unwrap();

        assert!(bundle.private_key.is_none());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_keychain_create_update_exists_read_delete_round_trip() {
        let vault = PlatformCredentialVault::new();
        let account = random_account().unwrap();
        vault.create(&account, b"first-canary").unwrap();
        assert!(vault.exists(&account).unwrap());
        assert_eq!(&*vault.read(&account).unwrap(), b"first-canary");
        vault.update(&account, b"second-canary").unwrap();
        assert_eq!(&*vault.read(&account).unwrap(), b"second-canary");
        vault.delete(&account).unwrap();
        assert!(!vault.exists(&account).unwrap());
    }
}
