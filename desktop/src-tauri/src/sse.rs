//! An incremental Server-Sent Events parser for `/api/events`.
//!
//! `EventSource` would do this in the webview, but it cannot send an
//! `Authorization` header, which is why the phone polls (see the header of
//! `mobile/src/api/client.ts`). The Rust core holds the stream with the Bearer
//! header instead and feeds the bytes through this parser as they arrive.
//!
//! It follows the WHATWG event-stream rules that matter for server.py's frames
//! (`handlers/workspace.py: handle_events_stream`): lines end in LF, CRLF or a
//! lone CR; a blank line dispatches; `:` lines are comments (the server's
//! 15-second keep-alive); one optional space after the colon is dropped;
//! repeated `data:` lines join with a newline. Chunks can split anywhere, even
//! inside a UTF-8 sequence, so bytes are buffered until a line is complete.

/// One dispatched event. `event` is `message` when the frame named none.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseEvent {
    pub event: String,
    pub data: String,
}

/// What a chunk produced: events, and whether a keep-alive comment arrived
/// (proof the connection is alive even when nothing happened).
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Parsed {
    pub events: Vec<SseEvent>,
    pub heartbeat: bool,
}

/// A line longer than this is not one server.py sends; drop the connection's
/// buffer rather than grow without bound on a misbehaving peer.
const MAX_LINE: usize = 1 << 20;

#[derive(Default)]
pub struct SseParser {
    buf: Vec<u8>,
    event: String,
    data: Vec<String>,
    /// The previous chunk ended in CR, so a leading LF belongs to that line.
    skip_lf: bool,
}

impl SseParser {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, chunk: &[u8]) -> Parsed {
        let mut out = Parsed::default();
        for &b in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if b == b'\n' {
                    continue;
                }
            }
            match b {
                b'\n' | b'\r' => {
                    self.skip_lf = b == b'\r';
                    let line = std::mem::take(&mut self.buf);
                    self.line(&String::from_utf8_lossy(&line), &mut out);
                }
                _ => {
                    if self.buf.len() >= MAX_LINE {
                        self.buf.clear();
                        self.data.clear();
                        self.event.clear();
                    }
                    self.buf.push(b);
                }
            }
        }
        out
    }

    fn line(&mut self, line: &str, out: &mut Parsed) {
        if line.is_empty() {
            if !self.data.is_empty() {
                let event = if self.event.is_empty() { "message".to_string() } else { std::mem::take(&mut self.event) };
                out.events.push(SseEvent { event, data: self.data.join("\n") });
            }
            self.data.clear();
            self.event.clear();
            return;
        }
        if line.starts_with(':') {
            out.heartbeat = true;
            return;
        }
        let (field, value) = match line.find(':') {
            Some(i) => {
                let v = &line[i + 1..];
                (&line[..i], v.strip_prefix(' ').unwrap_or(v))
            }
            None => (line, ""),
        };
        match field {
            "event" => self.event = value.to_string(),
            "data" => self.data.push(value.to_string()),
            // id / retry: the core reconnects on its own schedule and the
            // server does not resume by id, so both are ignored.
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all(chunks: &[&[u8]]) -> (Vec<SseEvent>, bool) {
        let mut p = SseParser::new();
        let mut events = vec![];
        let mut hb = false;
        for c in chunks {
            let r = p.feed(c);
            events.extend(r.events);
            hb |= r.heartbeat;
        }
        (events, hb)
    }

    fn ev(event: &str, data: &str) -> SseEvent {
        SseEvent { event: event.into(), data: data.into() }
    }

    #[test]
    fn parses_the_frames_server_py_sends() {
        let (events, hb) = all(&[
            b"event: ready\ndata: {}\n\n",
            b": keep-alive\n\n",
            b"event: feed.item\ndata: {\"id\": \"f1\", \"kind\": \"task\"}\n\n",
            b"event: end\ndata: timeout\n\n",
        ]);
        assert!(hb);
        assert_eq!(
            events,
            vec![ev("ready", "{}"), ev("feed.item", "{\"id\": \"f1\", \"kind\": \"task\"}"), ev("end", "timeout")]
        );
    }

    #[test]
    fn chunks_may_split_anywhere() {
        let frame = "event: task.status\ndata: {\"title\":\"caf\u{e9} \u{2603}\"}\n\n".as_bytes();
        for split in 1..frame.len() {
            let (events, _) = all(&[&frame[..split], &frame[split..]]);
            assert_eq!(events, vec![ev("task.status", "{\"title\":\"caf\u{e9} \u{2603}\"}")], "split at {split}");
        }
    }

    #[test]
    fn crlf_and_lone_cr_end_lines() {
        let (events, _) = all(&[b"event: a\r\ndata: 1\r\n\r\n", b"event: b\rdata: 2\r\r"]);
        assert_eq!(events, vec![ev("a", "1"), ev("b", "2")]);
        // CR at the end of one chunk, LF at the start of the next: one line end.
        let (events, _) = all(&[b"data: x\r", b"\n\r", b"\n"]);
        assert_eq!(events, vec![ev("message", "x")]);
    }

    #[test]
    fn multi_line_data_joins_and_spacing_follows_the_spec() {
        let (events, _) = all(&[b"data:a\ndata:  b\ndata\n\n"]);
        assert_eq!(events, vec![ev("message", "a\n b\n")]);
    }

    #[test]
    fn a_frame_without_data_dispatches_nothing() {
        let (events, _) = all(&[b"event: lonely\n\n", b"id: 7\nretry: 10\n\n"]);
        assert!(events.is_empty());
    }

    #[test]
    fn the_event_name_does_not_leak_into_the_next_frame() {
        let (events, _) = all(&[b"event: one\ndata: 1\n\ndata: 2\n\n"]);
        assert_eq!(events, vec![ev("one", "1"), ev("message", "2")]);
    }

    #[test]
    fn an_oversized_line_is_dropped_not_buffered_forever() {
        let mut p = SseParser::new();
        let big = vec![b'x'; MAX_LINE + 10];
        p.feed(b"data: ");
        assert!(p.feed(&big).events.is_empty());
        assert!(p.buf.len() <= MAX_LINE);
        let r = p.feed(b"\n\nevent: ok\ndata: 1\n\n");
        assert_eq!(r.events.last(), Some(&ev("ok", "1")));
    }
}
