//! Where the Rust core may send a request, and how it builds the URL.
//!
//! The desktop app talks to exactly one host: the workspace the user connected.
//! Every request the core makes, for the UI or for itself, is built here from
//! that host and a path, so there is no code path that takes a full URL from
//! the webview. The policy is the one `mobile/src/util/urlPolicy.ts` applies
//! before the phone attaches its token (`validateHost(…, 'workspace')`):
//!
//! - HTTPS is always fine.
//! - Plain HTTP only for loopback (localhost, 127.0.0.0/8, ::1), and only in a
//!   build that allows cleartext (`KC_ALLOW_CLEARTEXT=1` at compile time, the
//!   twin of the mobile `EXPO_PUBLIC_ALLOW_CLEARTEXT`).
//! - Nothing else: no other scheme, no userinfo smuggled into the authority.

use url::Url;

/// Whether this build lets the token travel over plain HTTP to loopback.
/// Off for release builds unless the builder opts in explicitly.
pub fn cleartext_allowed() -> bool {
    matches!(
        option_env!("KC_ALLOW_CLEARTEXT").map(str::trim),
        Some("1") | Some("true") | Some("yes")
    )
}

/// True for hosts whose traffic never leaves the machine. A LAN address is
/// deliberately not loopback: it can be intercepted.
pub fn is_loopback_host(host: &url::Host<&str>) -> bool {
    match host {
        url::Host::Domain(d) => d.eq_ignore_ascii_case("localhost"),
        url::Host::Ipv4(ip) => ip.is_loopback(),
        url::Host::Ipv6(ip) => ip.is_loopback(),
    }
}

/// Validate a user-supplied workspace host. Returns the normalised base URL
/// (no trailing slash, no query or fragment) or the reason it was refused.
pub fn validate_host(raw: &str, allow_cleartext: bool) -> Result<Url, String> {
    let trimmed = raw.trim().trim_end_matches('/');
    if trimmed.is_empty() {
        return Err("Enter a host URL.".into());
    }
    let url = Url::parse(trimmed).map_err(|_| {
        "Enter a full URL starting with https:// (for example https://you.kube-coder.example.com)."
            .to_string()
    })?;
    let host = url
        .host()
        .ok_or_else(|| "Enter a full URL with a host name.".to_string())?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err("Remove the user name or password from the URL.".into());
    }
    match url.scheme() {
        "https" => {}
        "http" => {
            if !is_loopback_host(&host) {
                return Err(format!(
                    "Plain HTTP is only allowed for localhost. Use https:// for \"{host}\" so your \
                     API token isn't transmitted in the clear where it could be captured."
                ));
            }
            if !allow_cleartext {
                return Err(
                    "This build blocks cleartext HTTP. Use an https:// host, or a development \
                     build to reach a local http://localhost workspace."
                        .into(),
                );
            }
        }
        other => return Err(format!("Unsupported URL scheme \"{other}://\". Use https://.")),
    }
    let mut base = url.clone();
    base.set_query(None);
    base.set_fragment(None);
    let path = base.path().trim_end_matches('/').to_string();
    base.set_path(&path);
    Ok(base)
}

/// The scheme, host and port that make two URLs the same origin.
pub fn same_origin(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme()
        && a.host_str().map(str::to_ascii_lowercase) == b.host_str().map(str::to_ascii_lowercase)
        && a.port_or_known_default() == b.port_or_known_default()
}

/// Build `base + target`, refusing anything that could leave the host.
///
/// `target` is an absolute path on the workspace with an optional query
/// (`/api/feed?limit=200`). The path starts with a single `/` and carries no
/// scheme, authority, backslash, fragment, space, control byte or dot
/// segment; the query carries no fragment, space or control byte. The origin
/// comes from the connected host alone, so nothing the webview sends can
/// point a request — and the token on it — anywhere else.
pub fn workspace_url(base: &Url, target: &str) -> Result<Url, String> {
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p, Some(q)),
        None => (target, None),
    };
    if !path.starts_with('/') || path.starts_with("//") {
        return Err("path must start with a single /".into());
    }
    if path.contains('\\') || target.contains('#') {
        return Err("path may not contain \\ or #".into());
    }
    if target.chars().any(|c| c.is_control() || c == ' ') {
        return Err("path may not contain spaces or control characters".into());
    }
    if path.split('/').any(|seg| seg == ".." || seg == "." || seg.eq_ignore_ascii_case("%2e%2e")) {
        return Err("path may not contain dot segments".into());
    }
    let joined = format!("{}{}", base.as_str().trim_end_matches('/'), path);
    let mut url = Url::parse(&joined).map_err(|e| format!("bad path: {e}"))?;
    if !same_origin(base, &url) || !url.path().starts_with(base.path()) {
        return Err("path resolves outside the workspace".into());
    }
    url.set_query(query.filter(|q| !q.is_empty()));
    Ok(url)
}

/// The paths an embedded-app window may be bootstrapped into: the same
/// allowlist server.py applies to `/api/claude/apps/session?next=`
/// (`_APP_SESSION_NEXT_RE`), so the core never asks for a session the server
/// would refuse.
pub fn is_embed_path(next: &str) -> bool {
    let rest = match next.strip_prefix("/api/") {
        Some(r) => r,
        None => return false,
    };
    if next.contains("..") || next.contains('\\') || next.contains("//") {
        return false;
    }
    if let Some(after) = rest.strip_prefix("app-proxy/") {
        let digits: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
        let tail = &after[digits.len()..];
        return !digits.is_empty() && digits.len() <= 5 && (tail.is_empty() || tail.starts_with('/'));
    }
    if let Some(tail) = rest.strip_prefix("terminal-proxy") {
        return tail.is_empty() || tail.starts_with('/');
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(raw: &str) -> String {
        validate_host(raw, false).expect(raw).to_string()
    }

    #[test]
    fn https_hosts_are_accepted_and_normalised() {
        assert_eq!(ok("https://me.example.com/"), "https://me.example.com/");
        assert_eq!(ok("  https://me.example.com///  "), "https://me.example.com/");
        assert_eq!(ok("https://me.example.com:8443"), "https://me.example.com:8443/");
        assert_eq!(ok("https://me.example.com/?q=1#frag"), "https://me.example.com/");
    }

    #[test]
    fn cleartext_is_loopback_only_and_only_when_allowed() {
        assert!(validate_host("http://localhost:6080", true).is_ok());
        assert!(validate_host("http://127.0.0.5:6080", true).is_ok());
        assert!(validate_host("http://[::1]:6080", true).is_ok());
        assert!(validate_host("http://localhost:6080", false).is_err());
        assert!(validate_host("http://192.168.1.4", true).is_err());
        assert!(validate_host("http://me.example.com", true).is_err());
        assert!(validate_host("http://localhost.evil.com", true).is_err());
    }

    #[test]
    fn other_schemes_and_junk_are_refused() {
        for raw in ["", "   ", "me.example.com", "ftp://me.example.com", "file:///etc/passwd",
                    "javascript:alert(1)", "https://user:pw@me.example.com"] {
            assert!(validate_host(raw, true).is_err(), "{raw} should be refused");
        }
    }

    #[test]
    fn workspace_url_stays_on_the_host() {
        let base = validate_host("https://me.example.com", false).unwrap();
        let u = workspace_url(&base, "/api/feed?limit=200").unwrap();
        assert_eq!(u.as_str(), "https://me.example.com/api/feed?limit=200");
        let u = workspace_url(&base, "/api/feed/a%2Fb/read").unwrap();
        assert_eq!(u.host_str(), Some("me.example.com"));
        // A URL in the query is only a value: the origin is still the host's.
        let u = workspace_url(&base, "/api/x?next=https%3A%2F%2Fevil.com%2F&y=@evil.com").unwrap();
        assert_eq!(u.host_str(), Some("me.example.com"));
        assert_eq!(u.query_pairs().count(), 2);
    }

    #[test]
    fn workspace_url_refuses_escapes() {
        let base = validate_host("https://me.example.com", false).unwrap();
        for p in ["api/feed", "//evil.com/x", "/\\evil.com", "https://evil.com", "/api/../../x",
                  "/api/./x", "/api/%2e%2e/x", "/api/feed#x", "/api/feed?x=1#y", "/api/a b",
                  "/api/\nx", "/api/x?a=\r\n", "@evil.com", "?x=1"] {
            assert!(workspace_url(&base, p).is_err(), "{p:?} should be refused");
        }
    }

    #[test]
    fn workspace_url_keeps_a_path_prefix() {
        let base = validate_host("https://proxy.example.com/ws/me", false).unwrap();
        let u = workspace_url(&base, "/api/feed").unwrap();
        assert_eq!(u.as_str(), "https://proxy.example.com/ws/me/api/feed");
    }

    #[test]
    fn same_origin_compares_scheme_host_and_port() {
        let a = Url::parse("https://Me.example.com/x").unwrap();
        assert!(same_origin(&a, &Url::parse("https://me.example.com:443/y").unwrap()));
        assert!(!same_origin(&a, &Url::parse("http://me.example.com/").unwrap()));
        assert!(!same_origin(&a, &Url::parse("https://me.example.com:8443/").unwrap()));
        assert!(!same_origin(&a, &Url::parse("https://evil.com/").unwrap()));
    }

    #[test]
    fn embed_paths_match_the_server_allowlist() {
        for p in ["/api/app-proxy/3000/", "/api/app-proxy/3000", "/api/app-proxy/8080/x?y=1",
                  "/api/terminal-proxy/", "/api/terminal-proxy/?t=1"] {
            assert!(is_embed_path(p), "{p} should be allowed");
        }
        for p in ["/api/app-proxy/", "/api/app-proxy/abc/", "/api/app-proxy/123456/",
                  "/api/feed", "https://evil.com/api/app-proxy/1/", "/api/app-proxy/1/../../x",
                  "/api/terminal-proxyx", "//api/app-proxy/1/"] {
            assert!(!is_embed_path(p), "{p} should be refused");
        }
    }
}
