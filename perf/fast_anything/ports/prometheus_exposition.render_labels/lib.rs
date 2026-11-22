//! Rust port of prometheus_exposition.render_labels (prometheus_exposition.py:126).
//!
//! Faithful to the Python, including its quirks:
//! * `LABEL_NAME_RE.match` uses `$`, which also matches just before ONE
//!   trailing '\n' — so "abc\n" is an accepted label name.
//! * `sorted(labels)` orders str keys by code point; Rust `&str` ordering is
//!   UTF-8 byte order, which is the same order. Anything that is not a plain
//!   dict of str keys goes through Python's own `sorted()` so mixed-type keys
//!   raise the same TypeError.
//! * Values: None -> '', else `str(value)`; control chars dropped; truncated
//!   to 199 code points + '…' when over 200; then \\ " \n escaped.
//! * Errors are the module's own `ExpositionError`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

const MAX_LABEL_VALUE_LEN: usize = 200;

#[inline]
fn is_control(c: char) -> bool {
    // [\x00-\x08\x0b-\x1f\x7f]
    matches!(c, '\u{00}'..='\u{08}' | '\u{0b}'..='\u{1f}' | '\u{7f}')
}

/// `LABEL_NAME_RE.match(name)` for r'^[a-zA-Z_][a-zA-Z0-9_]*$'.
fn label_name_ok(name: &str) -> bool {
    let core = name.strip_suffix('\n').unwrap_or(name);
    let b = core.as_bytes();
    match b.first() {
        Some(c) if c.is_ascii_alphabetic() || *c == b'_' => {}
        _ => return false,
    }
    b[1..].iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_')
}

fn exposition_error(py: Python<'_>, msg: String) -> PyErr {
    match py
        .import("prometheus_exposition")
        .and_then(|m| m.getattr("ExpositionError"))
    {
        Ok(cls) => match cls.call1((msg.clone(),)) {
            Ok(inst) => PyErr::from_value(inst),
            Err(e) => e,
        },
        Err(_) => PyValueError::new_err(msg),
    }
}

/// Append escape_label_value(s) to `out`.
fn push_escaped(out: &mut String, s: &str) {
    // Fast path: ASCII with nothing to drop, escape or truncate.
    let bytes = s.as_bytes();
    if bytes.len() <= MAX_LABEL_VALUE_LEN
        && bytes
            .iter()
            .all(|&c| c >= 0x20 && c != 0x7f && c != b'\\' && c != b'"' && c < 0x80)
    {
        out.push_str(s);
        return;
    }
    // Count kept code points first so truncation matches Python exactly.
    let kept = s.chars().filter(|&c| !is_control(c)).count();
    let limit = if kept > MAX_LABEL_VALUE_LEN {
        MAX_LABEL_VALUE_LEN - 1
    } else {
        usize::MAX
    };
    let mut n = 0usize;
    for c in s.chars() {
        if is_control(c) {
            continue;
        }
        if n == limit {
            break;
        }
        n += 1;
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            _ => out.push(c),
        }
    }
    if limit != usize::MAX {
        out.push('…');
    }
}

fn push_value(out: &mut String, value: &Bound<'_, PyAny>) -> PyResult<()> {
    if value.is_none() {
        return Ok(());
    }
    if let Ok(s) = value.cast_exact::<PyString>() {
        push_escaped(out, &s.to_str()?);
        return Ok(());
    }
    let s = value.str()?;
    push_escaped(out, &s.to_str()?);
    Ok(())
}

fn check_name(py: Python<'_>, name: &Bound<'_, PyAny>) -> PyResult<()> {
    let ok = match name.cast::<PyString>() {
        Ok(s) => match s.to_str() {
            Ok(t) => label_name_ok(&t),
            Err(_) => false, // lone surrogates can't match the ASCII pattern
        },
        Err(_) => false,
    };
    if !ok {
        return Err(exposition_error(
            py,
            format!("invalid label name: {}", name.repr()?.to_str()?),
        ));
    }
    let s = name.cast::<PyString>()?.to_str()?;
    if s.starts_with("__") {
        return Err(exposition_error(
            py,
            format!("reserved label name: {}", name.repr()?.to_str()?),
        ));
    }
    Ok(())
}

#[pyfunction]
fn render_labels(py: Python<'_>, labels: &Bound<'_, PyAny>) -> PyResult<String> {
    if labels.is_none() || !labels.is_truthy()? {
        return Ok(String::new());
    }

    // Fast path: an exact dict whose keys are all exact, valid str.
    if let Ok(d) = labels.cast_exact::<PyDict>() {
        let mut items: Vec<(String, Bound<'_, PyAny>)> = Vec::with_capacity(d.len());
        let mut simple = true;
        for (k, v) in d.iter() {
            match k.cast_exact::<PyString>() {
                Ok(ks) => match ks.to_str() {
                    Ok(t) => items.push((t.to_owned(), v)),
                    Err(_) => {
                        simple = false;
                        break;
                    }
                },
                Err(_) => {
                    simple = false;
                    break;
                }
            }
        }
        if simple {
            items.sort_unstable_by(|a, b| a.0.cmp(&b.0));
            let mut out = String::with_capacity(16 + items.len() * 32);
            out.push('{');
            for (i, (name, value)) in items.iter().enumerate() {
                if !label_name_ok(name) {
                    let r = PyString::new(py, name).repr()?;
                    return Err(exposition_error(
                        py,
                        format!("invalid label name: {}", r.to_str()?),
                    ));
                }
                if name.starts_with("__") {
                    let r = PyString::new(py, name).repr()?;
                    return Err(exposition_error(
                        py,
                        format!("reserved label name: {}", r.to_str()?),
                    ));
                }
                if i > 0 {
                    out.push(',');
                }
                out.push_str(name);
                out.push_str("=\"");
                push_value(&mut out, value)?;
                out.push('"');
            }
            out.push('}');
            return Ok(out);
        }
    }

    // General path: exactly what Python does, via its own sorted() and [].
    let sorted = py
        .import("builtins")?
        .getattr("sorted")?
        .call1((labels,))?;
    let names = sorted.cast::<PyList>()?;
    let mut out = String::with_capacity(16 + names.len() * 32);
    out.push('{');
    for (i, name) in names.iter().enumerate() {
        check_name(py, &name)?;
        let value = labels.get_item(&name)?;
        if i > 0 {
            out.push(',');
        }
        out.push_str(&name.cast::<PyString>()?.to_str()?);
        out.push_str("=\"");
        push_value(&mut out, &value)?;
        out.push('"');
    }
    out.push('}');
    Ok(out)
}

#[pymodule]
fn fa_prometheus_exposition_render_labels(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(render_labels, m)?)?;
    Ok(())
}
