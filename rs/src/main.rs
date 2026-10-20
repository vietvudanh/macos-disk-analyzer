use rustc_hash::{FxHashMap, FxHashSet};
use serde_json::Value;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::time::Instant;

fn default_host() -> String {
    std::process::Command::new("hostname")
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "localhost".to_string())
}

fn is_excluded_path(path: &str) -> bool {
    path.contains("/Library/CloudStorage/") || path.ends_with("/Library/CloudStorage")
}

fn normalize_path(raw: &str) -> String {
    let t = raw.trim_start_matches('/');
    let mut s = String::with_capacity(t.len() + 1);
    s.push('/');
    s.push_str(t);
    if s.starts_with("/System/Volumes/Data/") {
        return s[20..].to_string();
    }
    s
}

#[inline]
fn num(v: &Value, key: &str) -> u64 {
    v.get(key).and_then(|x| x.as_u64()).unwrap_or(0)
}

#[inline]
fn is_notreg(v: &Value) -> bool {
    v.get("notreg")
        .map(|x| x.as_bool().unwrap_or(true))
        .unwrap_or(false)
}

struct Stats {
    total_entries: u64,
}

fn walk_dir(
    node: &[Value],
    buf: &mut String,
    all_paths: &mut FxHashSet<String>,
    sizes: &mut FxHashMap<String, u64>,
    include_cloud: bool,
    stats: &mut Stats,
) {
    let Some(meta) = node.first() else { return };
    let name = meta.get("name").and_then(|n| n.as_str()).unwrap_or("");
    let prev_len = buf.len();
    if buf.is_empty() {
        buf.push('/');
        buf.push_str(name);
    } else {
        buf.push('/');
        buf.push_str(name);
    }
    let norm = normalize_path(buf);
    let excluded = !include_cloud && is_excluded_path(&norm);
    let is_dir = node.len() > 1 || is_notreg(meta);

    stats.total_entries += 1;
    if !excluded {
        all_paths.insert(norm.clone());
        if !is_dir {
            let asize = num(meta, "asize");
            let dsize = num(meta, "dsize");
            sizes.insert(norm, if dsize != 0 { dsize } else { asize });
        }
    }

    for child in &node[1..] {
        if let Some(arr) = child.as_array() {
            walk_dir(arr, buf, all_paths, sizes, include_cloud, stats);
        } else if let Some(obj) = child.as_object() {
            let cname = obj
                .get("name")
                .and_then(|n| n.as_str())
                .unwrap_or("");
            let prev = buf.len();
            buf.push('/');
            buf.push_str(cname);
            let cnorm = normalize_path(buf);
            stats.total_entries += 1;
            if include_cloud || !is_excluded_path(&cnorm) {
                all_paths.insert(cnorm.clone());
                let asize = obj.get("asize").and_then(|x| x.as_u64()).unwrap_or(0);
                let dsize = obj.get("dsize").and_then(|x| x.as_u64()).unwrap_or(0);
                sizes.insert(cnorm, if dsize != 0 { dsize } else { asize });
            }
            buf.truncate(prev);
        }
    }
    buf.truncate(prev_len);
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
    let mut args = std::env::args().skip(1).peekable();
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
    let path_str = input.to_string_lossy();
    let bytes = if path_str.ends_with(".zst") {
        let out = std::process::Command::new("zstd")
            .args(["-dc", input.to_str().unwrap()])
            .output()
            .expect("failed to run zstd -dc");
        out.stdout
    } else if path_str.ends_with(".gz") {
        let out = std::process::Command::new("gzip")
            .args(["-dc", input.to_str().unwrap()])
            .output()
            .expect("failed to run gzip -dc");
        out.stdout
    } else {
        std::fs::read(&input).expect("read json")
    };
    let data: Value = serde_json::from_slice(&bytes).expect("parse ncdu json");

    let meta = &data[2];
    let prog = meta.get("progname").and_then(|v| v.as_str()).unwrap_or("");
    let ver = meta.get("progver").and_then(|v| v.as_str()).unwrap_or("");
    let ts = meta.get("timestamp").map(|v| v.to_string()).unwrap_or_default();
    println!("ncdu version: {} {}", prog, ver);
    println!("Scan timestamp: {}", ts);

    println!("Parsing directory tree...");
    let tree = data.get(3).and_then(|v| v.as_array()).cloned().unwrap_or_default();

    // Pre-size: Python saw ~6.8M entries / ~3.2M unique on this host.
    let mut all_paths: FxHashSet<String> = FxHashSet::default();
    all_paths.reserve(3_500_000);
    let mut sizes: FxHashMap<String, u64> = FxHashMap::default();
    sizes.reserve(3_500_000);
    let mut stats = Stats { total_entries: 0 };
    let mut buf = String::with_capacity(256);
    walk_dir(&tree, &mut buf, &mut all_paths, &mut sizes, include_cloud, &mut stats);
    println!("Found {} entries", stats.total_entries);
    println!("Aggregating sizes by path...");
    println!("After deduplication: {} unique paths", sizes.len());

    println!("\n{}", "=".repeat(80));
    println!("TOP 30 LARGEST FILES");
    println!("{}", "=".repeat(80));
    let mut files: Vec<(&String, &u64)> = sizes.iter().collect();
    files.sort_unstable_by(|a, b| b.1.cmp(a.1));
    for (p, s) in files.iter().take(30) {
        println!("{}  {}", fmt_size(**s), p);
    }

    println!("\n{}", "=".repeat(80));
    println!("CLEANABLE DIRECTORIES (GROUPED & AGGREGATED)");
    println!("{}", "=".repeat(80));

    let mut sorted_all: Vec<&String> = all_paths.iter().collect();
    sorted_all.sort();
    let mut sorted_files: Vec<&String> = sizes.keys().collect();
    sorted_files.sort();

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
        let mut roots: Vec<&String> = Vec::new();
        for p in &sorted_all {
            if p.ends_with(suffix) && p.matches(suffix).count() == 1 {
                roots.push(p);
            }
        }
        if roots.is_empty() {
            continue;
        }
        let mut totals: Vec<(&String, u64)> = Vec::with_capacity(roots.len());
        for root in &roots {
            let prefix = format!("{}/", root);
            let start = sorted_files.partition_point(|p| p.as_str() < prefix.as_str());
            let mut total: u64 = 0;
            for p in sorted_files.iter().skip(start) {
                if !p.starts_with(prefix.as_str()) {
                    break;
                }
                total += sizes[*p];
            }
            totals.push((root, total));
        }
        let grand: u64 = totals.iter().map(|(_, t)| t).sum();
        let gb = grand as f64 / 1024.0 / 1024.0 / 1024.0;
        if gb >= 0.01 {
            println!("\n{}:", desc);
            println!("  {} directories, Total: {:.2} GB", totals.len(), gb);
            totals.sort_unstable_by(|a, b| b.1.cmp(&a.1));
            println!("  Top 10 largest:");
            for (p, s) in totals.iter().take(10) {
                println!("    {}  {}", fmt_size(*s), p);
            }
            let fname = format!("cleanable_{}_paths.txt", suffix.replace('/', "_").trim_matches('_'));
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
    for (p, s) in &sizes {
        // Preserve Python precedence: A or (B and C)
        let is_app = p.starts_with("/Applications/")
            || (p.starts_with("/Users/") && p.contains("/Library/"));
        if !is_app {
            continue;
        }
        let parts: Vec<&str> = p.split('/').collect();
        let key = if p.starts_with("/Applications/") && parts.len() >= 3 {
            format!("/{}/{}", parts[1], parts[2])
        } else if p.starts_with("/Users/") && parts.len() >= 5 {
            format!("/{}/{}/{}/{}", parts[1], parts[2], parts[3], parts[4])
        } else {
            continue;
        };
        *app.entry(key).or_insert(0) += *s;
    }
    let mut appv: Vec<(String, u64)> = app.into_iter().collect();
    appv.sort_unstable_by(|a, b| b.1.cmp(&a.1));
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
