//! Rust port of instruction_scan.scan_text (instruction_scan.py:194).
//!
//! Same semantics as the Python:
//! * offsets, lines, columns and the context window are in code points;
//! * ZWJ between pictographs (past skin-tone / variation selectors) is legal,
//!   a BOM at offset 0 is legal, everything in ZERO_WIDTH / BIDI_CONTROL / the
//!   tag block is reported;
//! * the excerpt drops invisibles, maps '\n' to ' ', then applies Python's
//!   `str.strip()` whitespace set exactly (it includes U+001C..U+001F, which
//!   Rust's `char::is_whitespace` does not).
//! `_describe`'s `unicodedata.name` fallback is unreachable: only codepoints
//! with a dedicated name are ever given a severity.
//!
//! Fast path: every suspicious codepoint is >= U+180E, so a text with no such
//! code point (all of ASCII, most prose) returns [] after one byte scan.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

const TAG_START: u32 = 0xE0000;
const TAG_END: u32 = 0xE007F;

#[inline]
fn bidi_name(cp: u32) -> Option<&'static str> {
    Some(match cp {
        0x202A => "LEFT-TO-RIGHT EMBEDDING",
        0x202B => "RIGHT-TO-LEFT EMBEDDING",
        0x202C => "POP DIRECTIONAL FORMATTING",
        0x202D => "LEFT-TO-RIGHT OVERRIDE",
        0x202E => "RIGHT-TO-LEFT OVERRIDE",
        0x2066 => "LEFT-TO-RIGHT ISOLATE",
        0x2067 => "RIGHT-TO-LEFT ISOLATE",
        0x2068 => "FIRST STRONG ISOLATE",
        0x2069 => "POP DIRECTIONAL ISOLATE",
        _ => return None,
    })
}

#[inline]
fn zero_width_name(cp: u32) -> Option<&'static str> {
    Some(match cp {
        0x200B => "ZERO WIDTH SPACE",
        0x200C => "ZERO WIDTH NON-JOINER",
        0x200D => "ZERO WIDTH JOINER",
        0x2060 => "WORD JOINER",
        0xFEFF => "ZERO WIDTH NO-BREAK SPACE (BOM)",
        0x180E => "MONGOLIAN VOWEL SEPARATOR",
        _ => return None,
    })
}

#[inline]
fn is_tag(cp: u32) -> bool {
    (TAG_START..=TAG_END).contains(&cp)
}

/// Dropped from the excerpt: ZERO_WIDTH, BIDI_CONTROL, tag block.
#[inline]
fn is_invisible(cp: u32) -> bool {
    is_tag(cp) || zero_width_name(cp).is_some() || bidi_name(cp).is_some()
}

#[inline]
fn is_emoji_modifier(cp: u32) -> bool {
    (0x1F3FB..=0x1F3FF).contains(&cp) || cp == 0xFE0F || cp == 0xFE0E
}

const PICTOGRAPHIC: &[(u32, u32)] = &[
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x21AA), (0x231A, 0x231B),
    (0x2328, 0x2328), (0x23CF, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25FE),
    (0x2600, 0x27BF), (0x2934, 0x2935), (0x2B00, 0x2BFF), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3299),
    (0x1F000, 0x1FAFF), (0x1FC00, 0x1FFFD),
];

#[inline]
fn is_pictographic(cp: u32) -> bool {
    PICTOGRAPHIC.iter().any(|&(lo, hi)| lo <= cp && cp <= hi)
}

/// Python `str.isspace()` — the exact set `str.strip()` removes.
#[inline]
fn py_isspace(c: char) -> bool {
    matches!(c as u32,
        0x09..=0x0D | 0x1C..=0x20 | 0x85 | 0xA0 | 0x1680 | 0x2000..=0x200A
        | 0x2028 | 0x2029 | 0x202F | 0x205F | 0x3000)
}

fn zwj_is_emoji_join(cps: &[u32], i: usize) -> bool {
    let mut j = i as isize - 1;
    while j >= 0 && is_emoji_modifier(cps[j as usize]) {
        j -= 1;
    }
    let mut k = i + 1;
    while k < cps.len() && is_emoji_modifier(cps[k]) {
        k += 1;
    }
    if j < 0 || k >= cps.len() {
        return false;
    }
    is_pictographic(cps[j as usize]) && is_pictographic(cps[k])
}

fn severity(cp: u32, cps: &[u32], i: usize) -> Option<&'static str> {
    if is_tag(cp) {
        return Some("high");
    }
    if bidi_name(cp).is_some() {
        return Some("high");
    }
    match cp {
        0x200D => {
            if zwj_is_emoji_join(cps, i) { None } else { Some("medium") }
        }
        0x200C => Some("medium"),
        0xFEFF => {
            if i == 0 { None } else { Some("medium") }
        }
        _ if zero_width_name(cp).is_some() => Some("medium"),
        _ => None,
    }
}

fn describe(cp: u32) -> &'static str {
    if is_tag(cp) {
        return "UNICODE TAG (invisible ASCII carrier)";
    }
    if let Some(n) = bidi_name(cp) {
        return n;
    }
    if let Some(n) = zero_width_name(cp) {
        return n;
    }
    unreachable!("only named codepoints receive a severity")
}

#[inline]
fn could_be_suspicious(cp: u32) -> bool {
    is_tag(cp) || bidi_name(cp).is_some() || zero_width_name(cp).is_some()
}

#[pyfunction]
#[pyo3(signature = (text, context_chars = 40))]
fn scan_text(py: Python<'_>, text: &str, context_chars: i64) -> PyResult<Py<PyAny>> {
    let findings = PyList::empty(py);

    // Fast path: no candidate code point anywhere → no findings.
    let any = !text.is_ascii() && text.chars().any(|c| could_be_suspicious(c as u32));
    if !any {
        return Ok(findings.into_any().unbind());
    }

    let cps: Vec<u32> = text.chars().map(|c| c as u32).collect();
    let n = cps.len() as i64;
    let (mut line, mut col) = (1i64, 1i64);
    let mut excerpt = String::new();
    for (i, &cp) in cps.iter().enumerate() {
        if could_be_suspicious(cp) {
            if let Some(sev) = severity(cp, &cps, i) {
                let ii = i as i64;
                let lo = ii.saturating_sub(context_chars).max(0);
                let hi = ii.saturating_add(context_chars).min(n);
                excerpt.clear();
                if lo < hi {
                    for &c in &cps[lo as usize..hi as usize] {
                        if is_invisible(c) {
                            continue;
                        }
                        let ch = if c == 0x0A { ' ' } else { char::from_u32(c).unwrap() };
                        excerpt.push(ch);
                    }
                }
                let ctx = excerpt.trim_matches(py_isspace);
                let d = PyDict::new(py);
                d.set_item(intern!(py, "offset"), ii)?;
                d.set_item(intern!(py, "line"), line)?;
                d.set_item(intern!(py, "column"), col)?;
                d.set_item(intern!(py, "codepoint"), format!("U+{:04X}", cp))?;
                d.set_item(intern!(py, "name"), describe(cp))?;
                d.set_item(intern!(py, "severity"), sev)?;
                d.set_item(intern!(py, "context"), ctx)?;
                findings.append(d)?;
            }
        }
        if cp == 0x0A {
            line += 1;
            col = 1;
        } else {
            col += 1;
        }
    }
    Ok(findings.into_any().unbind())
}

#[pymodule]
fn fa_instruction_scan_scan_text(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_text, m)?)?;
    Ok(())
}
