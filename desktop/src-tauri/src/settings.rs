//! Non-secret preferences the webview stores (`getItem`/`setItem` in
//! `mobile/src/store/storage.desktop.ts`): the host, the Notifications switch,
//! the last-opened board and so on. A small JSON file in the app's config
//! directory, written atomically. Never a secret: those go to `secrets.rs`.
//!
//! It lives in Rust rather than localStorage because the core needs the host
//! before any webview exists, to hold the event stream and raise notifications
//! from the tray.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

pub const HOST_KEY: &str = "kc.apiBase";
const MAX_KEY: usize = 64;
const MAX_VALUE: usize = 64 * 1024;
const MAX_KEYS: usize = 256;

#[derive(Debug, Default)]
pub struct Settings {
    path: Option<PathBuf>,
    items: BTreeMap<String, String>,
}

pub fn valid_key(key: &str) -> bool {
    key.starts_with("kc.")
        && key.len() <= MAX_KEY
        && key.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | ':'))
}

impl Settings {
    pub fn load(path: PathBuf) -> Self {
        let items = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice::<BTreeMap<String, String>>(&b).ok())
            .unwrap_or_default()
            .into_iter()
            .filter(|(k, _)| valid_key(k))
            .collect();
        Settings { path: Some(path), items }
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.items.get(key).map(String::as_str)
    }

    pub fn set(&mut self, key: &str, value: &str) -> Result<(), String> {
        if !valid_key(key) {
            return Err("invalid key".into());
        }
        if value.len() > MAX_VALUE {
            return Err("value too large".into());
        }
        if !self.items.contains_key(key) && self.items.len() >= MAX_KEYS {
            return Err("too many keys".into());
        }
        self.items.insert(key.to_string(), value.to_string());
        self.save()
    }

    pub fn remove(&mut self, key: &str) -> Result<(), String> {
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
}
