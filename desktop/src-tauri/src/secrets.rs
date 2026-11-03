//! The API token lives in the OS credential store and nowhere else on disk:
//! the macOS Keychain, the Windows Credential Manager, or the Secret Service
//! (GNOME Keyring / KWallet) on Linux, all through the `keyring` crate.
//!
//! The webview's `getSecret`/`setSecret` calls land here (see
//! `mobile/src/store/storage.desktop.ts`). Only the two keys the app stores are
//! accepted. The workspace token is never handed back to the webview: a read
//! returns a mask that keeps its last four characters, which is all the
//! Settings screen shows, so after a restart the token exists only in this
//! process and the keychain.

use std::fmt;

const SERVICE: &str = "kube-coder-desktop";
/// The workspace Bearer token (`TOKEN_KEY` in mobile/src/store/config.ts).
pub const TOKEN_KEY: &str = "kc.devToken";
/// The optional controller token (`CTRL_TOKEN_KEY`). The desktop app has no
/// controller connection (mobile/src/store/config.ts `hasController`), so it
/// is accepted and answered without ever touching the keychain.
pub const CTRL_TOKEN_KEY: &str = "kc.controllerToken";

const MASK_PREFIX: &str = "\u{2022}\u{2022}\u{2022}\u{2022}";

pub fn is_allowed_key(key: &str) -> bool {
    key == TOKEN_KEY || key == CTRL_TOKEN_KEY
}

/// A token that redacts itself in `Debug`/`Display`, so a stray `{:?}` in a
/// log line can never print it.
#[derive(Clone, PartialEq, Eq, Default)]
pub struct Token(String);

impl Token {
    pub fn new(s: impl Into<String>) -> Self {
        Token(s.into())
    }
    pub fn expose(&self) -> &str {
        &self.0
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Token(<redacted>)")
    }
}

impl fmt::Display for Token {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("<redacted>")
    }
}

/// What the webview sees for the workspace token.
pub fn mask(token: &str) -> String {
    let tail: String = {
        let chars: Vec<char> = token.chars().collect();
        chars[chars.len().saturating_sub(4)..].iter().collect()
    };
    if token.chars().count() <= 4 {
        return MASK_PREFIX.to_string();
    }
    format!("{MASK_PREFIX}{tail}")
}

/// True for a value the webview read back as a mask and is now writing again.
pub fn is_mask(value: &str) -> bool {
    value.starts_with(MASK_PREFIX)
}

/// A keychain that does not answer must not hang the app: on Linux the store
/// is a D-Bus service that can be missing, locked behind a prompt or wedged.
const KEYCHAIN_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

fn entry(key: &str) -> keyring::Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, key)
}

/// Run one keychain operation on its own thread, and give up after
/// `KEYCHAIN_TIMEOUT`. The thread is left to finish (or not) on its own.
fn bounded<T: Send + 'static>(op: impl FnOnce() -> Result<T, String> + Send + 'static) -> Result<T, String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::Builder::new()
        .name("kc-keychain".into())
        .spawn(move || {
            let _ = tx.send(op());
        })
        .map_err(|e| e.to_string())?;
    rx.recv_timeout(KEYCHAIN_TIMEOUT)
        .unwrap_or_else(|_| Err("The system keychain did not answer.".into()))
}

pub fn get(key: &str) -> Result<Option<Token>, String> {
    let key = key.to_string();
    bounded(move || match entry(&key).and_then(|e| e.get_password()) {
        Ok(v) => Ok(Some(Token::new(v))),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(describe(e)),
    })
}

pub fn set(key: &str, value: &Token) -> Result<(), String> {
    let (key, value) = (key.to_string(), value.clone());
    bounded(move || entry(&key).and_then(|e| e.set_password(value.expose())).map_err(describe))
}

pub fn delete(key: &str) -> Result<(), String> {
    let key = key.to_string();
    bounded(move || match entry(&key).and_then(|e| e.delete_credential()) {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(describe(e)),
    })
}

/// The error without anything that could echo a secret back.
fn describe(e: keyring::Error) -> String {
    match e {
        keyring::Error::NoStorageAccess(_) | keyring::Error::PlatformFailure(_) => {
            "The system keychain is not available (on Linux, is a Secret Service such as \
             GNOME Keyring or KWallet running?)"
                .into()
        }
        other => format!("keychain error: {}", other.to_string().chars().take(160).collect::<String>()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_never_print() {
        let t = Token::new("kc_live_supersecret");
        assert_eq!(format!("{t:?}"), "Token(<redacted>)");
        assert_eq!(format!("{t}"), "<redacted>");
        assert!(!format!("{:?}", Some(t)).contains("supersecret"));
    }

    #[test]
    fn mask_keeps_only_the_tail() {
        assert_eq!(mask("abcdefgh1234"), "\u{2022}\u{2022}\u{2022}\u{2022}1234");
        assert_eq!(mask("abc"), MASK_PREFIX);
        assert!(is_mask(&mask("abcdefgh1234")));
        assert!(!is_mask("abcdefgh1234"));
    }

    #[test]
    fn only_the_app_keys_are_accepted() {
        assert!(is_allowed_key(TOKEN_KEY));
        assert!(is_allowed_key(CTRL_TOKEN_KEY));
        assert!(!is_allowed_key("anything.else"));
    }
}
