//! Workspace apps (the app proxy) and the terminal, in their own window.
//!
//! On the phone these load in a WebView that sends the Bearer header once, to
//! `/api/claude/apps/session?next=…`, and lives on the short-lived
//! `kc_app_session` cookie that request mints (handlers/apps.py). A desktop
//! webview cannot add a header to a navigation, so the core makes that one
//! request itself, without following the redirect, and hands the cookie to a
//! NEW window before pointing it at the app.
//!
//! That window is remote content, so it gets nothing else: it is not the
//! `main` window, no capability names it, and every command checks the
//! caller's label (lib.rs), so it has no IPC at all. Navigation is pinned to
//! the workspace origin and pop-ups are refused.

use crate::core::Core;
use crate::scope;
use std::sync::atomic::{AtomicU32, Ordering};
use tauri::webview::cookie::{Cookie, SameSite};
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, WebviewUrl, WebviewWindowBuilder};

static NEXT: AtomicU32 = AtomicU32::new(1);
const COOKIE: &str = "kc_app_session";

/// `name=value` out of a Set-Cookie header, if it is the session cookie and
/// its value is a plain token.
pub fn session_cookie(set_cookie: &str) -> Option<String> {
    let pair = set_cookie.split(';').next()?.trim();
    let (name, value) = pair.split_once('=')?;
    if name.trim() != COOKIE {
        return None;
    }
    let value = value.trim();
    if value.is_empty() || value.len() > 512 || !value.chars().all(|c| c.is_ascii_graphic() && c != ';' && c != ',') {
        return None;
    }
    Some(value.to_string())
}

pub async fn open(app: &AppHandle, core: &Core, next: &str, title: &str) -> Result<(), String> {
    if !scope::is_embed_path(next) {
        return Err("not an app or terminal path".into());
    }
    let conn = core.conn().ok_or("Not configured")?;
    let mut mint = scope::workspace_url(&conn.base, "/api/claude/apps/session")?;
    mint.query_pairs_mut().append_pair("next", next);
    let resp = core
        .http
        .get(mint)
        .bearer_auth(conn.token.expose())
        .send()
        .await
        .map_err(|e| crate::http::network_error(&e))?;
    if resp.status().as_u16() != 302 {
        return Err(format!("the workspace refused the app session ({})", resp.status().as_u16()));
    }
    let value = resp
        .headers()
        .get_all(reqwest::header::SET_COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .find_map(session_cookie)
        .ok_or("the workspace sent no app session")?;
    let target = scope::workspace_url(&conn.base, next)?;
    let host = conn.base.host_str().ok_or("no host")?.to_string();
    let secure = conn.base.scheme() == "https";
    let origin = conn.base.clone();

    let label = format!("embed-{}", NEXT.fetch_add(1, Ordering::Relaxed));
    let pinned = origin.clone();
    let window = WebviewWindowBuilder::new(app, &label, WebviewUrl::External("about:blank".parse().unwrap()))
        .title(if title.trim().is_empty() { "kube-coder app" } else { title.trim() })
        .inner_size(1100.0, 760.0)
        .on_navigation(move |url| url.as_str() == "about:blank" || scope::same_origin(&pinned, url))
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .build()
        .map_err(|e| e.to_string())?;
    let cookie = Cookie::build((COOKIE, value))
        .domain(host)
        .path("/")
        .http_only(true)
        .secure(secure)
        .same_site(SameSite::Lax)
        .build();
    window.set_cookie(cookie).map_err(|e| e.to_string())?;
    window.navigate(target).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn picks_the_session_cookie_only() {
        assert_eq!(
            session_cookie("kc_app_session=abc.def-123; Path=/; HttpOnly; SameSite=Lax; Max-Age=600"),
            Some("abc.def-123".into())
        );
        assert_eq!(session_cookie("other=1; Path=/"), None);
        assert_eq!(session_cookie("kc_app_session=; Path=/"), None);
        assert_eq!(session_cookie("kc_app_session=a b"), None);
        assert_eq!(session_cookie("kc_app_session_x=abc"), None);
    }
}
