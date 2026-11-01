//! Non-secret preferences the webview stores (`getItem`/`setItem` in
//! `mobile/src/store/storage.desktop.ts`): the host, the Notifications switch,
//! the last-opened board and so on. A small JSON file in the app's config
//! directory, written atomically. Never a secret: those go to `secrets.rs`.
//!
//! It lives in Rust rather than localStorage because the core needs the host
//! before any webview exists, to hold the event stream and raise notifications
//! from the tray.
//!
//! Keys under `kc.session.` are kept in memory only, never written to the
//! file: they carry the UI across a webview that was dropped and rebuilt
//! while the app kept running (the screen it was on, see unload.rs), and a
//! fresh launch starts without them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const HOST_KEY: &str = "kc.apiBase";
const MAX_KEY: usize = 64;
const MAX_VALUE: usize = 64 * 1024;
const MAX_KEYS: usize = 256;
/// In memory only; see the module docs.
pub const SESSION_PREFIX: &str = "kc.session.";
const MAX_SESSION_KEYS: usize = 16;

#[derive(Debug, Default)]
pub struct Settings {
    path: Option<PathBuf>,
    items: BTreeMap<String, String>,
    session: BTreeMap<String, String>,
}

pub fn valid_key(key: &str) -> bool {
    key.starts_with("kc.")
        && key.len() <= MAX_KEY
        && key.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | ':'))
}

fn is_session(key: &str) -> bool {
    key.starts_with(SESSION_PREFIX)
}

impl Settings {
    pub fn load(path: PathBuf) -> Self {
        let items = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice::<BTreeMap<String, String>>(&b).ok())
            .unwrap_or_default()
            .into_iter()
            .filter(|(k, _)| valid_key(k) && !is_session(k))
            .collect();
        Settings { path: Some(path), items, session: BTreeMap::new() }
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        if is_session(key) {
            return self.session.get(key).map(String::as_str);
        }
        self.items.get(key).map(String::as_str)
    }

    pub fn set(&mut self, key: &str, value: &str) -> Result<(), String> {
        if !valid_key(key) {
            return Err("invalid key".into());
        }
        if value.len() > MAX_VALUE {
            return Err("value too large".into());
        }
        if is_session(key) {
            if !self.session.contains_key(key) && self.session.len() >= MAX_SESSION_KEYS {
                return Err("too many keys".into());
            }
            self.session.insert(key.to_string(), value.to_string());
            return Ok(());
        }
        if !self.items.contains_key(key) && self.items.len() >= MAX_KEYS {
            return Err("too many keys".into());
        }
        self.items.insert(key.to_string(), value.to_string());
        self.save()
    }

    pub fn remove(&mut self, key: &str) -> Result<(), String> {
        if is_session(key) {
            self.session.remove(key);
            return Ok(());
        }
        if self.items.remove(key).is_some() {
            self.save()?;
        }
        Ok(())
    }

    fn save(&self) -> Result<(), String> {
        let Some(path) = &self.path else { return Ok(()) };
        write_atomic(path, &serde_json::to_vec_pretty(&self.items).map_err(|e| e.to_string())?)
    }
}

/// tmp + rename in the same directory, so a crash never leaves half a file.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let dir = path.parent().ok_or("no parent directory")?;
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let tmp = dir.join(format!(".{}.tmp", path.file_name().and_then(|n| n.to_str()).unwrap_or("settings")));
    std::fs::write(&tmp, bytes).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_are_namespaced_and_bounded() {
        assert!(valid_key("kc.apiBase"));
        assert!(valid_key("kc.lastBoard:proj-1"));
        assert!(!valid_key("apiBase"));
        assert!(!valid_key("kc.../etc"));
        assert!(!valid_key(&format!("kc.{}", "x".repeat(80))));
    }

    #[test]
    fn round_trips_through_the_file() {
        let dir = std::env::temp_dir().join(format!("kc-settings-test-{}", std::process::id()));
        let path = dir.join("settings.json");
        let mut s = Settings::load(path.clone());
        s.set(HOST_KEY, "https://me.example.com").unwrap();
        s.set("kc.pushEnabled", "0").unwrap();
        assert!(s.set("nope", "x").is_err());
        let again = Settings::load(path.clone());
        assert_eq!(again.get(HOST_KEY), Some("https://me.example.com"));
        assert_eq!(again.get("kc.pushEnabled"), Some("0"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn session_items_never_reach_the_file() {
        let dir = std::env::temp_dir().join(format!("kc-settings-session-{}", std::process::id()));
        let path = dir.join("settings.json");
        let mut s = Settings::load(path.clone());
        s.set(HOST_KEY, "https://me.example.com").unwrap();
        s.set("kc.session.navState", r#"{"index":3}"#).unwrap();
        assert_eq!(s.get("kc.session.navState"), Some(r#"{"index":3}"#));
        let on_disk = std::fs::read_to_string(&path).unwrap();
        assert!(!on_disk.contains("kc.session"), "{on_disk}");
        // A relaunch starts without it, even if an old file carried one.
        std::fs::write(&path, r#"{"kc.apiBase":"https://me.example.com","kc.session.navState":"x"}"#).unwrap();
        let again = Settings::load(path);
        assert_eq!(again.get("kc.session.navState"), None);
        assert_eq!(again.get(HOST_KEY), Some("https://me.example.com"));
        s.remove("kc.session.navState").unwrap();
        assert_eq!(s.get("kc.session.navState"), None);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn session_items_are_bounded() {
        let mut s = Settings::default();
        for i in 0..MAX_SESSION_KEYS {
            s.set(&format!("kc.session.k{i}"), "v").unwrap();
        }
        assert!(s.set("kc.session.one-more", "v").is_err());
        assert!(s.set("kc.session.k0", "replaced").is_ok());
        assert!(s.set("kc.session.big", &"x".repeat(MAX_VALUE + 1)).is_err());
    }
}
