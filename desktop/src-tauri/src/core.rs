//! The always-running half of the app: the connection, the live event stream,
//! the Feed alerts and the badge. It runs whether or not the window is shown,
//! which is what lets the app sit in the tray and still say "needs you".
//!
//! ## Stream, then poll
//!
//! The core holds `GET /api/events` (handlers/workspace.py) with the Bearer
//! header and forwards each named event to the UI as `kc://event`. While that
//! stream is healthy the UI stops its interval polling (`usePolling` in the
//! mobile app). If the stream fails, the core re-fetches the Feed on a backoff
//! and the UI falls back to its normal polling, so nothing goes stale.
//!
//! ## Letting a workspace sleep (#728)
//!
//! A held connection to the workspace counts as a "dashboard client" in
//! `keeper_idle.py`, which would keep a keeper-managed workspace awake for as
//! long as the app runs. So on a workspace with a keeper, the core lets go of
//! the workspace — no stream, no polling — once the window has gone
//! unattended (unfocused) for `KC_DESKTOP_RELEASE_AFTER_MIN` minutes
//! (default 20), and picks up again the moment the window is focused. That is
//! the cost of a sleeping workspace: an alert raised while you were away is
//! shown when you come back, not before. A workspace without a keeper never
//! sleeps, so there the stream is held for as long as the app runs.
//!
//! While asleep the core only asks the keeper (`/api/keeper/status`, served by
//! the keeper, never by the pod), which cannot wake anything. It never calls
//! `/api/keeper/wake`; only the user's tap on the wake banner does.

use crate::feed::{self, Alert, FeedItem, Ledger};
use crate::http::{self, Conn};
use crate::scope;
use crate::secrets::{self, Token};
use crate::settings::{self, Settings};
use crate::sse::SseParser;
use crate::unload::{self, HiddenUnload};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::{watch, Notify};

pub const MAIN: &str = "main";
const FEED_PATH: &str = "/api/feed?limit=200";
/// The server sends a keep-alive every 15 s; three missed means the
/// connection is dead even if the socket has not noticed.
const STREAM_IDLE: Duration = Duration::from_secs(45);
/// A burst of feed.item events (a board run finishing ten items) is one fetch.
const FEED_DEBOUNCE: Duration = Duration::from_millis(750);
/// While streaming, still re-read the Feed now and then: marking a row read on
/// another device publishes no event, and the badge should catch up.
const FEED_SAFETY: Duration = Duration::from_secs(300);
const BACKOFF_MIN: Duration = Duration::from_secs(2);
const BACKOFF_MAX: Duration = Duration::from_secs(60);
const ASLEEP_RECHECK: Duration = Duration::from_secs(60);

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Something to open in the UI: a deep link, or a notification's target.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Target {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(rename = "ref", skip_serializing_if = "Option::is_none")]
    pub reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub feed_id: Option<String>,
}

/// Only our own scheme, and nothing absurdly long. The UI parses the rest
/// (`mobile/src/desktop/deepLink.ts`), the same code a notification tap uses.
pub fn accept_deep_link(url: &str) -> Option<Target> {
    let u = url.trim();
    if u.len() > 2048 || !u.to_ascii_lowercase().starts_with("kubecoder:") {
        return None;
    }
    if u.chars().any(|c| c.is_control()) {
        return None;
    }
    Some(Target { url: Some(u.to_string()), reference: None, feed_id: None })
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Mode {
    /// No workspace connected.
    Off,
    Connecting,
    /// The event stream is up: the UI can stop polling.
    Stream,
    /// The stream is down; the core and the UI poll.
    Polling,
    /// Let go so a keeper can sleep the workspace (see the module docs).
    Paused,
    Asleep,
    /// The token was refused. Nothing retries until the connection changes.
    Unauthorized,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LiveStatus {
    pub mode: Mode,
    pub keeper: Option<bool>,
    pub needs_you: usize,
    /// Feed fetches the core made, for the polling measurement in the README.
    pub feed_fetches: u64,
}

pub struct Core {
    app: AppHandle,
    pub http: reqwest::Client,
    pub settings: Mutex<Settings>,
    token: Mutex<Option<Token>>,
    conn_tx: watch::Sender<Option<Conn>>,
    attended_tx: watch::Sender<bool>,
    focus: Mutex<(bool, Instant)>,
    release_after: Duration,
    min_interval: f64,
    kick: Notify,
    feed_dirty: Notify,
    status: Mutex<LiveStatus>,
    targets: Mutex<VecDeque<Target>>,
    notes: Mutex<HashMap<i32, Target>>,
    next_note: AtomicI32,
    data_dir: PathBuf,
    /// When a hidden window's webview is dropped (macOS; see unload.rs).
    unload: Mutex<HiddenUnload>,
}

impl Core {
    pub fn new(app: AppHandle, config_dir: PathBuf, data_dir: PathBuf) -> Arc<Self> {
        let settings = Settings::load(config_dir.join("settings.json"));
        let token = match secrets::get(secrets::TOKEN_KEY) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("[kc] {e}");
                None
            }
        };
        let release_after = std::env::var("KC_DESKTOP_RELEASE_AFTER_MIN")
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(|m| Duration::from_secs(m * 60))
            .unwrap_or(Duration::from_secs(20 * 60));
        let core = Arc::new(Core {
            app,
            http: http::client(),
            settings: Mutex::new(settings),
            token: Mutex::new(token),
            conn_tx: watch::channel(None).0,
            attended_tx: watch::channel(true).0,
            focus: Mutex::new((true, Instant::now())),
            release_after,
            min_interval: feed::min_interval_from(std::env::var("KC_PUSH_MIN_INTERVAL").ok().as_deref()),
            kick: Notify::new(),
            feed_dirty: Notify::new(),
            status: Mutex::new(LiveStatus { mode: Mode::Off, keeper: None, needs_you: 0, feed_fetches: 0 }),
            targets: Mutex::new(VecDeque::new()),
            notes: Mutex::new(HashMap::new()),
            next_note: AtomicI32::new(1),
            data_dir,
            unload: Mutex::new(HiddenUnload::new(unload::after_from(
                std::env::var("KC_DESKTOP_UNLOAD_AFTER_MIN").ok().as_deref(),
            ))),
        });
        core.seed_dev_connection();
        core.refresh_conn();
        core
    }

    /// `KC_DESKTOP_HOST` + `KC_DESKTOP_TOKEN` pre-seed a connection for a test
    /// run, like the mobile `EXPO_PUBLIC_HOST`/`EXPO_PUBLIC_TOKEN`. Only in a
    /// build compiled with `KC_ALLOW_CLEARTEXT` (a development build), and the
    /// token stays in memory: it is never written to the keychain.
    fn seed_dev_connection(&self) {
        if !scope::cleartext_allowed() {
            return;
        }
        let (Ok(host), Ok(token)) = (std::env::var("KC_DESKTOP_HOST"), std::env::var("KC_DESKTOP_TOKEN")) else {
            return;
        };
        if let Ok(base) = scope::validate_host(&host, true) {
            let _ = lock(&self.settings).set(settings::HOST_KEY, base.as_str().trim_end_matches('/'));
            *lock(&self.token) = Some(Token::new(token.trim()));
        }
    }

    pub fn conn(&self) -> Option<Conn> {
        self.conn_tx.borrow().clone()
    }

    /// Recompute the connection from the saved host and the token. Called on
    /// every host or token write; a change restarts the live session.
    pub fn refresh_conn(&self) {
        let host = lock(&self.settings).get(settings::HOST_KEY).unwrap_or("").to_string();
        let token = lock(&self.token).clone().filter(|t| !t.is_empty());
        let next = match (scope::validate_host(&host, scope::cleartext_allowed()), token) {
            (Ok(base), Some(token)) if !host.is_empty() => Some(Conn { base, token }),
            _ => None,
        };
        self.conn_tx.send_if_modified(|cur| {
            if *cur == next {
                return false;
            }
            *cur = next;
            true
        });
    }

    // ---- storage commands -------------------------------------------------

    pub fn secret_get(&self, key: &str) -> Result<Option<String>, String> {
        if !secrets::is_allowed_key(key) {
            return Err("unknown secret".into());
        }
        if key == secrets::TOKEN_KEY {
            // Never the token itself: see secrets.rs.
            return Ok(lock(&self.token).as_ref().filter(|t| !t.is_empty()).map(|t| secrets::mask(t.expose())));
        }
        Ok(None) // the controller token: not on desktop
    }

    pub fn secret_set(&self, key: &str, value: &str) -> Result<(), String> {
        if !secrets::is_allowed_key(key) {
            return Err("unknown secret".into());
        }
        if secrets::is_mask(value) {
            return Ok(()); // the webview echoing back what it was shown
        }
        if value.is_empty() {
            return self.secret_delete(key);
        }
        if key != secrets::TOKEN_KEY {
            return Err("The desktop app has no controller connection.".into());
        }
        let token = Token::new(value);
        let stored = secrets::set(key, &token);
        // Usable for this session even if the keychain refused it; the error
        // still reaches the caller.
        *lock(&self.token) = Some(token);
        self.refresh_conn();
        stored
    }

    pub fn secret_delete(&self, key: &str) -> Result<(), String> {
        if !secrets::is_allowed_key(key) {
            return Err("unknown secret".into());
        }
        if key != secrets::TOKEN_KEY {
            return Ok(());
        }
        *lock(&self.token) = None;
        self.refresh_conn();
        secrets::delete(key)
    }

    pub fn item_set(&self, key: &str, value: &str) -> Result<(), String> {
        if key == settings::HOST_KEY && !value.is_empty() {
            scope::validate_host(value, scope::cleartext_allowed())?;
        }
        lock(&self.settings).set(key, value)?;
        if key == settings::HOST_KEY {
            self.refresh_conn();
        }
        Ok(())
    }

    pub fn item_get(&self, key: &str) -> Option<String> {
        lock(&self.settings).get(key).map(str::to_string)
    }

    pub fn item_remove(&self, key: &str) -> Result<(), String> {
        lock(&self.settings).remove(key)?;
        if key == settings::HOST_KEY {
            self.refresh_conn();
        }
        Ok(())
    }

    // ---- UI requests --------------------------------------------------------

    pub async fn request(&self, req: http::WsRequest) -> Result<http::WsResponse, String> {
        let conn = self.conn().ok_or("Not configured")?;
        // Reading or dismissing a row changes the badge, and the server sends
        // no event for it: re-read the Feed once the write has landed.
        let feed_write = req.method.eq_ignore_ascii_case("POST") && req.path.starts_with("/api/feed/");
        let resp = http::send(&self.http, &conn, req).await?;
        if feed_write {
            self.feed_dirty.notify_one();
        }
        Ok(resp)
    }

    /// The UI saw the workspace come back (a wake landed): reconnect now
    /// rather than at the next scheduled check.
    pub fn kick(&self) {
        self.kick.notify_one();
    }

    pub fn status(&self) -> LiveStatus {
        lock(&self.status).clone()
    }

    fn set_status(&self, f: impl FnOnce(&mut LiveStatus)) {
        let snapshot = {
            let mut s = lock(&self.status);
            let before = s.clone();
            f(&mut s);
            if *s == before {
                return;
            }
            s.clone()
        };
        let _ = self.app.emit_to(MAIN, "kc://live", snapshot);
    }

    // ---- focus / attention -------------------------------------------------

    pub fn on_focus(&self, focused: bool) {
        let mut f = lock(&self.focus);
        *f = (focused, Instant::now());
        drop(f);
        self.attended_tx.send_replace(true);
    }

    fn attended(&self) -> bool {
        let (focused, since) = *lock(&self.focus);
        focused || since.elapsed() < self.release_after
    }

    /// How long until an unfocused window stops counting as attended.
    fn attention_left(&self) -> Option<Duration> {
        let (focused, since) = *lock(&self.focus);
        if focused {
            None
        } else {
            Some(self.release_after.saturating_sub(since.elapsed()))
        }
    }

    // ---- the window's lifetime (unload.rs) ---------------------------------

    /// The window was hidden: when to ask `take_unload`, if ever.
    pub fn on_hidden(&self) -> Option<(u64, Duration)> {
        lock(&self.unload).hidden()
    }

    pub fn on_shown(&self) {
        lock(&self.unload).shown();
    }

    /// Whether the timer holding `ticket` should drop the webview now. A yes
    /// is final: the caller destroys the window.
    pub fn take_unload(&self, ticket: u64) -> bool {
        let mut u = lock(&self.unload);
        let due = u.due(ticket);
        if due {
            u.unloaded(ticket);
        }
        due
    }

    // ---- targets (deep links + notification clicks) ------------------------

    pub fn push_target(&self, t: Target) {
        {
            let mut q = lock(&self.targets);
            q.push_back(t);
            while q.len() > 16 {
                q.pop_front();
            }
        }
        show_main(&self.app);
        // A signal only: the UI drains the queue, so a target that arrives
        // before the UI listens is not lost, and none is routed twice.
        let _ = self.app.emit_to(MAIN, "kc://targets", ());
    }

    pub fn take_targets(&self) -> Vec<Target> {
        lock(&self.targets).drain(..).collect()
    }

    pub fn on_notification_action(&self, id: i32) {
        if let Some(t) = lock(&self.notes).remove(&id) {
            self.push_target(t);
        } else {
            show_main(&self.app);
        }
    }

    // ---- the live session -------------------------------------------------

    pub async fn run(self: Arc<Self>) {
        let mut conn_rx = self.conn_tx.subscribe();
        loop {
            let conn = conn_rx.borrow_and_update().clone();
            let Some(conn) = conn else {
                self.set_status(|s| {
                    s.mode = Mode::Off;
                    s.keeper = None;
                    s.needs_you = 0;
                });
                self.apply_badge(0);
                if conn_rx.changed().await.is_err() {
                    return;
                }
                continue;
            };
            tokio::select! {
                _ = self.clone().session(conn) => {}
                r = conn_rx.changed() => {
                    if r.is_err() {
                        return;
                    }
                }
            }
        }
    }

    async fn session(self: Arc<Self>, conn: Conn) {
        let ledger_path = self.data_dir.join(format!("alerts-{}.json", host_slug(&conn.base)));
        let mut ledger: Ledger = std::fs::read(&ledger_path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        let mut backoff = BACKOFF_MIN;
        let mut keeper: Option<Option<String>> = None;
        let mut attended_rx = self.attended_tx.subscribe();
        self.set_status(|s| {
            s.mode = Mode::Connecting;
            s.keeper = None;
        });
        loop {
            // 1. Is there a keeper, and what does it say? (Never wakes.)
            let state = match &keeper {
                Some(s) => s.clone(),
                None => {
                    let s = self.keeper_state(&conn).await;
                    keeper = Some(s.clone());
                    self.set_status(|st| st.keeper = Some(s.is_some()));
                    s
                }
            };
            if let Some(state) = &state {
                if state == "asleep" || state == "stopped" {
                    self.set_status(|s| s.mode = Mode::Asleep);
                    self.wait(ASLEEP_RECHECK, &mut attended_rx, false).await;
                    keeper = None;
                    continue;
                }
                if !self.attended() {
                    self.set_status(|s| s.mode = Mode::Paused);
                    // Only focus (or a kick) brings it back.
                    tokio::select! {
                        _ = attended_rx.changed() => {}
                        _ = self.kick.notified() => {}
                    }
                    keeper = None;
                    continue;
                }
            }
            // 2. Hold the stream.
            match self.stream(&conn, &mut ledger, &ledger_path, state.is_some(), &mut attended_rx).await {
                StreamEnd::Ended => {
                    backoff = BACKOFF_MIN;
                }
                StreamEnd::Released => {
                    keeper = None;
                }
                StreamEnd::Asleep => {
                    keeper = None;
                    self.set_status(|s| s.mode = Mode::Asleep);
                    self.wait(ASLEEP_RECHECK, &mut attended_rx, false).await;
                }
                StreamEnd::Unauthorized => {
                    self.set_status(|s| s.mode = Mode::Unauthorized);
                    self.kick.notified().await;
                    keeper = None;
                }
                StreamEnd::Failed => {
                    self.set_status(|s| s.mode = Mode::Polling);
                    // The fallback poll: the Feed, so alerts and the badge
                    // keep working while the stream cannot.
                    let _ = self.refresh_feed(&conn, &mut ledger, &ledger_path).await;
                    self.wait(backoff, &mut attended_rx, true).await;
                    backoff = (backoff * 2).min(BACKOFF_MAX);
                    // Ask the keeper again: the failure may have been sleep.
                    keeper = None;
                }
            }
        }
    }

    /// Sleep for `d`, or less when a kick (or, if `on_attention`, a focus
    /// change) says now is better.
    async fn wait(&self, d: Duration, attended_rx: &mut watch::Receiver<bool>, on_attention: bool) {
        tokio::select! {
            _ = tokio::time::sleep(d) => {}
            _ = self.kick.notified() => {}
            _ = attended_rx.changed(), if on_attention => {}
        }
    }

    /// The keeper's view of the workspace, or None when it has no keeper.
    async fn keeper_state(&self, conn: &Conn) -> Option<String> {
        #[derive(Deserialize)]
        struct S {
            state: String,
        }
        let url = scope::workspace_url(&conn.base, "/api/keeper/status").ok()?;
        let resp = self
            .http
            .get(url)
            .bearer_auth(conn.token.expose())
            .header("Accept", "application/json")
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .ok()?;
        if !resp.status().is_success() {
            return None;
        }
        resp.json::<S>().await.ok().map(|s| s.state)
    }

    async fn stream(
        &self,
        conn: &Conn,
        ledger: &mut Ledger,
        ledger_path: &PathBuf,
        has_keeper: bool,
        attended_rx: &mut watch::Receiver<bool>,
    ) -> StreamEnd {
        let Ok(url) = scope::workspace_url(&conn.base, "/api/events") else { return StreamEnd::Failed };
        let resp = match self
            .http
            .get(url)
            .bearer_auth(conn.token.expose())
            // JSON as well, so a keeper answers "asleep" in a form we can read.
            .header("Accept", "text/event-stream, application/json")
            .header("Cache-Control", "no-cache")
            .send()
            .await
        {
            Ok(r) => r,
            Err(_) => return StreamEnd::Failed,
        };
        let status = resp.status();
        let ctype = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();
        if status == 401 || status == 403 {
            return StreamEnd::Unauthorized;
        }
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return if is_sleeping(status.as_u16(), &ctype, &body) { StreamEnd::Asleep } else { StreamEnd::Failed };
        }
        if !ctype.starts_with("text/event-stream") {
            return StreamEnd::Failed;
        }

        let mut parser = SseParser::new();
        let mut body = resp.bytes_stream();
        let mut feed_due: Option<tokio::time::Instant> = None;
        let mut safety = tokio::time::interval(FEED_SAFETY);
        safety.reset();
        let mut ready = false;
        loop {
            let due = feed_due;
            let feed_sleep = async move {
                match due {
                    Some(t) => tokio::time::sleep_until(t).await,
                    None => std::future::pending().await,
                }
            };
            let left = if has_keeper { self.attention_left() } else { None };
            let release = async move {
                match left {
                    Some(left) => tokio::time::sleep(left).await,
                    None => std::future::pending().await,
                }
            };
            tokio::select! {
                chunk = tokio::time::timeout(STREAM_IDLE, body.next()) => {
                    let bytes = match chunk {
                        Ok(Some(Ok(b))) => b,
                        // EOF, a read error or 45 s of silence.
                        _ => return if ready { StreamEnd::Ended } else { StreamEnd::Failed },
                    };
                    for ev in parser.feed(&bytes).events {
                        match ev.event.as_str() {
                            "ready" => {
                                ready = true;
                                self.set_status(|s| s.mode = Mode::Stream);
                                feed_due = Some(tokio::time::Instant::now());
                            }
                            // The server's lifetime cap: reconnect straight away.
                            "end" => return StreamEnd::Ended,
                            name => {
                                if name.starts_with("feed.") {
                                    feed_due.get_or_insert(tokio::time::Instant::now() + FEED_DEBOUNCE);
                                }
                                let data: serde_json::Value =
                                    serde_json::from_str(&ev.data).unwrap_or(serde_json::Value::Null);
                                let _ = self.app.emit_to(
                                    MAIN,
                                    "kc://event",
                                    serde_json::json!({ "type": name, "data": data }),
                                );
                            }
                        }
                    }
                }
                _ = feed_sleep => {
                    feed_due = None;
                    if self.refresh_feed(conn, ledger, ledger_path).await == Err(Down::Asleep) {
                        return StreamEnd::Asleep;
                    }
                }
                _ = safety.tick() => {
                    feed_due.get_or_insert(tokio::time::Instant::now());
                }
                _ = self.feed_dirty.notified() => {
                    feed_due.get_or_insert(tokio::time::Instant::now() + FEED_DEBOUNCE);
                }
                _ = release => return StreamEnd::Released,
                _ = attended_rx.changed() => {}
            }
        }
    }

    /// Fetch the Feed, raise whatever alerts it calls for, and set the badge.
    async fn refresh_feed(&self, conn: &Conn, ledger: &mut Ledger, ledger_path: &PathBuf) -> Result<(), Down> {
        #[derive(Deserialize)]
        struct Resp {
            #[serde(default)]
            items: Vec<FeedItem>,
        }
        let url = scope::workspace_url(&conn.base, FEED_PATH).map_err(|_| Down::Other)?;
        self.set_status(|s| s.feed_fetches += 1);
        let resp = self
            .http
            .get(url)
            .bearer_auth(conn.token.expose())
            .header("Accept", "application/json")
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .map_err(|_| Down::Other)?;
        let status = resp.status().as_u16();
        let ctype = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();
        let body = resp.text().await.map_err(|_| Down::Other)?;
        if !(200..300).contains(&status) {
            return Err(if is_sleeping(status, &ctype, &body) { Down::Asleep } else { Down::Other });
        }
        let items = serde_json::from_str::<Resp>(&body).map_err(|_| Down::Other)?.items;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let alerts = ledger.observe(&items, now, self.min_interval);
        if let Ok(bytes) = serde_json::to_vec(ledger) {
            let _ = settings::write_atomic(ledger_path, &bytes);
        }
        if self.item_get("kc.pushEnabled").as_deref() != Some("0") {
            for a in alerts {
                self.notify(a);
            }
        }
        let count = feed::needs_you_count(&items);
        self.set_status(|s| s.needs_you = count);
        self.apply_badge(count);
        Ok(())
    }

    fn notify(&self, alert: Alert) {
        use tauri_plugin_notification::NotificationExt;
        let id = self.next_note.fetch_add(1, Ordering::Relaxed);
        {
            let mut notes = lock(&self.notes);
            notes.insert(
                id,
                Target {
                    url: None,
                    reference: Some(alert.reference.clone()).filter(|r| !r.is_empty()),
                    feed_id: Some(alert.feed_id.clone()),
                },
            );
            // Notifications nobody clicked: keep the map small.
            if notes.len() > 64 {
                let oldest = *notes.keys().min().unwrap_or(&id);
                notes.remove(&oldest);
            }
        }
        let mut b = self.app.notification().builder().id(id).title(alert.title);
        if !alert.body.is_empty() {
            b = b.body(alert.body);
        }
        if let Err(e) = b.show() {
            eprintln!("[kc] notification failed: {e}");
        }
    }

    fn apply_badge(&self, count: usize) {
        crate::tray::set_needs_you(&self.app, count);
    }
}

#[derive(Debug, PartialEq)]
enum StreamEnd {
    /// The server closed it on purpose (lifetime cap) or it went quiet after
    /// working: reconnect at once.
    Ended,
    /// Never got going: back off and poll.
    Failed,
    Asleep,
    Unauthorized,
    /// Let go for the keeper (window unattended).
    Released,
}

#[derive(Debug, PartialEq)]
enum Down {
    Asleep,
    Other,
}

/// The keeper's away answer (`workspace_sleeping`), the same test
/// `classifyDown` makes in mobile/src/util/workspaceReach.ts.
pub fn is_sleeping(status: u16, content_type: &str, body: &str) -> bool {
    if !content_type.contains("application/json") {
        return false;
    }
    let _ = status;
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .map(|v| v.get("code").and_then(|c| c.as_str()) == Some("workspace_sleeping")
            || v.get("error").and_then(|c| c.as_str()) == Some("workspace_sleeping"))
        .unwrap_or(false)
}

/// A file-name-safe tag for one host, so two workspaces keep two ledgers.
fn host_slug(base: &url::Url) -> String {
    let s: String = format!("{}-{}", base.host_str().unwrap_or("host"), base.port_or_known_default().unwrap_or(0))
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '.' { c } else { '_' })
        .collect();
    s.chars().take(100).collect()
}

/// Bring the UI forward, rebuilding the window if closing it destroyed it
/// (see `close_to_tray` in lib.rs) or it was dropped after a while hidden
/// (unload.rs). A rebuilt UI boots, hydrates, restores the screen it was on
/// and drains the target queue, so whatever asked for the window still lands.
pub fn show_main(app: &AppHandle) {
    let window = match app.get_webview_window(MAIN) {
        Some(w) => w,
        None => {
            let Some(cfg) = app.config().app.windows.iter().find(|w| w.label == MAIN) else { return };
            match tauri::WebviewWindowBuilder::from_config(app, cfg).and_then(|b| b.build()) {
                Ok(w) => {
                    if let Some(core) = app.try_state::<Arc<Core>>() {
                        crate::tray::set_needs_you(app, core.status().needs_you);
                    }
                    w
                }
                Err(e) => {
                    eprintln!("[kc] could not open the window: {e}");
                    return;
                }
            }
        }
    };
    if let Some(core) = app.try_state::<Arc<Core>>() {
        core.on_shown();
    }
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deep_links_are_our_scheme_only() {
        assert!(accept_deep_link("kubecoder://board/b1/i1").is_some());
        assert!(accept_deep_link("KubeCoder://task/t1").is_some());
        assert!(accept_deep_link("https://evil.com").is_none());
        assert!(accept_deep_link("javascript:alert(1)").is_none());
        assert!(accept_deep_link(&format!("kubecoder://task/{}", "x".repeat(3000))).is_none());
        assert!(accept_deep_link("kubecoder://task/t1\n").is_some()); // trimmed
        assert!(accept_deep_link("kubecoder://task/t\u{0}1").is_none());
    }

    #[test]
    fn sleeping_is_the_keeper_json_only() {
        assert!(is_sleeping(503, "application/json", r#"{"code":"workspace_sleeping","error":"Your workspace is asleep."}"#));
        assert!(!is_sleeping(503, "text/html", "<html>asleep</html>"));
        assert!(!is_sleeping(503, "application/json", r#"{"error":"busy"}"#));
    }

    #[test]
    fn host_slugs_are_file_safe() {
        let u = url::Url::parse("https://me.example.com/").unwrap();
        assert_eq!(host_slug(&u), "me.example.com-443");
        let u = url::Url::parse("http://[::1]:6080/").unwrap();
        assert!(host_slug(&u).chars().all(|c| c.is_ascii_alphanumeric() || "-._".contains(c)));
    }

    #[test]
    fn targets_serialize_for_the_ui() {
        let t = Target { url: None, reference: Some("board:b:1".into()), feed_id: Some("f1".into()) };
        assert_eq!(serde_json::to_string(&t).unwrap(), r#"{"ref":"board:b:1","feedId":"f1"}"#);
    }
}
