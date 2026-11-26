use rustc_hash::{FxHashMap, FxHashSet};
use std::fs::File;
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Instant;

fn default_host() -> String {
    Command::new("hostname")
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "localhost".to_string())
}

#[inline]
fn is_excluded_path(path: &str) -> bool {
    path.contains("/Library/CloudStorage/") || path.ends_with("/Library/CloudStorage")
}

#[inline]
fn normalize_path_into(raw: &[u8], out: &mut String) {
    out.clear();
    let mut b = raw;
    while let Some((&b'/', rest)) = b.split_first() {
        b = rest;
    }
    const PREFIX: &[u8] = b"System/Volumes/Data/";
    if b.starts_with(PREFIX) {
        let slice = &b[PREFIX.len() - 1..];
        match std::str::from_utf8(slice) {
            Ok(s) => out.push_str(s),
            Err(_) => out.push_str(&String::from_utf8_lossy(slice)),
        }
    } else {
        out.push('/');
        match std::str::from_utf8(b) {
            Ok(s) => out.push_str(s),
            Err(_) => out.push_str(&String::from_utf8_lossy(b)),
        }
    }
}

struct SnapshotStream {
    reader: BufReader<Box<dyn Read>>,
    child: Option<Child>,
}

impl SnapshotStream {
    fn open(path: &Path) -> io::Result<Self> {
        let path_str = path.to_string_lossy();
        if path_str.ends_with(".zst") {
            let mut child = Command::new("zstd")
                .args(["-dcf", path.to_str().unwrap()])
                .stdout(Stdio::piped())
                .spawn()?;
            let stdout = child.stdout.take().expect("zstd stdout pipe");
            Ok(Self {
                reader: BufReader::with_capacity(1 << 20, Box::new(stdout)),
                child: Some(child),
            })
        } else if path_str.ends_with(".gz") {
            let mut child = Command::new("gzip")
                .args(["-dcf", path.to_str().unwrap()])
                .stdout(Stdio::piped())
                .spawn()?;
            let stdout = child.stdout.take().expect("gzip stdout pipe");
            Ok(Self {
                reader: BufReader::with_capacity(1 << 20, Box::new(stdout)),
                child: Some(child),
            })
        } else {
            let f = File::open(path)?;
            Ok(Self {
                reader: BufReader::with_capacity(1 << 20, Box::new(f)),
                child: None,
            })
        }
    }

    fn finish(mut self) -> io::Result<()> {
        let _ = io::copy(&mut self.reader, &mut io::sink());
        drop(self.reader);
        if let Some(mut child) = self.child {
            let status = child.wait()?;
            if !status.success() {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("decompressor exited with {}", status),
                ));
            }
        }
        Ok(())
    }
}

struct ScanMeta {
    progname: String,
    progver: String,
    timestamp: String,
}

struct NodeMeta {
    asize: u64,
    dsize: u64,
    ino: u64,
    hlnkc: bool,
    notreg: bool,
}

struct Stats {
    total_entries: u64,
}

struct JsonScanner<R: BufRead> {
    r: R,
    key_buf: Vec<u8>,
    name_buf: Vec<u8>,
    norm_buf: String,
    seen_inodes: FxHashSet<u64>,
}

impl<R: BufRead> JsonScanner<R> {
    fn new(r: R) -> Self {
        let mut seen_inodes = FxHashSet::default();
        seen_inodes.reserve(65_536);
        Self {
            r,
            key_buf: Vec::with_capacity(64),
            name_buf: Vec::with_capacity(256),
            norm_buf: String::with_capacity(512),
            seen_inodes,
        }
    }

    #[inline]
    fn read_byte(&mut self) -> io::Result<u8> {
        let buf = self.r.fill_buf()?;
        if buf.is_empty() {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "unexpected EOF"));
        }
        let b = buf[0];
        self.r.consume(1);
        Ok(b)
    }

    #[inline]
    fn peek_non_ws(&mut self) -> io::Result<u8> {
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "unexpected EOF"));
            }
            let mut i = 0;
            while i < buf.len() {
                match buf[i] {
                    b' ' | b'\t' | b'\n' | b'\r' => i += 1,
                    b => {
                        if i > 0 {
                            self.r.consume(i);
                        }
                        return Ok(b);
                    }
                }
            }
            let n = buf.len();
            self.r.consume(n);
        }
    }

    #[inline]
    fn next_non_ws(&mut self) -> io::Result<u8> {
        let b = self.peek_non_ws()?;
        self.r.consume(1);
        Ok(b)
    }

    #[inline]
    fn expect_byte(&mut self, want: u8) -> io::Result<()> {
        let b = self.next_non_ws()?;
        if b != want {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("expected '{}', got '{}'", want as char, b as char),
            ));
        }
        Ok(())
    }

    fn read_hex4(&mut self) -> io::Result<u16> {
        let mut val: u16 = 0;
        for _ in 0..4 {
            let b = self.read_byte()?;
            let d = match b {
                b'0'..=b'9' => (b - b'0') as u16,
                b'a'..=b'f' => (b - b'a' + 10) as u16,
                b'A'..=b'F' => (b - b'A' + 10) as u16,
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "invalid hex digit in \\u escape",
                    ))
                }
            };
            val = (val << 4) | d;
        }
        Ok(val)
    }

    fn read_string_into(&mut self, out: &mut Vec<u8>) -> io::Result<()> {
        out.clear();
        self.expect_byte(b'"')?;
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "unterminated string"));
            }
            if let Some(pos) = buf.iter().position(|&b| b == b'"' || b == b'\\') {
                let b = buf[pos];
                out.extend_from_slice(&buf[..pos]);
                self.r.consume(pos + 1);
                if b == b'"' {
                    return Ok(());
                }
                let esc = self.read_byte()?;
                match esc {
                    b'"' | b'\\' | b'/' => out.push(esc),
                    b'b' => out.push(0x08),
                    b'f' => out.push(0x0c),
                    b'n' => out.push(b'\n'),
                    b'r' => out.push(b'\r'),
                    b't' => out.push(b'\t'),
                    b'u' => {
                        let u1 = self.read_hex4()?;
                        let ch = if (0xD800..=0xDBFF).contains(&u1) {
                            let next2 = {
                                let b1 = self.read_byte()?;
                                if b1 == b'\\' {
                                    let b2 = self.read_byte()?;
                                    if b2 == b'u' {
                                        let u2 = self.read_hex4()?;
                                        if (0xDC00..=0xDFFF).contains(&u2) {
                                            let cp = 0x10000
                                                + (((u1 as u32 - 0xD800) << 10)
                                                    | (u2 as u32 - 0xDC00));
                                            char::from_u32(cp)
                                        } else {
                                            None
                                        }
                                    } else {
                                        None
                                    }
                                } else {
                                    None
                                }
                            };
                            next2.unwrap_or(char::REPLACEMENT_CHARACTER)
                        } else {
                            char::from_u32(u1 as u32).unwrap_or(char::REPLACEMENT_CHARACTER)
                        };
                        let mut tmp = [0u8; 4];
                        out.extend_from_slice(ch.encode_utf8(&mut tmp).as_bytes());
                    }
                    other => out.push(other),
                }
            } else {
                let n = buf.len();
                out.extend_from_slice(buf);
                self.r.consume(n);
            }
        }
    }

    fn skip_string(&mut self) -> io::Result<()> {
        self.expect_byte(b'"')?;
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "unterminated string"));
            }
            if let Some(pos) = buf.iter().position(|&b| b == b'"' || b == b'\\') {
                let b = buf[pos];
                self.r.consume(pos + 1);
                if b == b'"' {
                    return Ok(());
                }
                let _ = self.read_byte()?;
            } else {
                let n = buf.len();
                self.r.consume(n);
            }
        }
    }

    fn skip_value(&mut self) -> io::Result<()> {
        match self.peek_non_ws()? {
            b'"' => self.skip_string(),
            b'{' => {
                self.r.consume(1);
                if self.peek_non_ws()? == b'}' {
                    self.r.consume(1);
                    return Ok(());
                }
                loop {
                    self.skip_string()?;
                    self.expect_byte(b':')?;
                    self.skip_value()?;
                    match self.next_non_ws()? {
                        b'}' => return Ok(()),
                        b',' => continue,
                        other => {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                format!("unexpected '{}' in object", other as char),
                            ))
                        }
                    }
                }
            }
            b'[' => {
                self.r.consume(1);
                if self.peek_non_ws()? == b']' {
                    self.r.consume(1);
                    return Ok(());
                }
                loop {
                    self.skip_value()?;
                    match self.next_non_ws()? {
                        b']' => return Ok(()),
                        b',' => continue,
                        other => {
                            return Err(io::Error::new(
                                io::ErrorKind::InvalidData,
                                format!("unexpected '{}' in array", other as char),
                            ))
                        }
                    }
                }
            }
            _ => loop {
                let buf = self.r.fill_buf()?;
                if buf.is_empty() {
                    return Ok(());
                }
                if let Some(pos) = buf
                    .iter()
                    .position(|&b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | b',' | b'}' | b']'))
                {
                    self.r.consume(pos);
                    return Ok(());
                }
                let n = buf.len();
                self.r.consume(n);
            },
        }
    }

    #[inline]
    fn read_u64(&mut self) -> io::Result<u64> {
        let first = self.peek_non_ws()?;
        if !first.is_ascii_digit() {
            self.skip_value()?;
            return Ok(0);
        }
        let mut val: u64 = 0;
        let mut non_int = false;
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                return Ok(if non_int { 0 } else { val });
            }
            let mut i = 0;
            while i < buf.len() {
                let b = buf[i];
                if b.is_ascii_digit() {
                    val = val.wrapping_mul(10).wrapping_add((b - b'0') as u64);
                    i += 1;
                } else if matches!(b, b'.' | b'e' | b'E' | b'+' | b'-') {
                    non_int = true;
                    i += 1;
                } else {
                    self.r.consume(i);
                    return Ok(if non_int { 0 } else { val });
                }
            }
            let n = buf.len();
            self.r.consume(n);
        }
    }

    fn read_bool(&mut self) -> io::Result<bool> {
        match self.peek_non_ws()? {
            b't' => {
                self.skip_value()?;
                Ok(true)
            }
            b'f' => {
                self.skip_value()?;
                Ok(false)
            }
            _ => {
                self.skip_value()?;
                Ok(true)
            }
        }
    }

    fn read_raw_scalar(&mut self) -> io::Result<String> {
        if self.peek_non_ws()? == b'"' {
            let mut tmp = Vec::new();
            self.read_string_into(&mut tmp)?;
            return Ok(format!("\"{}\"", String::from_utf8_lossy(&tmp)));
        }
        let mut out = Vec::with_capacity(16);
        loop {
            let buf = self.r.fill_buf()?;
            if buf.is_empty() {
                break;
            }
            if let Some(pos) = buf
                .iter()
                .position(|&b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | b',' | b'}' | b']'))
            {
                out.extend_from_slice(&buf[..pos]);
                self.r.consume(pos);
                break;
            }
            out.extend_from_slice(buf);
            let n = buf.len();
            self.r.consume(n);
        }
        Ok(String::from_utf8_lossy(&out).into_owned())
    }

    fn read_scan_meta(&mut self) -> io::Result<ScanMeta> {
        let mut meta = ScanMeta {
            progname: String::new(),
            progver: String::new(),
            timestamp: String::new(),
        };
        self.expect_byte(b'{')?;
        if self.peek_non_ws()? == b'}' {
            self.r.consume(1);
            return Ok(meta);
        }
        let mut key = Vec::with_capacity(32);
        let mut val = Vec::with_capacity(64);
        loop {
            self.read_string_into(&mut key)?;
            self.expect_byte(b':')?;
            match key.as_slice() {
                b"progname" => {
                    self.read_string_into(&mut val)?;
                    meta.progname = String::from_utf8_lossy(&val).into_owned();
                }
                b"progver" => {
                    self.read_string_into(&mut val)?;
                    meta.progver = String::from_utf8_lossy(&val).into_owned();
                }
                b"timestamp" => {
                    meta.timestamp = self.read_raw_scalar()?;
                }
                _ => self.skip_value()?,
            }
            match self.next_non_ws()? {
                b'}' => return Ok(meta),
                b',' => continue,
                other => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("unexpected '{}' in scan metadata", other as char),
                    ))
                }
            }
        }
    }

    #[inline]
    fn read_node_meta(&mut self) -> io::Result<NodeMeta> {
        let mut meta = NodeMeta {
            asize: 0,
            dsize: 0,
            ino: 0,
            hlnkc: false,
            notreg: false,
        };
        self.name_buf.clear();
        self.expect_byte(b'{')?;
        if self.peek_non_ws()? == b'}' {
            self.r.consume(1);
            return Ok(meta);
        }
        loop {
            let mut key_buf = std::mem::take(&mut self.key_buf);
            self.read_string_into(&mut key_buf)?;
            self.expect_byte(b':')?;
            let res = match key_buf.as_slice() {
                b"name" => {
                    let mut name_buf = std::mem::take(&mut self.name_buf);
                    let r = self.read_string_into(&mut name_buf);
                    self.name_buf = name_buf;
                    r
                }
                b"asize" => self.read_u64().map(|v| meta.asize = v),
                b"dsize" => self.read_u64().map(|v| meta.dsize = v),
                b"ino" => self.read_u64().map(|v| meta.ino = v),
                b"hlnkc" => self.read_bool().map(|v| meta.hlnkc = v),
                b"notreg" => self.read_bool().map(|v| meta.notreg = v),
                _ => self.skip_value(),
            };
            self.key_buf = key_buf;
            res?;
            match self.next_non_ws()? {
                b'}' => return Ok(meta),
                b',' => continue,
                other => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("unexpected '{}' in node metadata", other as char),
                    ))
                }
            }
        }
    }

    fn walk_dir(
        &mut self,
        buf: &mut Vec<u8>,
        entries: &mut FxHashMap<Box<str>, Option<u64>>,
        include_cloud: bool,
        stats: &mut Stats,
    ) -> io::Result<()> {
        self.expect_byte(b'[')?;
        if self.peek_non_ws()? == b']' {
            self.r.consume(1);
            return Ok(());
        }

        let _meta = self.read_node_meta()?;
        let prev_len = buf.len();
        buf.push(b'/');
        buf.extend_from_slice(&self.name_buf);

        normalize_path_into(buf, &mut self.norm_buf);
        let excluded = !include_cloud && is_excluded_path(&self.norm_buf);

        // In ncdu JSON, an array node is always a directory.
        stats.total_entries += 1;
        if !excluded && !entries.contains_key(self.norm_buf.as_str()) {
            entries.insert(Box::from(self.norm_buf.as_str()), None);
        }

        while self.peek_non_ws()? == b',' {
            self.r.consume(1);
            match self.peek_non_ws()? {
                b'[' => self.walk_dir(buf, entries, include_cloud, stats)?,
                b'{' => {
                    let cmeta = self.read_node_meta()?;
                    let prev = buf.len();
                    buf.push(b'/');
                    buf.extend_from_slice(&self.name_buf);
                    normalize_path_into(buf, &mut self.norm_buf);
                    stats.total_entries += 1;
                    if include_cloud || !is_excluded_path(&self.norm_buf) {
                        let skip_hardlink = cmeta.hlnkc
                            && cmeta.ino != 0
                            && !self.seen_inodes.insert(cmeta.ino);
                        if !skip_hardlink {
                            // Use actual disk size only (do not fall back to asize for sparse/cloud files).
                            let sz = cmeta.dsize;
                            if let Some(slot) = entries.get_mut(self.norm_buf.as_str()) {
                                *slot = Some(sz);
                            } else {
                                entries.insert(Box::from(self.norm_buf.as_str()), Some(sz));
                            }
                        }
                    }
                    buf.truncate(prev);
                }
                _ => self.skip_value()?,
            }
        }

        self.expect_byte(b']')?;
        buf.truncate(prev_len);
        Ok(())
    }
}

fn fmt_size(size: u64) -> String {
    let gb = size as f64 / 1024.0 / 1024.0 / 1024.0;
    if gb >= 1.0 {
        format!("{:>8.2} GB", gb)
    } else {
        format!("{:>8.2} MB", size as f64 / 1024.0 / 1024.0)
    }
}

fn main() {
    let t0 = Instant::now();
    let mut host = default_host();
    let mut include_cloud = false;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--host" => {
                if let Some(v) = args.next() {
                    host = v;
                }
            }
            s if s.starts_with("--host=") => host = s["--host=".len()..].to_string(),
            "--include-cloudstorage" => include_cloud = true,
            _ => {}
        }
    }

    let data_dir = PathBuf::from("data").join(&host);
    std::fs::create_dir_all(&data_dir).expect("create data dir");
    println!("Host: {}", host);
    println!("Output directory: {}/", data_dir.display());

    let candidates = [
        data_dir.join("disk_usage.json.zst"),
        data_dir.join("disk_usage.json.gz"),
        data_dir.join("disk_usage.json"),
    ];
    let input = candidates
        .iter()
        .find(|p| p.exists())
        .cloned()
        .unwrap_or_else(|| candidates[0].clone());
    if !input.exists() {
        println!(
            "Error: no snapshot found (.json.zst, .json.gz, .json) in {}.",
            data_dir.display()
        );
        return;
    }

    println!("Loading snapshot file: {}...", input.display());
    let mut stream = SnapshotStream::open(&input).expect("open snapshot");

    // Pre-size: ~2.7M–3.5M unique paths on typical macOS hosts.
    let mut entries: FxHashMap<Box<str>, Option<u64>> = FxHashMap::default();
    entries.reserve(3_500_000);
    let mut stats = Stats { total_entries: 0 };

    {
        let mut scanner = JsonScanner::new(&mut stream.reader);
        scanner.expect_byte(b'[').expect("outer '['");
        scanner.skip_value().expect("major version");
        scanner.expect_byte(b',').expect("comma 1");
        scanner.skip_value().expect("minor version");
        scanner.expect_byte(b',').expect("comma 2");
        let meta = scanner.read_scan_meta().expect("scan metadata");
        println!("ncdu version: {} {}", meta.progname, meta.progver);
        println!("Scan timestamp: {}", meta.timestamp);
        println!("Parsing directory tree...");
        scanner.expect_byte(b',').expect("comma 3");

        let mut buf = Vec::with_capacity(256);
        scanner
            .walk_dir(&mut buf, &mut entries, include_cloud, &mut stats)
            .expect("walk ncdu tree");
        let _ = scanner.expect_byte(b']');
    }
    stream.finish().expect("finish snapshot stream");

    let mut files: Vec<(&str, u64)> = Vec::with_capacity(entries.len());
    let mut sorted_all: Vec<&str> = Vec::with_capacity(entries.len());
    for (p, sz) in &entries {
        sorted_all.push(p.as_ref());
        if let Some(s) = sz {
            files.push((p.as_ref(), *s));
        }
    }

    println!("Found {} entries", stats.total_entries);
    println!("Aggregating sizes by path...");
    println!("After deduplication: {} unique paths", files.len());

    println!("\n{}", "=".repeat(80));
    println!("TOP 30 LARGEST FILES");
    println!("{}", "=".repeat(80));
    files.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(b.0)));
    for (p, s) in files.iter().take(30) {
        println!("{}  {}", fmt_size(*s), p);
    }

    println!("\n{}", "=".repeat(80));
    println!("CLEANABLE DIRECTORIES (GROUPED & AGGREGATED)");
    println!("{}", "=".repeat(80));

    sorted_all.sort_unstable();
    let mut sorted_files: Vec<(&str, u64)> = files;
    sorted_files.sort_unstable_by(|a, b| a.0.cmp(b.0));

    let patterns: &[(&str, &str)] = &[
        ("/node_modules", "Node.js dependencies (node_modules)"),
        ("/.venv", "Python virtual environments (.venv)"),
        ("/venv", "Python virtual environments (venv)"),
        ("/target", "Rust/Cargo build artifacts (target)"),
        ("/.m2", "Maven cache (.m2)"),
        ("/.gradle", "Gradle cache (.gradle)"),
        ("/.npm", "NPM cache (.npm)"),
        ("/__pycache__", "Python bytecode cache (__pycache__)"),
        ("/Library/Caches", "macOS application caches"),
        ("/Library/Logs", "macOS log files"),
        ("/.cache", "General cache directories (.cache)"),
        ("/.git", "Git repositories (.git)"),
    ];

    for (suffix, desc) in patterns {
        let mut roots: Vec<&str> = Vec::new();
        for &p in &sorted_all {
            if p.ends_with(suffix) && p.matches(suffix).count() == 1 {
                roots.push(p);
            }
        }
        if roots.is_empty() {
            continue;
        }
        let mut totals: Vec<(&str, u64)> = Vec::with_capacity(roots.len());
        let mut prefix = String::with_capacity(128);
        for &root in &roots {
            prefix.clear();
            prefix.push_str(root);
            prefix.push('/');
            let start = sorted_files.partition_point(|(p, _)| *p < prefix.as_str());
            let mut total: u64 = 0;
            for &(p, sz) in &sorted_files[start..] {
                if !p.starts_with(prefix.as_str()) {
                    break;
                }
                total += sz;
            }
            totals.push((root, total));
        }
        let grand: u64 = totals.iter().map(|(_, t)| t).sum();
        let gb = grand as f64 / 1024.0 / 1024.0 / 1024.0;
        if gb >= 0.01 {
            println!("\n{}:", desc);
            println!("  {} directories, Total: {:.2} GB", totals.len(), gb);
            totals.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(b.0)));
            println!("  Top 10 largest:");
            for (p, s) in totals.iter().take(10) {
                println!("    {}  {}", fmt_size(*s), p);
            }
            let fname = format!(
                "cleanable_{}_paths.txt",
                suffix.replace('/', "_").trim_matches('_')
            );
            let out = data_dir.join(&fname);
            let of = File::create(&out).expect("create output");
            let mut w = BufWriter::new(of);
            for (p, s) in &totals {
                writeln!(w, "{:.4}\t{}", *s as f64 / 1024.0 / 1024.0 / 1024.0, p).unwrap();
            }
            w.flush().unwrap();
            println!("  → Exported all {} paths to: {}", totals.len(), out.display());
        }
    }

    println!("\n{}", "=".repeat(80));
    println!("LARGEST APPLICATION & USER DIRECTORIES");
    println!("{}", "=".repeat(80));
    let mut app: FxHashMap<String, u64> = FxHashMap::default();
    for &(p, s) in &sorted_files {
        let is_app = p.starts_with("/Applications/")
            || (p.starts_with("/Users/") && p.contains("/Library/"));
        if !is_app {
            continue;
        }
        let key = if p.starts_with("/Applications/") {
            let mut it = p.splitn(4, '/');
            let _ = it.next();
            match (it.next(), it.next()) {
                (Some(p1), Some(p2)) => format!("/{}/{}", p1, p2),
                _ => continue,
            }
        } else {
            let mut it = p.splitn(6, '/');
            let _ = it.next();
            match (it.next(), it.next(), it.next(), it.next()) {
                (Some(p1), Some(p2), Some(p3), Some(p4)) => {
                    format!("/{}/{}/{}/{}", p1, p2, p3, p4)
                }
                _ => continue,
            }
        };
        *app.entry(key).or_insert(0) += s;
    }
    let mut appv: Vec<(String, u64)> = app.into_iter().collect();
    appv.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    for (p, s) in appv.iter().take(20) {
        if *s as f64 / 1024.0 / 1024.0 / 1024.0 >= 0.1 {
            println!("  {}  {}", fmt_size(*s), p);
        }
    }

    println!("\n{}", "=".repeat(80));
    println!("Analysis complete!");
    println!("All output files saved to: {}/", data_dir.display());
    println!("Done in {:.1}s", t0.elapsed().as_secs_f64());
    println!("{}", "=".repeat(80));
}
