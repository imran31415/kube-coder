//! Which Feed rows become a native notification, and the dock badge count.
//!
//! The phone hears about these through push, and the server decides
//! (`charts/workspace/push_notify.py`). The desktop app has no push channel, so
//! it makes the same decision locally over the rows `/api/feed` returns,
//! applying the same rules:
//!
//! - **What alerts** (`should_push`): a row an agent is blocked on
//!   (`waiting`), or a recorded `decision`.
//! - **Coalescing** (`dedupe_key`): the Feed keeps re-emits of one alert on
//!   one row, under one id, with a fresh `ts`. A repeat alerts only once the
//!   user has read that row since (`seen`), or when it is a different row (the
//!   old one was dismissed), and never more often than the minimum interval
//!   (`_may_repeat`, `KC_PUSH_MIN_INTERVAL`, default 1800 s).
//! - **Staleness**: push drops an alert older than an hour (`PUSH_TTL_SECONDS`)
//!   rather than deliver it late; so does this.
//!
//! `/api/feed` does not expose `dedupe_key` (feed_test asserts it), so the key
//! is the row's primary link ref — `board:<board>:<item>`, `task:<id>`, … —
//! which is what the server's own keys encode, and falls back to the row id.
//! A `dedupe_key` is used as-is if the server ever sends one.
//!
//! The first look at a workspace only records what is already there: rows the
//! phone was paged about hours ago are not news on a laptop that just opened.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const DEFAULT_MIN_INTERVAL: f64 = 1800.0;
pub const ALERT_TTL: f64 = 3600.0;
const RETENTION: f64 = 7.0 * 24.0 * 3600.0;

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq)]
pub struct FeedLink {
    #[serde(rename = "ref", default)]
    pub reference: String,
    #[serde(default)]
    pub label: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq)]
pub struct FeedItem {
    pub id: String,
    #[serde(default)]
    pub ts: f64,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub body_md: String,
    #[serde(default)]
    pub links: Vec<FeedLink>,
    #[serde(default)]
    pub waiting: bool,
    #[serde(default)]
    pub read: bool,
    #[serde(default)]
    pub dedupe_key: String,
}

impl FeedItem {
    /// The first link with a ref — the row's deep link, as push sends it.
    pub fn primary_ref(&self) -> &str {
        self.links
            .iter()
            .map(|l| l.reference.trim())
            .find(|r| !r.is_empty())
            .unwrap_or("")
    }

    /// The coalescing key: the server's `dedupe_key` when present, else the
    /// target ref, else the row itself.
    pub fn alert_key(&self) -> String {
        if !self.dedupe_key.is_empty() {
            return self.dedupe_key.clone();
        }
        let r = self.primary_ref();
        if !r.is_empty() {
            return format!("{}:{}", if self.waiting { "waiting" } else { &self.kind }, r);
        }
        format!("id:{}", self.id)
    }
}

/// `push_notify.should_push`.
pub fn should_alert(item: &FeedItem) -> bool {
    item.waiting || item.kind == "decision"
}

/// The badge: rows still waiting on the human and not yet read. The same Feed
/// rows (and the same `waiting && !read` rule) as the phone's drawer badge and
/// Needs-you count, across every kind rather than boards alone — a Build
/// parked on a question needs you as much as a review does.
pub fn needs_you_count(items: &[FeedItem]) -> usize {
    items.iter().filter(|i| i.waiting && !i.read).count()
}

/// One notification to show.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Alert {
    pub feed_id: String,
    pub reference: String,
    pub title: String,
    pub body: String,
}

impl Alert {
    /// Title and body exactly as `push_notify._build_messages` words them.
    fn from_item(item: &FeedItem) -> Self {
        let title = {
            let t = item.title.trim();
            truncate(if t.is_empty() { "kube-coder" } else { t }, 120)
        };
        let first = item.body_md.trim().lines().next().unwrap_or("").trim();
        let body = if first.is_empty() {
            if item.waiting { "Action needed" } else { "" }
        } else {
            first
        };
        Alert {
            feed_id: item.id.clone(),
            reference: item.primary_ref().to_string(),
            title,
            body: truncate(body, 180),
        }
    }
}

fn truncate(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}

/// What the ledger remembers per key. `item_ts` is the emit it last judged,
/// so a refresh that sees the same emit again does nothing; `alerted_at` is
/// when it last actually alerted, which the interval runs from.
#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq)]
pub struct Entry {
    pub item_id: String,
    pub item_ts: f64,
    #[serde(default)]
    pub alerted_at: Option<f64>,
}

/// `push_notify._may_repeat`, with `alerted_at` standing in for the ledger's
/// `ts`: a key never alerted goes out; inside the interval nothing does; a
/// different row is new news; the same row only if it was read. A clock that
/// stepped backwards does not hold the key hostage.
pub fn may_repeat(entry: Option<&Entry>, item_id: &str, seen: bool, now: f64, min_interval: f64) -> bool {
    let Some(e) = entry else { return true };
    if let Some(last) = e.alerted_at {
        let age = now - last;
        if (0.0..min_interval).contains(&age) {
            return false;
        }
    }
    if e.item_id != item_id {
        return true;
    }
    seen
}

#[derive(Debug, Clone, Default, Deserialize, Serialize, PartialEq)]
pub struct Ledger {
    #[serde(default)]
    pub keys: HashMap<String, Entry>,
    /// False until the first refresh of this workspace has been recorded.
    #[serde(default)]
    pub primed: bool,
}

impl Ledger {
    /// Judge one refresh of the Feed. Returns the alerts to show and records
    /// every emit it looked at, so the same emit is never judged twice.
    pub fn observe(&mut self, items: &[FeedItem], now: f64, min_interval: f64) -> Vec<Alert> {
        let mut alerts = Vec::new();
        let priming = !self.primed;
        for item in items.iter().filter(|i| should_alert(i)) {
            let key = item.alert_key();
            let entry = self.keys.get(&key);
            if let Some(e) = entry {
                if e.item_id == item.id && e.item_ts >= item.ts {
                    continue; // this emit was judged already
                }
            }
            let fresh = now - item.ts <= ALERT_TTL;
            // `read` is sticky per row id on the server, so for a re-emit it
            // means exactly what push's `seen` means: read before this emit.
            let push = !priming && fresh && may_repeat(entry, &item.id, item.read, now, min_interval);
            let alerted_at = if push { Some(now) } else { entry.and_then(|e| e.alerted_at) };
            if push {
                alerts.push(Alert::from_item(item));
            }
            self.keys.insert(key, Entry { item_id: item.id.clone(), item_ts: item.ts, alerted_at });
        }
        self.primed = true;
        let cutoff = now - RETENTION;
        self.keys.retain(|_, e| e.alerted_at.unwrap_or(e.item_ts) >= cutoff);
        alerts
    }
}

/// `KC_PUSH_MIN_INTERVAL` parsed the way push_notify parses it: blank,
/// unparseable, NaN or negative falls back to the default; 0 turns the timer
/// off (the read-since rule still applies).
pub fn min_interval_from(raw: Option<&str>) -> f64 {
    match raw.map(str::trim).filter(|s| !s.is_empty()).map(str::parse::<f64>) {
        Some(Ok(v)) if v.is_finite() && v >= 0.0 => v,
        _ => DEFAULT_MIN_INTERVAL,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: f64 = 1_800_000_000.0;
    const IV: f64 = DEFAULT_MIN_INTERVAL;

    fn waiting(id: &str, r: &str, ts: f64) -> FeedItem {
        FeedItem {
            id: id.into(),
            ts,
            kind: "board".into(),
            title: format!("Review {id}"),
            body_md: "Approve the PR comment\nsecond line".into(),
            links: vec![FeedLink { reference: r.into(), label: "Open".into() }],
            waiting: true,
            ..Default::default()
        }
    }

    fn primed() -> Ledger {
        Ledger { primed: true, ..Default::default() }
    }

    #[test]
    fn first_look_records_without_alerting() {
        let mut l = Ledger::default();
        let items = vec![waiting("f1", "board:b:1", NOW - 60.0)];
        assert!(l.observe(&items, NOW, IV).is_empty());
        assert!(l.primed);
        // The same row seen again is not news either.
        assert!(l.observe(&items, NOW + 5.0, IV).is_empty());
        // A new row after priming is.
        let mut more = items.clone();
        more.push(waiting("f2", "board:b:2", NOW + 10.0));
        let a = l.observe(&more, NOW + 11.0, IV);
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].feed_id, "f2");
    }

    #[test]
    fn only_waiting_rows_and_decisions_alert() {
        let mut l = primed();
        let mut info = waiting("f1", "task:t1", NOW);
        info.waiting = false;
        info.kind = "task".into();
        let mut decision = info.clone();
        decision.id = "f2".into();
        decision.kind = "decision".into();
        decision.links[0].reference = "memory:project/x".into();
        let a = l.observe(&[info, decision], NOW, IV);
        assert_eq!(a.iter().map(|a| a.feed_id.as_str()).collect::<Vec<_>>(), vec!["f2"]);
    }

    #[test]
    fn a_coalesced_repeat_waits_for_a_read_and_the_interval() {
        let mut l = primed();
        assert_eq!(l.observe(&[waiting("f1", "task:t1", NOW)], NOW, IV).len(), 1);
        // Re-emitted (fresh ts, same row) inside the interval: nothing.
        assert!(l.observe(&[waiting("f1", "task:t1", NOW + 60.0)], NOW + 60.0, IV).is_empty());
        // Re-emitted after the interval but never read: still nothing.
        let t = NOW + IV + 10.0;
        assert!(l.observe(&[waiting("f1", "task:t1", t)], t, IV).is_empty());
        // Read, then re-emitted after the interval: alerts again.
        let mut read = waiting("f1", "task:t1", t + 20.0);
        read.read = true;
        assert_eq!(l.observe(&[read.clone()], t + 20.0, IV).len(), 1);
        // ...and that alert restarts the interval.
        read.ts = t + 30.0;
        assert!(l.observe(&[read], t + 30.0, IV).is_empty());
    }

    #[test]
    fn a_new_row_for_the_same_target_is_news_after_the_interval() {
        let mut l = primed();
        assert_eq!(l.observe(&[waiting("f1", "board:b:1", NOW)], NOW, IV).len(), 1);
        // The old row was dismissed; the next emit lands on a new id.
        assert!(l.observe(&[waiting("f9", "board:b:1", NOW + 5.0)], NOW + 5.0, IV).is_empty());
        let t = NOW + IV + 1.0;
        let a = l.observe(&[waiting("f10", "board:b:1", t)], t, IV);
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].reference, "board:b:1");
    }

    #[test]
    fn interval_zero_leaves_only_the_read_rule() {
        let mut l = primed();
        assert_eq!(l.observe(&[waiting("f1", "task:t1", NOW)], NOW, 0.0).len(), 1);
        let mut again = waiting("f1", "task:t1", NOW + 1.0);
        assert!(l.observe(&[again.clone()], NOW + 1.0, 0.0).is_empty());
        again.ts = NOW + 2.0;
        again.read = true;
        assert_eq!(l.observe(&[again], NOW + 2.0, 0.0).len(), 1);
    }

    #[test]
    fn stale_rows_are_recorded_not_alerted() {
        let mut l = primed();
        let old = waiting("f1", "task:t1", NOW - ALERT_TTL - 1.0);
        assert!(l.observe(&[old.clone()], NOW, IV).is_empty());
        assert!(l.keys.contains_key(&old.alert_key()));
    }

    #[test]
    fn a_clock_step_backwards_does_not_hold_the_key() {
        let e = Entry { item_id: "f1".into(), item_ts: NOW, alerted_at: Some(NOW + 500.0) };
        assert!(may_repeat(Some(&e), "f2", false, NOW, IV));
    }

    #[test]
    fn alert_text_matches_push() {
        let mut item = waiting("f1", "board:b:1", NOW);
        item.title = "  ".into();
        item.body_md = "".into();
        let a = Alert::from_item(&item);
        assert_eq!((a.title.as_str(), a.body.as_str()), ("kube-coder", "Action needed"));
        item.title = "x".repeat(300);
        item.body_md = "\n  first line  \nsecond".into();
        let a = Alert::from_item(&item);
        assert_eq!(a.title.chars().count(), 120);
        assert_eq!(a.body, "first line");
    }

    #[test]
    fn keys_prefer_dedupe_key_then_ref_then_id() {
        let mut i = waiting("f1", "task:t1", NOW);
        assert_eq!(i.alert_key(), "waiting:task:t1");
        i.dedupe_key = "task:t1:waiting".into();
        assert_eq!(i.alert_key(), "task:t1:waiting");
        i.dedupe_key.clear();
        i.links.clear();
        assert_eq!(i.alert_key(), "id:f1");
    }

    #[test]
    fn badge_counts_unread_waiting_rows() {
        let mut read = waiting("f2", "task:t2", NOW);
        read.read = true;
        let mut done = waiting("f3", "task:t3", NOW);
        done.waiting = false;
        let items = vec![waiting("f1", "board:b:1", NOW), read, done];
        assert_eq!(needs_you_count(&items), 1);
    }

    #[test]
    fn old_entries_are_forgotten() {
        let mut l = primed();
        l.keys.insert("old".into(), Entry { item_id: "x".into(), item_ts: NOW - RETENTION - 10.0, alerted_at: None });
        l.observe(&[], NOW, IV);
        assert!(l.keys.is_empty());
    }

    #[test]
    fn min_interval_parses_like_push_notify() {
        assert_eq!(min_interval_from(None), DEFAULT_MIN_INTERVAL);
        assert_eq!(min_interval_from(Some("  ")), DEFAULT_MIN_INTERVAL);
        assert_eq!(min_interval_from(Some("abc")), DEFAULT_MIN_INTERVAL);
        assert_eq!(min_interval_from(Some("-5")), DEFAULT_MIN_INTERVAL);
        assert_eq!(min_interval_from(Some("NaN")), DEFAULT_MIN_INTERVAL);
        assert_eq!(min_interval_from(Some("0")), 0.0);
        assert_eq!(min_interval_from(Some("60")), 60.0);
    }

    #[test]
    fn parses_the_api_feed_shape() {
        let raw = r#"{"items":[{"id":"f1","ts":1.5,"kind":"board","title":"t","body_md":"",
            "source":"board:x","project_id":"","links":[{"label":"Open","ref":"board:b:1"}],
            "waiting":true,"read":false}]}"#;
        #[derive(Deserialize)]
        struct Resp { items: Vec<FeedItem> }
        let r: Resp = serde_json::from_str(raw).unwrap();
        assert_eq!(r.items[0].primary_ref(), "board:b:1");
    }
}
