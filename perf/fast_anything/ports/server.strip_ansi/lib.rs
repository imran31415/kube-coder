//! Rust port of server.strip_ansi (server.py:1043): `_ANSI_RE.sub('', text)` for
//!
//!     \x1b\[[0-9;?]*[ -/]*[@-~]   CSI
//!   | \x1b\][^\x07]*\x07          OSC ... BEL
//!   | \x1b[NOPYZ\\^_=>78<]        single-char escapes
//!
//! Hand-written scanner, exact to Python's leftmost-first alternation:
//! * the CSI classes [0-9;?], [ -/] and [@-~] are pairwise disjoint, so greedy
//!   matching never needs to backtrack — one forward pass decides it;
//! * OSC ends at the FIRST BEL (its body cannot contain one) and fails when no
//!   BEL follows; a cached "no BEL after here" keeps repeated unterminated
//!   OSCs linear instead of re-scanning to the end each time;
//! * an ESC that matches no alternative is kept, and scanning resumes at the
//!   next character.
//! Every pattern byte is ASCII, so cuts fall on ASCII bytes and the output is
//! valid UTF-8. Non-str input (and str with lone surrogates) is handed to
//! Python's own regex so errors are identical.

use pyo3::prelude::*;
use pyo3::types::PyString;

const ESC: u8 = 0x1b;
const BEL: u8 = 0x07;

#[inline]
fn is_param(b: u8) -> bool {
    b.is_ascii_digit() || b == b';' || b == b'?'
}
#[inline]
fn is_inter(b: u8) -> bool {
    (0x20..=0x2f).contains(&b)
}
#[inline]
fn is_final(b: u8) -> bool {
    (0x40..=0x7e).contains(&b)
}
#[inline]
fn is_single(b: u8) -> bool {
    matches!(b, b'N' | b'O' | b'P' | b'Y' | b'Z' | b'\\' | b'^' | b'_' | b'=' | b'>' | b'7' | b'8' | b'<')
}

/// Length of the escape sequence starting at `s[p] == ESC`, or None.
#[inline]
fn match_at(s: &[u8], p: usize, no_bel_from: &mut usize) -> Option<usize> {
    let next = *s.get(p + 1)?;
    match next {
        b'[' => {
            let mut q = p + 2;
            while q < s.len() && is_param(s[q]) {
                q += 1;
            }
            while q < s.len() && is_inter(s[q]) {
                q += 1;
            }
            if q < s.len() && is_final(s[q]) {
                Some(q + 1 - p)
            } else {
                None // '[' is not a single-char escape, OSC needs ']'
            }
        }
        b']' => {
            let start = p + 2;
            if start >= *no_bel_from {
                return None;
            }
            match s[start..].iter().position(|&b| b == BEL) {
                Some(off) => Some(start + off + 1 - p),
                None => {
                    *no_bel_from = start.min(*no_bel_from);
                    None
                }
            }
        }
        b if is_single(b) => Some(2),
        _ => None,
    }
}

fn strip(s: &str) -> Option<String> {
    let b = s.as_bytes();
    let mut p = match b.iter().position(|&c| c == ESC) {
        Some(p) => p,
        None => return None, // nothing to strip
    };
    let mut out: Vec<u8> = Vec::with_capacity(b.len());
    out.extend_from_slice(&b[..p]);
    let mut no_bel_from = usize::MAX;
    while p < b.len() {
        if b[p] == ESC {
            if let Some(n) = match_at(b, p, &mut no_bel_from) {
                p += n;
                continue;
            }
            out.push(ESC);
            p += 1;
            continue;
        }
        // Copy the run up to the next ESC in one go.
        let run = b[p..].iter().position(|&c| c == ESC).unwrap_or(b.len() - p);
        out.extend_from_slice(&b[p..p + run]);
        p += run;
    }
    // Cuts only ever remove whole ASCII sequences, so this stays valid UTF-8.
    Some(unsafe { String::from_utf8_unchecked(out) })
}

fn python_fallback(py: Python<'_>, text: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let re = py.import("re")?;
    let pat = re.call_method1(
        "compile",
        (concat!(
            "\x1b\\[[0-9;?]*[ -/]*[@-~]",
            "|\x1b\\][^\x07]*\x07",
            "|\x1b[NOPYZ\\\\^_=>78<]"
        ),),
    )?;
    Ok(pat.call_method1("sub", ("", text))?.unbind())
}

#[pyfunction]
fn strip_ansi(py: Python<'_>, text: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let s = match text.cast::<PyString>() {
        Ok(s) => s,
        Err(_) => return python_fallback(py, text),
    };
    let st = match s.to_str() {
        Ok(st) => st,
        Err(_) => return python_fallback(py, text),
    };
    match strip(st) {
        Some(out) => Ok(PyString::new(py, &out).into_any().unbind()),
        // re.sub with no match returns an equal str; for a str subclass it is
        // a plain str copy.
        None => {
            if text.is_exact_instance_of::<PyString>() {
                Ok(text.clone().unbind())
            } else {
                Ok(PyString::new(py, st).into_any().unbind())
            }
        }
    }
}

#[pymodule]
fn fa_server_strip_ansi(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(strip_ansi, m)?)?;
    Ok(())
}
