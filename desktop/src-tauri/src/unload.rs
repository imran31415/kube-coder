//! Dropping the webview after the window has been hidden for a while.
//!
//! The webview is nearly all of the app's memory, and the core needs none of
//! it to hold the stream, raise alerts and badge the dock (desktop/README.md).
//! Linux and Windows destroy the window as soon as it is closed
//! (`close_to_tray` in lib.rs). macOS hides it instead, because a Mac app that
//! stays in the dock is expected to come back the way it was left — so there
//! the window is destroyed only once it has stayed hidden for
//! `KC_DESKTOP_UNLOAD_AFTER_MIN` minutes (default 10). Opening it again
//! rebuilds it, and the UI restores the screen it was on (the
//! `kc.session.navState` item, see settings.rs).
//!
//! This module is the timing alone, with no Tauri in it, so it is tested on
//! every OS: lib.rs asks it for a ticket when it hides the window, sleeps the
//! delay, and destroys the window only if the ticket is still the current one.

use std::time::Duration;

pub const DEFAULT_AFTER: Duration = Duration::from_secs(10 * 60);

/// `KC_DESKTOP_UNLOAD_AFTER_MIN`: minutes hidden before the webview is
/// dropped. Unset or unreadable means the default; `0` drops it on close,
/// like Linux and Windows; `off` (or `never`) keeps it for as long as the app
/// runs, which is the Phase 1 behaviour.
pub fn after_from(v: Option<&str>) -> Option<Duration> {
    let Some(v) = v.map(str::trim).filter(|v| !v.is_empty()) else { return Some(DEFAULT_AFTER) };
    if v.eq_ignore_ascii_case("off") || v.eq_ignore_ascii_case("never") {
        return None;
    }
    match v.parse::<u64>() {
        Ok(m) => Some(Duration::from_secs(m.saturating_mul(60))),
        Err(_) => Some(DEFAULT_AFTER),
    }
}

/// Whether the hidden window may be dropped, as a sequence of tickets: each
/// hide issues a new one, and a show (or another hide) makes every earlier
/// ticket stale, so a timer started before the user came back never fires.
#[derive(Debug)]
pub struct HiddenUnload {
    after: Option<Duration>,
    ticket: u64,
    hidden: bool,
}

impl HiddenUnload {
    pub fn new(after: Option<Duration>) -> Self {
        HiddenUnload { after, ticket: 0, hidden: false }
    }

    /// The window was hidden. Returns the ticket and how long to wait before
    /// asking `due`, or None when unloading is off.
    pub fn hidden(&mut self) -> Option<(u64, Duration)> {
        self.ticket += 1;
        self.hidden = true;
        self.after.map(|d| (self.ticket, d))
    }

    /// The window is on screen again (or was rebuilt).
    pub fn shown(&mut self) {
        self.ticket += 1;
        self.hidden = false;
    }

    /// Whether the timer holding `ticket` may drop the webview now: the window
    /// is still hidden and has not been shown or hidden again since.
    pub fn due(&self, ticket: u64) -> bool {
        self.hidden && ticket == self.ticket
    }

    /// The window was destroyed: nothing is hidden any more.
    pub fn unloaded(&mut self, ticket: u64) {
        if ticket == self.ticket {
            self.hidden = false;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIN: Duration = Duration::from_secs(60);

    #[test]
    fn the_setting_reads_minutes_zero_and_off() {
        assert_eq!(after_from(None), Some(DEFAULT_AFTER));
        assert_eq!(after_from(Some("")), Some(DEFAULT_AFTER));
        assert_eq!(after_from(Some(" 3 ")), Some(3 * MIN));
        assert_eq!(after_from(Some("0")), Some(Duration::ZERO));
        assert_eq!(after_from(Some("off")), None);
        assert_eq!(after_from(Some("Never")), None);
        // A typo is not "never": it keeps the default rather than holding
        // the webview forever.
        assert_eq!(after_from(Some("ten")), Some(DEFAULT_AFTER));
        assert_eq!(after_from(Some("-5")), Some(DEFAULT_AFTER));
    }

    #[test]
    fn a_window_left_hidden_is_unloaded() {
        let mut u = HiddenUnload::new(Some(10 * MIN));
        let (t, wait) = u.hidden().unwrap();
        assert_eq!(wait, 10 * MIN);
        assert!(u.due(t));
        u.unloaded(t);
        assert!(!u.due(t), "a destroyed window is not unloaded twice");
    }

    #[test]
    fn showing_it_again_cancels_the_timer() {
        let mut u = HiddenUnload::new(Some(10 * MIN));
        let (t, _) = u.hidden().unwrap();
        u.shown();
        assert!(!u.due(t));
    }

    #[test]
    fn only_the_latest_hide_counts() {
        // Hidden, shown, hidden again: the first timer wakes 10 minutes after
        // the FIRST hide, which may be a minute after the second. It must not
        // drop a window that has only just been hidden.
        let mut u = HiddenUnload::new(Some(10 * MIN));
        let (first, _) = u.hidden().unwrap();
        u.shown();
        let (second, _) = u.hidden().unwrap();
        assert!(!u.due(first));
        assert!(u.due(second));
        // A stale timer's `unloaded` does not clear the live one.
        u.unloaded(first);
        assert!(u.due(second));
    }

    #[test]
    fn off_never_schedules() {
        let mut u = HiddenUnload::new(after_from(Some("off")));
        assert_eq!(u.hidden(), None);
    }

    #[test]
    fn zero_unloads_on_close() {
        let mut u = HiddenUnload::new(after_from(Some("0")));
        let (t, wait) = u.hidden().unwrap();
        assert_eq!(wait, Duration::ZERO);
        assert!(u.due(t));
    }
}
