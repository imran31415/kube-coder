//! Every workspace request the app makes, from the UI or from the core.
//!
//! The webview has no network access of its own (its CSP allows only IPC), so
//! the mobile client's `fetch` calls arrive here as `ws_request` commands with
//! a path. This module is the one place the Bearer token is attached, and it
//! attaches it only to a URL built by `scope::workspace_url` from the
//! connected host, with redirects switched off — the same "refuse ANY
//! redirect on a credentialed request" rule `mobile/src/api/client.ts` uses.

use crate::scope;
use crate::secrets::Token;
use base64::Engine;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::Duration;
use url::Url;

/// The connected workspace: its validated base URL and its token.
#[derive(Clone, Debug, PartialEq)]
pub struct Conn {
    pub base: Url,
    pub token: Token,
}

pub const DEFAULT_TIMEOUT_MS: u64 = 15_000;
const MAX_TIMEOUT_MS: u64 = 120_000;
const MAX_RESPONSE_BYTES: usize = 32 * 1024 * 1024;
const MAX_REQUEST_BYTES: usize = 32 * 1024 * 1024;

/// Headers the webview may set. Everything that identifies or authenticates
/// (Authorization, Cookie, Host, Origin, …) is the core's alone.
const ALLOWED_HEADERS: &[&str] = &["accept", "content-type", "x-dest-path", "x-filename", "x-keeper-wake"];

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .https_only(!scope::cleartext_allowed())
        .connect_timeout(Duration::from_secs(10))
        .user_agent(concat!("kube-coder-desktop/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("reqwest client")
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WsRequest {
    #[serde(default = "get")]
    pub method: String,
    /// `/api/...` with an optional `?query`; never a full URL.
    pub path: String,
    #[serde(default)]
    pub headers: HashMap<String, String>,
    /// A text (usually JSON) body.
    #[serde(default)]
    pub body: Option<String>,
    /// A binary body (an image upload, a voice note), base64-encoded.
    #[serde(default)]
    pub body_base64: Option<String>,
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

fn get() -> String {
    "GET".into()
}

#[derive(Debug, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct WsResponse {
    pub status: u16,
    pub status_text: String,
    pub content_type: String,
    pub body: String,
}

pub fn method(m: &str) -> Result<reqwest::Method, String> {
    match m.to_ascii_uppercase().as_str() {
        "GET" => Ok(reqwest::Method::GET),
        "POST" => Ok(reqwest::Method::POST),
        "PUT" => Ok(reqwest::Method::PUT),
        "PATCH" => Ok(reqwest::Method::PATCH),
        "DELETE" => Ok(reqwest::Method::DELETE),
        "HEAD" => Ok(reqwest::Method::HEAD),
        other => Err(format!("method {other} is not allowed")),
    }
}

/// The webview's headers, minus anything it may not set. Values must be
/// plain visible ASCII, so no header can be split or smuggled.
pub fn filter_headers(h: &HashMap<String, String>) -> Vec<(String, String)> {
    h.iter()
        .filter(|(k, _)| ALLOWED_HEADERS.contains(&k.to_ascii_lowercase().as_str()))
        .filter(|(_, v)| v.len() <= 4096 && v.chars().all(|c| (' '..='~').contains(&c)))
        .map(|(k, v)| (k.to_ascii_lowercase(), v.clone()))
        .collect()
}

/// Send one request on behalf of the webview.
pub async fn send(client: &reqwest::Client, conn: &Conn, req: WsRequest) -> Result<WsResponse, String> {
    let url = scope::workspace_url(&conn.base, &req.path)?;
    let mut builder = client
        .request(method(&req.method)?, url)
        .bearer_auth(conn.token.expose())
        .timeout(Duration::from_millis(req.timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS).min(MAX_TIMEOUT_MS)));
    for (k, v) in filter_headers(&req.headers) {
        builder = builder.header(k, v);
    }
    if let Some(b64) = req.body_base64 {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64.as_bytes())
            .map_err(|_| "body is not valid base64".to_string())?;
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err("request body too large".into());
        }
        builder = builder.body(bytes);
    } else if let Some(text) = req.body {
        if text.len() > MAX_REQUEST_BYTES {
            return Err("request body too large".into());
        }
        builder = builder.body(text);
    }
    let resp = builder.send().await.map_err(|e| network_error(&e))?;
    let status = resp.status();
    let content_type = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    if resp.content_length().is_some_and(|n| n as usize > MAX_RESPONSE_BYTES) {
        return Err("response too large".into());
    }
    let bytes = resp.bytes().await.map_err(|e| network_error(&e))?;
    if bytes.len() > MAX_RESPONSE_BYTES {
        return Err("response too large".into());
    }
    Ok(WsResponse {
        status: status.as_u16(),
        status_text: status.canonical_reason().unwrap_or("").to_string(),
        content_type,
        body: String::from_utf8_lossy(&bytes).into_owned(),
    })
}

/// A transport error, worded without the URL (whose query could carry an id
/// the user would rather not see in a toast) and never with the token.
pub fn network_error(e: &reqwest::Error) -> String {
    if e.is_timeout() {
        "Request timed out".into()
    } else if e.is_connect() {
        "Network error: could not connect to the workspace".into()
    } else {
        "Network error: the connection to the workspace failed".into()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_benign_headers_pass() {
        let mut h = HashMap::new();
        h.insert("Accept".into(), "application/json".into());
        h.insert("Content-Type".into(), "application/json".into());
        h.insert("X-Filename".into(), "a%20b.png".into());
        h.insert("Authorization".into(), "Bearer stolen".into());
        h.insert("Cookie".into(), "kc_app_session=x".into());
        h.insert("Host".into(), "evil.com".into());
        h.insert("Origin".into(), "https://evil.com".into());
        h.insert("X-Dest-Path".into(), "a\r\nInjected: 1".into());
        let mut got: Vec<String> = filter_headers(&h).into_iter().map(|(k, _)| k).collect();
        got.sort();
        assert_eq!(got, vec!["accept", "content-type", "x-filename"]);
    }

    #[test]
    fn methods_are_an_allowlist() {
        assert!(method("get").is_ok());
        assert!(method("DELETE").is_ok());
        assert!(method("CONNECT").is_err());
        assert!(method("TRACE").is_err());
    }

    #[test]
    fn requests_deserialize_from_the_webview_shape() {
        let r: WsRequest = serde_json::from_str(
            r#"{"path":"/api/feed?limit=5","headers":{"Accept":"application/json"},"timeoutMs":5000}"#,
        )
        .unwrap();
        assert_eq!(r.method, "GET");
        assert_eq!(r.timeout_ms, Some(5000));
    }
}
