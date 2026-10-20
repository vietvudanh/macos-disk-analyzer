#!/usr/bin/env python3
"""
Compare disk usage between two ncdu snapshots over time.
Supports compressed (.json.zst, .json.gz) and raw (.json) snapshots.

Finds:
  - What has increased/decreased in size (with size deltas and before/after)
  - What is new (new files and newly created directories)
  - What was deleted (deleted files and removed directories)
  - What remained the same (unchanged files volume and counts)
  - Category-by-category comparison (node_modules, target, .venv, caches, etc.)

Run with: uvx --with tqdm python compare_ncdu.py
Or: python3 compare_ncdu.py --list
"""

import argparse
import datetime
import gc
import gzip
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

try:
    from tqdm import tqdm
except ImportError:
    # Graceful fallback if tqdm is not installed
    def tqdm(iterable, *args, **kwargs):
        return iterable


def format_size(size_bytes: float, show_sign: bool = False) -> str:
    """Format bytes into human-readable string (GB, MB, or KB)."""
    abs_bytes = abs(size_bytes)
    sign = "+" if size_bytes > 0 and show_sign else ("-" if size_bytes < 0 else " ")

    if abs_bytes >= 1024 * 1024 * 1024:
        gb = abs_bytes / (1024 * 1024 * 1024)
        return f"{sign}{gb:>7.2f} GB"
    elif abs_bytes >= 1024 * 1024:
        mb = abs_bytes / (1024 * 1024)
        return f"{sign}{mb:>7.2f} MB"
    elif abs_bytes >= 1024:
        kb = abs_bytes / 1024
        return f"{sign}{kb:>7.2f} KB"
    else:
        return f"{sign}{abs_bytes:>7.0f} B "


def parse_size_str(size_str: str) -> int:
    """Parse size strings like '50MB', '1GB', '100K', '1048576' into bytes."""
    size_str = size_str.strip().upper()
    if not size_str:
        return 0
    multipliers = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024 * 1024,
        "MB": 1024 * 1024,
        "G": 1024 * 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
        "T": 1024 * 1024 * 1024 * 1024,
        "TB": 1024 * 1024 * 1024 * 1024,
    }
    match = re.match(r"^([0-9.]+)\s*([A-Z]*)$", size_str)
    if not match:
        raise ValueError(f"Invalid size string format: '{size_str}'")
    val = float(match.group(1))
    unit = match.group(2) or "B"
    if unit not in multipliers:
        raise ValueError(f"Unknown unit in size string: '{unit}'")
    return int(val * multipliers[unit])


def normalize_path(path: str) -> str:
    """Remove System/Volumes/Data prefix and extra leading slashes to deduplicate paths."""
    path = "/" + path.lstrip("/")
    if path.startswith("/System/Volumes/Data/"):
        return path[20:]
    return path


def is_excluded_path(path: str) -> bool:
    """Skip virtual/cloud files not realized on local disk (Drive File Provider, etc.)."""
    return "/Library/CloudStorage/" in path or path.endswith("/Library/CloudStorage")


def get_compression_format(path: Path) -> str:
    """Detect compression format based on file extension."""
    name = path.name.lower()
    if ".zst" in name:
        return "zstd"
    elif ".gz" in name:
        return "gzip"
    return "raw"


def read_snapshot_header(file_path: Path, max_bytes: int = 2048) -> Optional[str]:
    """Quickly read decompressed header chunk from .json, .zst, or .gz file."""
    name = file_path.name.lower()
    try:
        if ".gz" in name:
            with gzip.open(file_path, "rb") as f:
                return f.read(max_bytes).decode("utf-8", errors="ignore")
        elif ".zst" in name:
            try:
                import zstandard as zstd
                with open(file_path, "rb") as f:
                    dctx = zstd.ZstdDecompressor()
                    chunk = dctx.decompress(f.read(max_bytes * 4), max_output_size=max_bytes)
                    return chunk.decode("utf-8", errors="ignore")
            except Exception:
                proc = subprocess.Popen(
                    ["zstd", "-dc", str(file_path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                head = proc.stdout.read(max_bytes).decode("utf-8", errors="ignore")
                proc.terminate()
                return head
        else:
            with open(file_path, "rb") as f:
                return f.read(max_bytes).decode("utf-8", errors="ignore")
    except Exception:
        return None


def open_snapshot_file(file_path: Path):
    """
    Open snapshot file and return a text stream, transparently decompressing .zst or .gz.
    """
    file_path = Path(file_path)
    name = file_path.name.lower()

    if ".gz" in name:
        return gzip.open(file_path, "rt", encoding="utf-8")
    elif ".zst" in name:
        try:
            import zstandard as zstd
            dctx = zstd.ZstdDecompressor()
            f = open(file_path, "rb")
            reader = dctx.stream_reader(f)
            return io.TextIOWrapper(reader, encoding="utf-8")
        except ImportError:
            proc = subprocess.Popen(
                ["zstd", "-dc", str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            return io.TextIOWrapper(proc.stdout, encoding="utf-8")
    else:
        return open(file_path, "r", encoding="utf-8")


def strip_snapshot_ext(name: str) -> str:
    """Strip snapshot extension to identify companion files."""
    for ext in [".json.zst", ".json.gz", ".json", ".parquet", ".zst", ".gz"]:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def get_snapshot_info(file_path: Path) -> Optional[Dict[str, Any]]:
    """Quickly extract metadata (scan timestamp, program, host, date) from file header or metadata."""
    try:
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return None

        # Determine host from path
        parts = file_path.resolve().parts
        host_name = "unknown"
        if "data" in parts:
            idx = parts.index("data")
            if idx + 1 < len(parts):
                host_name = parts[idx + 1]

        # Parquet snapshot
        if file_path.name.lower().endswith(".parquet"):
            ts = None
            date_match = re.search(r"(\d{4}-\d{2}-\d{2}[-_]\d{6})", file_path.name)
            if date_match:
                d_str = date_match.group(1).replace("_", " ")
                try:
                    ts = int(datetime.datetime.strptime(d_str, "%Y-%m-%d %H%M%S").timestamp())
                except Exception:
                    pass
            if not ts:
                d_match2 = re.search(r"(\d{4}-\d{2}-\d{2})", file_path.name)
                if d_match2:
                    try:
                        ts = int(datetime.datetime.strptime(d_match2.group(1), "%Y-%m-%d").timestamp())
                    except Exception:
                        pass
            if not ts:
                ts = int(file_path.stat().st_mtime)

            dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            return {
                "path": file_path,
                "filename": file_path.name,
                "timestamp": ts,
                "date": dt,
                "host": host_name,
                "file_size_bytes": file_path.stat().st_size,
                "format": "parquet",
                "progver": "parquet",
            }

        head = read_snapshot_header(file_path, max_bytes=2048)
        if not head:
            return None

        # Basic verification that this is an ncdu json export
        if '"progname"' not in head and not head.startswith("["):
            return None

        # Extract timestamp
        ts = None
        ts_match = re.search(r'"timestamp":\s*(\d+)', head)
        if ts_match:
            ts = int(ts_match.group(1))
        else:
            ts = int(file_path.stat().st_mtime)

        progver = ""
        prog_match = re.search(r'"progver":\s*"([^"]+)"', head)
        if prog_match:
            progver = prog_match.group(1)

        dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        fmt = get_compression_format(file_path)

        return {
            "path": file_path,
            "filename": file_path.name,
            "timestamp": ts,
            "date": dt,
            "host": host_name,
            "file_size_bytes": file_path.stat().st_size,
            "format": fmt,
            "progver": progver,
        }
    except Exception:
        return None


def discover_snapshots(base_dir: Path = Path("data"), host: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Discover all ncdu snapshots (.json.zst, .json.gz, .json, .parquet) across data directory.
    Sorts them chronologically (oldest to newest).
    Deduplicates companion .parquet and .json.zst files representing the same scan.
    """
    if not base_dir.exists():
        return []

    candidates: List[Path] = []
    for root, _, files in os.walk(base_dir):
        # Skip diff exports directory from snapshot discovery
        if "/diffs" in root or root.endswith("/diffs"):
            continue
        for f in files:
            fl = f.lower()
            if (
                fl.endswith(".json")
                or fl.endswith(".json.zst")
                or fl.endswith(".json.gz")
                or fl.endswith(".zst")
                or fl.endswith(".gz")
                or fl.endswith(".parquet")
            ):
                candidates.append(Path(root) / f)

    snapshots_by_stem: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen_real_paths: Set[str] = set()

    for p in candidates:
        try:
            real_path = str(p.resolve())
            if real_path in seen_real_paths:
                continue
            info = get_snapshot_info(p)
            if not info:
                continue
            if host and info["host"] != host:
                continue
            seen_real_paths.add(real_path)

            stem_key = (str(p.parent.resolve()), strip_snapshot_ext(p.name))
            if stem_key in snapshots_by_stem:
                existing = snapshots_by_stem[stem_key]
                # If we saw parquet earlier but now found json.zst, prefer json.zst as primary
                # and attach parquet as companion
                if info["format"] != "parquet" and existing["format"] == "parquet":
                    parquet_p = existing["path"]
                    existing.update(info)
                    existing["companion_parquet"] = parquet_p
                elif info["format"] == "parquet":
                    existing["companion_parquet"] = info["path"]
            else:
                snapshots_by_stem[stem_key] = info
        except Exception:
            continue

    snapshots = list(snapshots_by_stem.values())
    # Sort chronologically
    snapshots.sort(key=lambda x: (x["timestamp"], str(x["path"])))
    return snapshots


def list_snapshots(snapshots: List[Dict[str, Any]]):
    """Print formatted list of available snapshots."""
    print("=" * 96)
    print(f"AVAILABLE SNAPSHOTS ({len(snapshots)} found)")
    print("=" * 96)
    if not snapshots:
        print("  No snapshots found in data/. Run 'make snapshot' to capture one.")
        return

    print(f"{'#':<4} {'Date & Time':<20} {'Host':<15} {'Format':<8} {'File Size':<11} {'Snapshot Path'}")
    print("-" * 96)
    for i, s in enumerate(snapshots, 1):
        size_str = f"{s['file_size_bytes'] / (1024*1024):.1f} MB"
        fmt_str = s.get("format", "raw")
        print(f"#{i:<3} {s['date']:<20} {s['host']:<15} {fmt_str:<8} {size_str:<11} {s['path']}")
    print("=" * 96)


def compress_existing_snapshots(base_dir: Path = Path("data"), compression: str = "zstd") -> List[Tuple[Path, Path, int, int]]:
    """
    Compress uncompressed .json snapshots in data/ to .json.zst (or .json.gz).
    Verifies decompressed header matches original before removing the raw file.
    """
    snapshots = discover_snapshots(base_dir)
    compressed_actions = []

    raw_snapshots = [s for s in snapshots if s["format"] == "raw"]
    if not raw_snapshots:
        print("No raw uncompressed snapshots found to compress.")
        return []

    print(f"Found {len(raw_snapshots)} raw uncompressed snapshot(s). Compressing to {compression}...")

    for s in raw_snapshots:
        orig_path = Path(s["path"])
        old_size = s["file_size_bytes"]
        ext = ".zst" if compression == "zstd" else ".gz"
        new_path = orig_path.with_name(orig_path.name + ext)
        tmp_path = orig_path.with_name(orig_path.name + ext + ".tmp")

        print(f"  Compressing {orig_path} ({old_size / (1024*1024):.1f} MB) -> {new_path.name}...")
        try:
            if compression == "zstd":
                subprocess.run(["zstd", "-3", "-f", "-o", str(tmp_path), str(orig_path)], check=True)
            else:
                with open(orig_path, "rb") as f_in, gzip.open(tmp_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            # Verify integrity
            verified_info = get_snapshot_info(tmp_path)
            if verified_info and verified_info["timestamp"] == s["timestamp"]:
                tmp_path.replace(new_path)
                orig_path.unlink()
                new_size = new_path.stat().st_size
                ratio = (1 - (new_size / old_size)) * 100.0 if old_size > 0 else 0
                print(f"    ✓ Compressed to {new_size / (1024*1024):.1f} MB ({ratio:.1f}% space saved)")
                compressed_actions.append((orig_path, new_path, old_size, new_size))
            else:
                print(f"    ✗ Verification failed for {tmp_path}. Original kept.")
                if tmp_path.exists():
                    tmp_path.unlink()
        except Exception as e:
            print(f"    ✗ Error compressing {orig_path}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    total_saved = sum(old - new for _, _, old, new in compressed_actions)
    print(f"Done! Compressed {len(compressed_actions)} snapshot(s), freed {total_saved / (1024*1024):.1f} MB.")
    return compressed_actions


def find_duckdb_binary() -> Optional[str]:
    """Find duckdb executable in system PATH or common macOS locations."""
    locations = [
        str(Path(__file__).resolve().parent / "bin" / "duckdb"),
        shutil.which("duckdb"),
        str(Path.home() / ".local" / "bin" / "duckdb"),
        "/opt/homebrew/bin/duckdb",
        "/usr/local/bin/duckdb",
    ]
    for loc in locations:
        if loc and os.path.isfile(loc) and os.access(loc, os.X_OK):
            return loc
    return None


def get_category_tag(path: str) -> str:
    """Pre-categorize paths for instant SQL grouping."""
    if "/node_modules" in path:
        return "node_modules"
    elif "/target" in path:
        return "target"
    elif "/.venv" in path or "/venv" in path:
        return "venv"
    elif "/Library/Caches" in path or "/.cache" in path:
        return "caches"
    elif "/Library/Logs" in path:
        return "logs"
    elif "/.git" in path:
        return "git"
    elif "/__pycache__" in path:
        return "pycache"
    return "other"


def export_snapshot_to_parquet(
    snapshot_path: Path,
    output_parquet: Optional[Path] = None,
    include_cloudstorage: bool = False,
) -> Optional[Path]:
    """
    Stream any snapshot (.json.zst, .json.gz, .json) into DuckDB to generate
    an optimized, ZSTD-compressed Parquet table with typed columns.
    Skips virtual/cloud placeholder files in ~/Library/CloudStorage (Drive, iCloud) by default.
    """
    duck_cmd = find_duckdb_binary()
    if not duck_cmd:
        print("Error: duckdb binary not found. Please install duckdb (e.g. brew install duckdb or in ~/.local/bin/duckdb).")
        return None

    snapshot_path = Path(snapshot_path)
    if not snapshot_path.is_file():
        print(f"Error: snapshot file {snapshot_path} does not exist.")
        return None

    if output_parquet is None:
        name = snapshot_path.name
        for ext in [".json.zst", ".json.gz", ".json", ".zst", ".gz"]:
            if name.endswith(ext):
                name = name[: -len(ext)]
                break
        output_parquet = snapshot_path.with_name(f"{name}.parquet")

    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    print(f"Converting snapshot {snapshot_path.name} -> {output_parquet.name} via DuckDB...")
    import time
    t0 = time.time()

    duck_sql = f"""
COPY (
    SELECT DISTINCT ON (path, is_dir)
        path::VARCHAR as path,
        name::VARCHAR as name,
        size_bytes::BIGINT as size_bytes,
        is_dir::BOOLEAN as is_dir,
        depth::INTEGER as depth,
        extension::VARCHAR as extension,
        category::VARCHAR as category
    FROM read_ndjson('/dev/stdin', columns={{
        'path':'VARCHAR',
        'name':'VARCHAR',
        'size_bytes':'BIGINT',
        'is_dir':'BOOLEAN',
        'depth':'INTEGER',
        'extension':'VARCHAR',
        'category':'VARCHAR'
    }})
) TO '{output_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD);
"""

    with open_snapshot_file(snapshot_path) as f_in:
        data = json.load(f_in)
    tree = data[3] if len(data) > 3 else []

    duck_proc = subprocess.Popen([duck_cmd, "-dark-mode", "-c", duck_sql], stdin=subprocess.PIPE)

    def walk_stream(node: List, parent: str = "", depth: int = 1):
        m = node[0]
        name = m.get("name", "")
        cur = normalize_path(f"{parent}/{name}" if parent else f"/{name}")

        # Skip virtual CloudStorage (Google Drive, iCloud, OneDrive) placeholders unless explicitly requested
        if not include_cloudstorage and is_excluded_path(cur):
            return

        dsize = m.get("dsize") or m.get("asize", 0)

        row = {
            "path": cur,
            "name": name,
            "size_bytes": dsize,
            "is_dir": True,
            "depth": depth,
            "extension": "",
            "category": get_category_tag(cur),
        }
        duck_proc.stdin.write(json.dumps(row).encode("utf-8") + b"\n")

        for child in node[1:]:
            if isinstance(child, dict):
                cname = child.get("name", "")
                cp = normalize_path(f"{cur}/{cname}")
                if not include_cloudstorage and is_excluded_path(cp):
                    continue
                cdsize = child.get("dsize") or child.get("asize", 0)
                idx = cname.rfind(".")
                ext = cname[idx:].lower() if idx > 0 and idx < len(cname) - 1 else ""
                row_f = {
                    "path": cp,
                    "name": cname,
                    "size_bytes": cdsize,
                    "is_dir": False,
                    "depth": depth + 1,
                    "extension": ext,
                    "category": get_category_tag(cp),
                }
                duck_proc.stdin.write(json.dumps(row_f).encode("utf-8") + b"\n")
            elif isinstance(child, list):
                walk_stream(child, cur, depth + 1)

    walk_stream(tree)
    duck_proc.stdin.close()
    duck_proc.wait()

    size_mb = output_parquet.stat().st_size / (1024 * 1024)
    print(f"✓ Created Parquet table in {time.time()-t0:.2f}s! ({size_mb:.1f} MB: {output_parquet})")
    return output_parquet


CATEGORY_LABELS = {
    "target": "Rust/Cargo build artifacts (target)",
    "node_modules": "Node.js dependencies (node_modules)",
    "venv": "Python virtual environments (venv)",
    "caches": "macOS application caches & .cache",
    "git": "Git repositories (.git)",
    "pycache": "Python bytecode cache (__pycache__)",
    "logs": "macOS log files",
}


def get_or_create_parquet(snap_info: Dict[str, Any], duck_cmd: str, include_cloudstorage: bool = False) -> Path:
    """Return an accessible Parquet path for the snapshot, converting on-the-fly if needed."""
    if snap_info.get("companion_parquet") and Path(snap_info["companion_parquet"]).is_file():
        return Path(snap_info["companion_parquet"])

    snap_path = Path(snap_info["path"])
    if snap_info.get("format") == "parquet" or snap_path.suffix == ".parquet":
        return snap_path

    # Check if companion parquet exists on disk
    base_name = strip_snapshot_ext(snap_path.name)
    parquet_path = snap_path.with_name(f"{base_name}.parquet")
    if parquet_path.is_file():
        snap_info["companion_parquet"] = parquet_path
        return parquet_path

    print(f"Converting snapshot {snap_path.name} to Parquet for fast DuckDB join...")
    res = export_snapshot_to_parquet(snap_path, parquet_path, include_cloudstorage=include_cloudstorage)
    if not res or not parquet_path.is_file():
        raise RuntimeError(f"Failed to export {snap_path} to Parquet format.")
    snap_info["companion_parquet"] = parquet_path
    return parquet_path


def compare_snapshots_duckdb(
    snap_before_info: Dict[str, Any],
    snap_after_info: Dict[str, Any],
    args: argparse.Namespace,
    duck_cmd: str,
):
    """
    Perform snapshot differential analysis via DuckDB SQL FULL OUTER JOIN.
    Sub-second query execution across millions of rows with minimal memory usage.
    """
    include_cs = getattr(args, "include_cloudstorage", False)
    parquet_before = get_or_create_parquet(snap_before_info, duck_cmd, include_cloudstorage=include_cs)
    parquet_after = get_or_create_parquet(snap_after_info, duck_cmd, include_cloudstorage=include_cs)

    # Resolve export directory
    d1 = snap_before_info["date"][:10].replace(" ", "_")
    d2 = snap_after_info["date"][:10].replace(" ", "_")
    export_dir = args.export_dir if args.export_dir else Path("data") / snap_after_info["host"] / "diffs" / f"{d1}_to_{d2}"

    cloud_filter = ""
    if not include_cs:
        cloud_filter = "WHERE COALESCE(b.path, a.path) NOT LIKE '%/Library/CloudStorage/%' AND COALESCE(b.path, a.path) NOT LIKE '%/Library/CloudStorage'"

    # Handle interactive diff shell if requested
    if getattr(args, "diff_shell", False):
        init_file = export_dir / "init_diff.sql" if args.export else Path(tempfile.gettempdir()) / f"duckdb_diff_{os.getpid()}.sql"
        init_file.parent.mkdir(parents=True, exist_ok=True)
        init_content = f"""
-- Preloaded Snapshot Diff View
-- Baseline [BEFORE]: {snap_before_info['date']} ({parquet_before})
-- Current  [AFTER] : {snap_after_info['date']} ({parquet_after})

CREATE OR REPLACE VIEW diff AS
SELECT 
    COALESCE(b.path, a.path) AS path,
    COALESCE(b.name, a.name) AS name,
    COALESCE(b.is_dir, a.is_dir) AS is_dir,
    COALESCE(b.category, a.category) AS category,
    COALESCE(b.extension, a.extension) AS extension,
    COALESCE(b.depth, a.depth) AS depth,
    COALESCE(b.size_bytes, 0) AS old_bytes,
    COALESCE(a.size_bytes, 0) AS new_bytes,
    COALESCE(a.size_bytes, 0) - COALESCE(b.size_bytes, 0) AS delta_bytes,
    CASE 
        WHEN b.path IS NULL THEN 'NEW'
        WHEN a.path IS NULL THEN 'DELETED'
        WHEN a.size_bytes > b.size_bytes THEN 'INCREASED'
        WHEN a.size_bytes < b.size_bytes THEN 'DECREASED'
        ELSE 'SAME'
    END AS status
FROM read_parquet('{parquet_before}') b
FULL OUTER JOIN read_parquet('{parquet_after}') a
  ON b.path = a.path AND b.is_dir = a.is_dir
{cloud_filter};

.print "================================================================================"
.print "🦆 DuckDB Snapshot Diff Shell"
.print "   Baseline: {snap_before_info['date']} | Current: {snap_after_info['date']}"
.print "================================================================================"
.print "Table view 'diff' is loaded! Schema:"
.print "  path, name, is_dir, category, extension, depth, old_bytes, new_bytes, delta_bytes, status"
.print ""
.print "Example queries:"
.print "  SELECT status, count(*), ROUND(SUM(delta_bytes)/1e9, 2) AS delta_gb FROM diff GROUP BY status;"
.print "  SELECT ROUND(delta_bytes/1e9, 2) AS gb, path FROM diff WHERE status = 'INCREASED' AND NOT is_dir ORDER BY delta_bytes DESC LIMIT 10;"
.print "  SELECT category, ROUND(SUM(delta_bytes)/1e9, 2) AS delta_gb FROM diff WHERE category != 'other' GROUP BY category;"
.print "================================================================================\\n"
"""
        init_file.write_text(init_content, encoding="utf-8")
        try:
            print(f"Launching interactive DuckDB shell with preloaded diff view...")
            subprocess.run([duck_cmd, "-dark-mode", "-init", str(init_file)])
        finally:
            if not args.export and init_file.exists():
                init_file.unlink()
        return

    min_size_bytes = parse_size_str(args.min_size)

    category_filter_sql = ""
    if args.category != "all":
        category_filter_sql = f"AND category = '{args.category}'"

    sql_parts = [f"""
CREATE TEMPORARY VIEW diff AS
SELECT 
    COALESCE(b.path, a.path) AS path,
    COALESCE(b.name, a.name) AS name,
    COALESCE(b.is_dir, a.is_dir) AS is_dir,
    COALESCE(b.category, a.category) AS category,
    COALESCE(b.extension, a.extension) AS extension,
    COALESCE(b.depth, a.depth) AS depth,
    COALESCE(b.size_bytes, 0) AS old_bytes,
    COALESCE(a.size_bytes, 0) AS new_bytes,
    COALESCE(a.size_bytes, 0) - COALESCE(b.size_bytes, 0) AS delta_bytes,
    CASE 
        WHEN b.path IS NULL THEN 'NEW'
        WHEN a.path IS NULL THEN 'DELETED'
        WHEN a.size_bytes > b.size_bytes THEN 'INCREASED'
        WHEN a.size_bytes < b.size_bytes THEN 'DECREASED'
        ELSE 'SAME'
    END AS status
FROM read_parquet('{parquet_before}') b
FULL OUTER JOIN read_parquet('{parquet_after}') a
  ON b.path = a.path AND b.is_dir = a.is_dir
{cloud_filter};
"""]

    # 1. Totals & status breakdown
    sql_parts.append("""
SELECT 'status_summary' AS _q, status, count(*) AS count, sum(delta_bytes) AS delta_bytes, sum(old_bytes) AS old_bytes, sum(new_bytes) AS new_bytes
FROM diff WHERE NOT is_dir GROUP BY status;
""")

    # 2. Root directory branches
    sql_parts.append("""
SELECT 'roots' AS _q, path, old_bytes, new_bytes, delta_bytes
FROM diff WHERE is_dir AND path IN ('/Users', '/Applications', '/Library', '/System', '/private', '/opt', '/usr', '/Volumes')
ORDER BY path;
""")

    # 3. Cleanable categories
    sql_parts.append(f"""
SELECT 'categories' AS _q, category, count(CASE WHEN status != 'SAME' THEN 1 END) AS changed_items, sum(old_bytes) AS old_bytes, sum(new_bytes) AS new_bytes, sum(delta_bytes) AS delta_bytes
FROM diff WHERE category != 'other' {category_filter_sql}
GROUP BY category ORDER BY ABS(sum(delta_bytes)) DESC;
""")

    # 4. Top growing files
    sql_parts.append(f"""
SELECT 'top_inc' AS _q, path, old_bytes, new_bytes, delta_bytes
FROM diff WHERE status = 'INCREASED' AND NOT is_dir AND delta_bytes >= {min_size_bytes}
ORDER BY delta_bytes DESC LIMIT {args.top};
""")

    # 5. Top shrinking files
    sql_parts.append(f"""
SELECT 'top_dec' AS _q, path, old_bytes, new_bytes, -delta_bytes AS freed_bytes
FROM diff WHERE status = 'DECREASED' AND NOT is_dir AND (-delta_bytes) >= {min_size_bytes}
ORDER BY delta_bytes ASC LIMIT {args.top};
""")

    # 6. Top new files
    sql_parts.append(f"""
SELECT 'top_new' AS _q, path, new_bytes
FROM diff WHERE status = 'NEW' AND NOT is_dir AND new_bytes >= {min_size_bytes}
ORDER BY new_bytes DESC LIMIT {args.top};
""")

    # 7. Top deleted files
    sql_parts.append(f"""
SELECT 'top_del' AS _q, path, old_bytes
FROM diff WHERE status = 'DELETED' AND NOT is_dir AND old_bytes >= {min_size_bytes}
ORDER BY old_bytes DESC LIMIT {args.top};
""")

    # 8. Top unchanged files
    sql_parts.append(f"""
SELECT 'top_same' AS _q, path, new_bytes
FROM diff WHERE status = 'SAME' AND NOT is_dir
ORDER BY new_bytes DESC LIMIT 10;
""")

    if args.export:
        export_dir.mkdir(parents=True, exist_ok=True)
        diff_parquet = export_dir / "diff.parquet"
        sql_parts.append(f"""
COPY (SELECT * FROM diff WHERE status != 'SAME') TO '{diff_parquet}' (FORMAT PARQUET, COMPRESSION ZSTD);
COPY (SELECT ROUND(delta_bytes/1024/1024/1024, 2), ROUND(old_bytes/1024/1024/1024, 2), ROUND(new_bytes/1024/1024/1024, 2), path FROM diff WHERE status = 'INCREASED' AND NOT is_dir ORDER BY delta_bytes DESC) TO '{export_dir}/diff_increased.txt' (HEADER FALSE, DELIMITER '\\t');
COPY (SELECT ROUND(-delta_bytes/1024/1024/1024, 2), ROUND(old_bytes/1024/1024/1024, 2), ROUND(new_bytes/1024/1024/1024, 2), path FROM diff WHERE status = 'DECREASED' AND NOT is_dir ORDER BY delta_bytes ASC) TO '{export_dir}/diff_decreased.txt' (HEADER FALSE, DELIMITER '\\t');
COPY (SELECT ROUND(new_bytes/1024/1024/1024, 2), path FROM diff WHERE status = 'NEW' AND NOT is_dir ORDER BY new_bytes DESC) TO '{export_dir}/diff_new.txt' (HEADER FALSE, DELIMITER '\\t');
COPY (SELECT ROUND(old_bytes/1024/1024/1024, 2), path FROM diff WHERE status = 'DELETED' AND NOT is_dir ORDER BY old_bytes DESC) TO '{export_dir}/diff_deleted.txt' (HEADER FALSE, DELIMITER '\\t');
COPY (SELECT ROUND(new_bytes/1024/1024/1024, 2), path FROM diff WHERE status = 'SAME' AND NOT is_dir ORDER BY new_bytes DESC) TO '{export_dir}/diff_same.txt' (HEADER FALSE, DELIMITER '\\t');
""")

    full_sql = "\n".join(sql_parts)

    proc = subprocess.run(
        [duck_cmd, "-dark-mode", "-json", "-c", full_sql],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(f"DuckDB error: {proc.stderr}")
        return

    # Parse JSON query results
    decoder = json.JSONDecoder()
    content = proc.stdout.strip()
    pos = 0
    parsed = []
    while pos < len(content):
        content_part = content[pos:].lstrip()
        if not content_part:
            break
        obj, idx = decoder.raw_decode(content_part)
        parsed.append(obj)
        pos += len(content[pos:]) - len(content_part) + idx

    queries_by_name = {}
    for res_list in parsed:
        if res_list and isinstance(res_list, list) and "_q" in res_list[0]:
            queries_by_name[res_list[0]["_q"]] = res_list

    status_summary = queries_by_name.get("status_summary", [])
    roots = queries_by_name.get("roots", [])
    categories = queries_by_name.get("categories", [])
    top_inc = queries_by_name.get("top_inc", [])
    top_dec = queries_by_name.get("top_dec", [])
    top_new = queries_by_name.get("top_new", [])
    top_del = queries_by_name.get("top_del", [])
    top_same = queries_by_name.get("top_same", [])

    def to_int(v) -> int:
        if v is None:
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    # Calculate overall aggregates
    tot_before = sum(to_int(row.get("old_bytes", 0)) for row in status_summary)
    tot_after = sum(to_int(row.get("new_bytes", 0)) for row in status_summary)
    tot_delta = sum(to_int(row.get("delta_bytes", 0)) for row in status_summary)

    counts_by_status = {r["status"]: to_int(r.get("count", 0)) for r in status_summary}
    sizes_by_status = {
        r["status"]: to_int(r.get("new_bytes", 0)) if r["status"] in ("NEW", "SAME") else to_int(r.get("old_bytes", 0))
        for r in status_summary
    }
    deltas_by_status = {r["status"]: to_int(r.get("delta_bytes", 0)) for r in status_summary}

    files_before_count = sum(to_int(r.get("count", 0)) for r in status_summary if r["status"] in ("SAME", "DECREASED", "INCREASED", "DELETED"))
    files_after_count = sum(to_int(r.get("count", 0)) for r in status_summary if r["status"] in ("SAME", "DECREASED", "INCREASED", "NEW"))

    if args.json:
        summary_obj = {
            "engine": "duckdb",
            "before": {
                "date": snap_before_info["date"],
                "path": str(snap_before_info["path"]),
                "format": snap_before_info.get("format", "raw"),
                "total_bytes": tot_before,
                "file_count": files_before_count,
            },
            "after": {
                "date": snap_after_info["date"],
                "path": str(snap_after_info["path"]),
                "format": snap_after_info.get("format", "raw"),
                "total_bytes": tot_after,
                "file_count": files_after_count,
            },
            "net_delta_bytes": tot_delta,
            "counts": {
                "new_files": counts_by_status.get("NEW", 0),
                "deleted_files": counts_by_status.get("DELETED", 0),
                "increased_files": counts_by_status.get("INCREASED", 0),
                "decreased_files": counts_by_status.get("DECREASED", 0),
                "same_files": counts_by_status.get("SAME", 0),
            },
            "sizes": {
                "new_bytes": sizes_by_status.get("NEW", 0),
                "deleted_bytes": sizes_by_status.get("DELETED", 0),
                "increased_delta_bytes": deltas_by_status.get("INCREASED", 0),
                "decreased_delta_bytes": deltas_by_status.get("DECREASED", 0),
                "same_bytes": sizes_by_status.get("SAME", 0),
            },
        }
        print(json.dumps(summary_obj, indent=2))
        return

    # Banner
    fmt_b = snap_before_info.get("format", "raw")
    fmt_a = snap_after_info.get("format", "raw")
    print("=" * 88)
    print("DISK USAGE SNAPSHOT COMPARISON (DuckDB Engine)")
    print("=" * 88)
    print(f"  [BEFORE] Baseline : {snap_before_info['date']} ({fmt_b})  ({snap_before_info['path']})")
    print(f"  [AFTER]  Current  : {snap_after_info['date']} ({fmt_a})  ({snap_after_info['path']})")
    print("=" * 88)

    # Executive Summary
    print("\n" + "=" * 88)
    print("EXECUTIVE SUMMARY: DISK USAGE DELTA")
    print("=" * 88)

    pct_change = (tot_delta / tot_before * 100.0) if tot_before > 0 else 0.0
    sign_str = "+" if tot_delta >= 0 else ""
    print(f"Total Disk Usage [BEFORE] : {format_size(tot_before):>12}  ({files_before_count:,} files)")
    print(f"Total Disk Usage [AFTER]  : {format_size(tot_after):>12}  ({files_after_count:,} files)")
    print(f"Net Space Change          : {format_size(tot_delta, show_sign=True):>12}  ({sign_str}{pct_change:.2f}%)")
    print("-" * 88)

    print(f"{'Category':<22} | {'Count':>12} | {'Total Size / Delta':>18} | {'Description'}")
    print("-" * 88)
    print(f"🆕  {'New Files':<18} | {counts_by_status.get('NEW', 0):>12,} | {format_size(sizes_by_status.get('NEW', 0), show_sign=True):>18} | Created since baseline")
    print(f"🗑️   {'Deleted Files':<18} | {counts_by_status.get('DELETED', 0):>12,} | {format_size(-sizes_by_status.get('DELETED', 0), show_sign=True):>18} | Freed up since baseline")
    print(f"📈  {'Increased Files':<18} | {counts_by_status.get('INCREASED', 0):>12,} | {format_size(deltas_by_status.get('INCREASED', 0), show_sign=True):>18} | Grew in size (+Δ)")
    print(f"📉  {'Decreased Files':<18} | {counts_by_status.get('DECREASED', 0):>12,} | {format_size(deltas_by_status.get('DECREASED', 0), show_sign=True):>18} | Shrank in size (-Δ)")
    print(f"⚖️   {'Same (Unchanged)':<18} | {counts_by_status.get('SAME', 0):>12,} | {format_size(sizes_by_status.get('SAME', 0)):>18} | Identical size & path")
    print("=" * 88)

    # 1. High-Level Mounts / Root Directory Comparison
    print("\n" + "=" * 88)
    print("HIGH-LEVEL ROOT DIRECTORY CHANGES")
    print("=" * 88)
    print(f"{'Directory':<24} | {'Before':>11} | {'After':>11} | {'Delta':>11} | {'Status'}")
    print("-" * 88)
    for row in roots:
        rb = row.get("path", "")
        s1 = to_int(row.get("old_bytes", 0))
        s2 = to_int(row.get("new_bytes", 0))
        diff = to_int(row.get("delta_bytes", 0))
        if s1 == 0 and s2 == 0:
            continue
        status = "SAME"
        if s1 == 0:
            status = "NEW"
        elif s2 == 0:
            status = "DELETED"
        elif diff > 0:
            status = f"+{(diff/s1*100.0) if s1 else 0:.1f}%"
        elif diff < 0:
            status = f"{(diff/s1*100.0) if s1 else 0:.1f}%"
        print(f"{rb:<24} | {format_size(s1):>11} | {format_size(s2):>11} | {format_size(diff, show_sign=True):>11} | {status}")

    # 2. Cleanable Categories Comparison
    print("\n" + "=" * 88)
    print("CLEANABLE CATEGORIES COMPARISON")
    print("=" * 88)
    print(f"{'Category':<32} | {'Before':>10} | {'After':>10} | {'Delta':>11} | {'Status':>15}")
    print("-" * 88)
    for row in categories:
        cat = row.get("category", "")
        desc = CATEGORY_LABELS.get(cat, cat)
        changed = to_int(row.get("changed_items", 0))
        s1 = to_int(row.get("old_bytes", 0))
        s2 = to_int(row.get("new_bytes", 0))
        diff = to_int(row.get("delta_bytes", 0))
        count_str = f"{changed:,} changed" if changed > 0 else "unchanged"
        print(f"{desc[:32]:<32} | {format_size(s1):>10} | {format_size(s2):>10} | {format_size(diff, show_sign=True):>11} | {count_str:>15}")

    # 3. Top Growing Files
    top_limit = min(args.top, len(top_inc))
    print("\n" + "=" * 88)
    print(f"TOP {top_limit} GROWING FILES (INCREASED IN SIZE)")
    print("=" * 88)
    if not top_inc:
        print("  (No files increased by >= minimum size threshold)")
    else:
        print(f"{'+Delta':>10} | {'Old Size':>9} -> {'New Size':>9} | Path")
        print("-" * 88)
        for row in top_inc:
            p = row.get("path", "")
            s_old = to_int(row.get("old_bytes", 0))
            s_new = to_int(row.get("new_bytes", 0))
            diff = to_int(row.get("delta_bytes", 0))
            print(f"{format_size(diff, show_sign=True):>10} | {format_size(s_old):>9} -> {format_size(s_new):>9} | {p}")

    # 4. Top Shrinking Files
    top_dec_limit = min(args.top, len(top_dec))
    print("\n" + "=" * 88)
    print(f"TOP {top_dec_limit} SHRINKING FILES (DECREASED IN SIZE)")
    print("=" * 88)
    if not top_dec:
        print("  (No files decreased by >= minimum size threshold)")
    else:
        print(f"{'-Delta':>10} | {'Old Size':>9} -> {'New Size':>9} | Path")
        print("-" * 88)
        for row in top_dec:
            p = row.get("path", "")
            s_old = to_int(row.get("old_bytes", 0))
            s_new = to_int(row.get("new_bytes", 0))
            freed = to_int(row.get("freed_bytes", 0))
            print(f"{format_size(-freed, show_sign=True):>10} | {format_size(s_old):>9} -> {format_size(s_new):>9} | {p}")

    # 5. Top New Files
    top_new_limit = min(args.top, len(top_new))
    print("\n" + "=" * 88)
    print(f"TOP {top_new_limit} NEW FILES (CREATED SINCE BASELINE)")
    print("=" * 88)
    if not top_new:
        print("  (No new files >= minimum size threshold)")
    else:
        print(f"{'Size':>10} | Path")
        print("-" * 88)
        for row in top_new:
            p = row.get("path", "")
            s = to_int(row.get("new_bytes", 0))
            print(f"{format_size(s):>10} | {p}")

    # 6. Top Deleted Files
    top_del_limit = min(args.top, len(top_del))
    print("\n" + "=" * 88)
    print(f"TOP {top_del_limit} DELETED FILES (FREED UP SINCE BASELINE)")
    print("=" * 88)
    if not top_del:
        print("  (No deleted files >= minimum size threshold)")
    else:
        print(f"{'Size':>10} | Path")
        print("-" * 88)
        for row in top_del:
            p = row.get("path", "")
            s = to_int(row.get("old_bytes", 0))
            print(f"{format_size(s):>10} | {p}")

    # 7. Top Unchanged Files
    print("\n" + "=" * 88)
    print(f"TOP {min(10, len(top_same))} UNCHANGED (SAME) FILES")
    print("=" * 88)
    print(f"{'Size':>10} | Path")
    print("-" * 88)
    for row in top_same:
        p = row.get("path", "")
        s = to_int(row.get("new_bytes", 0))
        print(f"{format_size(s):>10} | {p}")

    # 8. Export status
    if args.export:
        print("\n" + "=" * 88)
        print("EXPORTED DIFFERENTIAL REPORTS")
        print("=" * 88)
        print(f"✓ Diff reports saved to: {export_dir}/")
        print(f"  • diff.parquet       : Compressed Parquet table (queryable via DuckDB)")
        print(f"  • diff_increased.txt : Files that grew (+Δ)")
        print(f"  • diff_decreased.txt : Files that shrank (-Δ)")
        print(f"  • diff_new.txt       : Newly created files")
        print(f"  • diff_deleted.txt   : Deleted files")
        print(f"  • diff_same.txt      : Unchanged files")


def resolve_snapshot_spec(spec: str, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Resolve a snapshot argument which could be:
    - An index like '1' or '#1'
    - A date or substring like '2026-03-30'
    - A direct file path
    """
    spec_clean = spec.strip()

    # 1. Direct path check
    direct_path = Path(spec_clean)
    if direct_path.is_file():
        info = get_snapshot_info(direct_path)
        if info:
            return info

    # 2. Check if integer index (1-based)
    if spec_clean.startswith("#"):
        spec_clean = spec_clean[1:]
    if spec_clean.isdigit():
        idx = int(spec_clean)
        if 1 <= idx <= len(snapshots):
            return snapshots[idx - 1]

    # 3. Match against date, filename, or path substring
    matches = []
    for s in snapshots:
        if spec.lower() in s["date"].lower() or spec.lower() in str(s["path"]).lower():
            matches.append(s)

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        return matches[-1]

    return None


def load_snapshot(
    file_path: Path,
    include_cloudstorage: bool = False,
    desc: str = "Loading snapshot",
) -> Tuple[Dict[str, Any], Dict[str, int], Dict[str, int]]:
    """
    Load an ncdu snapshot (compressed or raw) and parse file sizes and recursive directory sizes.
    Returns: (metadata, file_sizes, dir_sizes)
    """
    with open_snapshot_file(file_path) as f:
        data = json.load(f)

    meta = data[2] if len(data) > 2 else {}
    tree = data[3] if len(data) > 3 else []

    file_sizes: Dict[str, int] = {}
    dir_sizes: Dict[str, int] = {}

    def walk(node: List, parent: str = "") -> int:
        if not node:
            return 0
        metadata = node[0]
        name = metadata.get("name", "")
        cur = f"{parent}/{name}" if parent else f"/{name}"
        n_cur = normalize_path(cur)
        if not include_cloudstorage and is_excluded_path(n_cur):
            return 0

        tot = 0
        for child in node[1:]:
            if isinstance(child, dict):
                cname = child.get("name", "")
                cp = normalize_path(f"{cur}/{cname}")
                if not include_cloudstorage and is_excluded_path(cp):
                    continue
                dsize = child.get("dsize") or child.get("asize", 0)
                file_sizes[cp] = dsize
                tot += dsize
            elif isinstance(child, list):
                tot += walk(child, cur)

        dir_sizes[n_cur] = tot
        return tot

    walk(tree)

    # Free the large parsed tree to minimize peak memory
    del data, tree
    gc.collect()

    return meta, file_sizes, dir_sizes


def get_root_independent_dirs(dir_dict: Dict[str, int]) -> List[str]:
    """Filter directory paths to only include top-level non-overlapping roots."""
    sorted_paths = sorted(dir_dict.keys())
    roots = []
    for p in sorted_paths:
        prefix = p + "/"
        if not any(p != r and p.startswith(r + "/") for r in roots):
            roots.append(p)
    return roots


def main():
    parser = argparse.ArgumentParser(
        description="Compare disk usage between two snapshots to see size changes, new files, and same files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available snapshots and exit",
    )
    parser.add_argument(
        "--compress-all",
        action="store_true",
        help="Compress all raw .json snapshots to .json.zst and free disk space",
    )
    parser.add_argument(
        "--to-parquet",
        nargs="?",
        const="latest",
        help="Export a snapshot (.json.zst) to Parquet format via DuckDB (specify path, index, or default: latest)",
    )
    parser.add_argument(
        "--before",
        "-b",
        help="Baseline snapshot: index (#1), date (YYYY-MM-DD), or file path. Default: 2nd most recent.",
    )
    parser.add_argument(
        "--after",
        "-a",
        help="Current snapshot: index (#2), date (YYYY-MM-DD), or file path. Default: most recent.",
    )
    parser.add_argument(
        "--host",
        help="Filter snapshots by hostname (e.g., VietVu-02)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of items to show in top lists (default: 20)",
    )
    parser.add_argument(
        "--min-size",
        default="50MB",
        help="Minimum file/directory size or delta to show (e.g. 50MB, 1GB; default: 50MB)",
    )
    parser.add_argument(
        "--category",
        choices=["all", "node_modules", "target", "venv", "caches", "git", "logs"],
        default="all",
        help="Focus on a specific category of cleanable directories",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export detailed diff text files (diff_increased.txt, diff_decreased.txt, etc.)",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="Directory to save exported diff files (default: data/<host>/diffs/<date1>_to_<date2>/)",
    )
    parser.add_argument(
        "--include-cloudstorage",
        action="store_true",
        help="Include ~/Library/CloudStorage (Drive/iCloud placeholders)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output comparison summary as JSON",
    )
    parser.add_argument(
        "--engine",
        choices=["auto", "duckdb", "python"],
        default="auto",
        help="Comparison engine: 'duckdb' (sub-second SQL join), 'python' (in-memory tree diff), or 'auto' (default: duckdb if available)",
    )
    parser.add_argument(
        "--diff-shell",
        action="store_true",
        help="Launch an interactive DuckDB SQL shell on the diff view between snapshots",
    )

    args = parser.parse_args()

    if args.compress_all:
        compress_existing_snapshots(Path("data"))
        return

    if args.to_parquet:
        snapshots = discover_snapshots(Path("data"), host=args.host)
        target_info = None
        if args.to_parquet == "latest":
            if not snapshots:
                print("Error: No snapshots found to convert.")
                return
            target_info = snapshots[-1]
        else:
            target_info = resolve_snapshot_spec(args.to_parquet, snapshots)

        if not target_info:
            print(f"Error: Could not find snapshot '{args.to_parquet}'.")
            return
        export_snapshot_to_parquet(target_info["path"])
        return

    min_size_bytes = parse_size_str(args.min_size)

    # Discover snapshots
    snapshots = discover_snapshots(Path("data"), host=args.host)

    if args.list:
        list_snapshots(snapshots)
        return

    if len(snapshots) < 2 and not (args.before and args.after):
        list_snapshots(snapshots)
        print("\nError: At least two snapshots are required for comparison.")
        print("Run 'make snapshot' to capture a new compressed snapshot.")
        sys.exit(1)

    # Resolve before & after
    snap_before_info = None
    snap_after_info = None

    if args.before:
        snap_before_info = resolve_snapshot_spec(args.before, snapshots)
        if not snap_before_info:
            print(f"Error: Could not resolve '--before {args.before}'. Run with --list to see available snapshots.")
            sys.exit(1)
    else:
        snap_before_info = snapshots[-2]

    if args.after:
        snap_after_info = resolve_snapshot_spec(args.after, snapshots)
        if not snap_after_info:
            print(f"Error: Could not resolve '--after {args.after}'. Run with --list to see available snapshots.")
            sys.exit(1)
    else:
        snap_after_info = snapshots[-1]

    if snap_before_info["path"] == snap_after_info["path"]:
        print("Warning: Both before and after point to the same snapshot file:")
        print(f"  {snap_before_info['path']}")
        print("Comparing identical snapshots will show 0 delta and 100% same.")

    fmt_b = snap_before_info.get("format", "raw")
    fmt_a = snap_after_info.get("format", "raw")

    duck_cmd = find_duckdb_binary()
    use_duckdb = False
    if getattr(args, "diff_shell", False):
        use_duckdb = True
        if not duck_cmd:
            print("Error: duckdb binary is required for --diff-shell. Please install duckdb.")
            sys.exit(1)
    elif args.engine == "duckdb":
        use_duckdb = True
        if not duck_cmd:
            print("Error: duckdb binary not found. Install duckdb or use '--engine python'.")
            sys.exit(1)
    elif args.engine == "auto" and duck_cmd:
        use_duckdb = True

    if use_duckdb:
        compare_snapshots_duckdb(snap_before_info, snap_after_info, args, duck_cmd)
        return

    print("=" * 88)
    print("DISK USAGE SNAPSHOT COMPARISON")
    print("=" * 88)
    print(f"  [BEFORE] Baseline : {snap_before_info['date']} ({fmt_b})  ({snap_before_info['path']})")
    print(f"  [AFTER]  Current  : {snap_after_info['date']} ({fmt_a})  ({snap_after_info['path']})")
    print("=" * 88)

    # Load snapshots
    print("\nLoading baseline snapshot [BEFORE]...")
    meta_before, files_before, dirs_before = load_snapshot(
        snap_before_info["path"],
        include_cloudstorage=args.include_cloudstorage,
        desc="Loading BEFORE snapshot",
    )
    print(f"  Loaded {len(files_before):,} files and {len(dirs_before):,} directories")

    print("\nLoading current snapshot [AFTER]...")
    meta_after, files_after, dirs_after = load_snapshot(
        snap_after_info["path"],
        include_cloudstorage=args.include_cloudstorage,
        desc="Loading AFTER snapshot",
    )
    print(f"  Loaded {len(files_after):,} files and {len(dirs_after):,} directories")

    # Comparisons
    print("\nComputing diffs between snapshots...")

    tot_before = sum(files_before.values())
    tot_after = sum(files_after.values())
    tot_delta = tot_after - tot_before

    keys_before = set(files_before.keys())
    keys_after = set(files_after.keys())

    new_keys = keys_after - keys_before
    del_keys = keys_before - keys_after
    common_keys = keys_before & keys_after

    inc_files: List[Tuple[str, int, int, int]] = []
    dec_files: List[Tuple[str, int, int, int]] = []
    same_files: List[Tuple[str, int]] = []

    for k in common_keys:
        s_old = files_before[k]
        s_new = files_after[k]
        if s_new > s_old:
            inc_files.append((k, s_old, s_new, s_new - s_old))
        elif s_new < s_old:
            dec_files.append((k, s_old, s_new, s_old - s_new))
        else:
            same_files.append((k, s_new))

    inc_files.sort(key=lambda x: x[3], reverse=True)
    dec_files.sort(key=lambda x: x[3], reverse=True)
    same_files.sort(key=lambda x: x[1], reverse=True)

    new_files_list = [(k, files_after[k]) for k in new_keys]
    new_files_list.sort(key=lambda x: x[1], reverse=True)

    del_files_list = [(k, files_before[k]) for k in del_keys]
    del_files_list.sort(key=lambda x: x[1], reverse=True)

    tot_new_bytes = sum(s for _, s in new_files_list)
    tot_del_bytes = sum(s for _, s in del_files_list)
    tot_inc_bytes = sum(diff for _, _, _, diff in inc_files)
    tot_dec_bytes = sum(diff for _, _, _, diff in dec_files)
    tot_same_bytes = sum(s for _, s in same_files)

    # JSON output if requested
    if args.json:
        summary_obj = {
            "before": {
                "date": snap_before_info["date"],
                "path": str(snap_before_info["path"]),
                "format": fmt_b,
                "total_bytes": tot_before,
                "file_count": len(files_before),
            },
            "after": {
                "date": snap_after_info["date"],
                "path": str(snap_after_info["path"]),
                "format": fmt_a,
                "total_bytes": tot_after,
                "file_count": len(files_after),
            },
            "net_delta_bytes": tot_delta,
            "counts": {
                "new_files": len(new_keys),
                "deleted_files": len(del_keys),
                "increased_files": len(inc_files),
                "decreased_files": len(dec_files),
                "same_files": len(same_files),
            },
            "sizes": {
                "new_bytes": tot_new_bytes,
                "deleted_bytes": tot_del_bytes,
                "increased_delta_bytes": tot_inc_bytes,
                "decreased_delta_bytes": tot_dec_bytes,
                "same_bytes": tot_same_bytes,
            },
        }
        print(json.dumps(summary_obj, indent=2))
        return

    # Print Summary Table
    print("\n" + "=" * 88)
    print("EXECUTIVE SUMMARY: DISK USAGE DELTA")
    print("=" * 88)

    pct_change = (tot_delta / tot_before * 100.0) if tot_before > 0 else 0.0
    sign_str = "+" if tot_delta >= 0 else ""
    print(f"Total Disk Usage [BEFORE] : {format_size(tot_before):>12}  ({len(files_before):,} files)")
    print(f"Total Disk Usage [AFTER]  : {format_size(tot_after):>12}  ({len(files_after):,} files)")
    print(f"Net Space Change          : {format_size(tot_delta, show_sign=True):>12}  ({sign_str}{pct_change:.2f}%)")
    print("-" * 88)

    print(f"{'Category':<22} | {'Count':>12} | {'Total Size / Delta':>18} | {'Description'}")
    print("-" * 88)
    print(f"🆕  {'New Files':<18} | {len(new_keys):>12,} | {format_size(tot_new_bytes, show_sign=True):>18} | Created since baseline")
    print(f"🗑️   {'Deleted Files':<18} | {len(del_keys):>12,} | {format_size(-tot_del_bytes, show_sign=True):>18} | Freed up since baseline")
    print(f"📈  {'Increased Files':<18} | {len(inc_files):>12,} | {format_size(tot_inc_bytes, show_sign=True):>18} | Grew in size (+Δ)")
    print(f"📉  {'Decreased Files':<18} | {len(dec_files):>12,} | {format_size(-tot_dec_bytes, show_sign=True):>18} | Shrank in size (-Δ)")
    print(f"⚖️   {'Same (Unchanged)':<18} | {len(same_files):>12,} | {format_size(tot_same_bytes):>18} | Identical size & path")
    print("=" * 88)

    # 1. High-Level Mounts / Root Directory Comparison
    print("\n" + "=" * 88)
    print("HIGH-LEVEL ROOT DIRECTORY CHANGES")
    print("=" * 88)
    root_branches = [
        "/Users",
        "/Applications",
        "/Library",
        "/System",
        "/private",
        "/opt",
        "/usr",
        "/Volumes",
    ]
    print(f"{'Directory':<24} | {'Before':>11} | {'After':>11} | {'Delta':>11} | {'Status'}")
    print("-" * 88)
    for rb in root_branches:
        s1 = dirs_before.get(rb, 0)
        s2 = dirs_after.get(rb, 0)
        diff = s2 - s1
        if s1 == 0 and s2 == 0:
            continue
        status = "SAME"
        if s1 == 0:
            status = "NEW"
        elif s2 == 0:
            status = "DELETED"
        elif diff > 0:
            status = f"+{(diff/s1*100.0) if s1 else 0:.1f}%"
        elif diff < 0:
            status = f"{(diff/s1*100.0) if s1 else 0:.1f}%"

        print(f"{rb:<24} | {format_size(s1):>11} | {format_size(s2):>11} | {format_size(diff, show_sign=True):>11} | {status}")

    # 2. Cleanable Categories Comparison
    print("\n" + "=" * 88)
    print("CLEANABLE CATEGORIES COMPARISON")
    print("=" * 88)
    all_patterns = [
        ("/node_modules", "Node.js dependencies (node_modules)", "node_modules"),
        ("/.venv", "Python virtual environments (.venv)", "venv"),
        ("/venv", "Python virtual environments (venv)", "venv"),
        ("/target", "Rust/Cargo build artifacts (target)", "target"),
        ("/.m2", "Maven cache (.m2)", "caches"),
        ("/.gradle", "Gradle cache (.gradle)", "caches"),
        ("/.npm", "NPM cache (.npm)", "caches"),
        ("/__pycache__", "Python bytecode cache (__pycache__)", "caches"),
        ("/Library/Caches", "macOS application caches", "caches"),
        ("/Library/Logs", "macOS log files", "logs"),
        ("/.cache", "General cache directories (.cache)", "caches"),
        ("/.git", "Git repositories (.git)", "git"),
    ]

    selected_patterns = all_patterns
    if args.category != "all":
        selected_patterns = [p for p in all_patterns if p[2] == args.category]

    print(f"{'Category':<32} | {'Before':>10} | {'After':>10} | {'Delta':>11} | {'Dirs (Old->New)'}")
    print("-" * 88)
    for pat, desc, _ in selected_patterns:
        roots1 = {p: s for p, s in dirs_before.items() if p.endswith(pat) and p.count(pat) == 1}
        roots2 = {p: s for p, s in dirs_after.items() if p.endswith(pat) and p.count(pat) == 1}
        s1 = sum(roots1.values())
        s2 = sum(roots2.values())
        diff = s2 - s1
        count_str = f"{len(roots1)} -> {len(roots2)}"
        print(f"{desc[:32]:<32} | {format_size(s1):>10} | {format_size(s2):>10} | {format_size(diff, show_sign=True):>11} | {count_str:>15}")

    # 3. Top Growing Files (increased in size)
    print("\n" + "=" * 88)
    print(f"TOP {args.top} GROWING FILES (INCREASED IN SIZE)")
    print("=" * 88)
    shown_inc = 0
    for p, s_old, s_new, diff in inc_files:
        if diff < min_size_bytes and shown_inc >= 5:
            break
        print(f"{format_size(diff, show_sign=True):>11}  ({format_size(s_old).strip()} -> {format_size(s_new).strip()})  {p}")
        shown_inc += 1
        if shown_inc >= args.top:
            break
    if shown_inc == 0:
        print("  No files increased by more than threshold.")

    # 4. Top Shrinking Files (decreased in size)
    print("\n" + "=" * 88)
    print(f"TOP {args.top} SHRINKING FILES (DECREASED IN SIZE)")
    print("=" * 88)
    shown_dec = 0
    for p, s_old, s_new, diff in dec_files:
        if diff < min_size_bytes and shown_dec >= 5:
            break
        print(f"{format_size(-diff, show_sign=True):>11}  ({format_size(s_old).strip()} -> {format_size(s_new).strip()})  {p}")
        shown_dec += 1
        if shown_dec >= args.top:
            break
    if shown_dec == 0:
        print("  No files decreased by more than threshold.")

    # 5. Top New Files
    print("\n" + "=" * 88)
    print(f"TOP {args.top} NEW FILES (CREATED)")
    print("=" * 88)
    shown_new = 0
    for p, s in new_files_list:
        if s < min_size_bytes and shown_new >= 5:
            break
        print(f"{format_size(s):>11}  {p}")
        shown_new += 1
        if shown_new >= args.top:
            break
    if shown_new == 0:
        print("  No new files larger than threshold.")

    # 6. Top Deleted Files
    print("\n" + "=" * 88)
    print(f"TOP {args.top} DELETED FILES (REMOVED)")
    print("=" * 88)
    shown_del = 0
    for p, s in del_files_list:
        if s < min_size_bytes and shown_del >= 5:
            break
        print(f"{format_size(s):>11}  {p}")
        shown_del += 1
        if shown_del >= args.top:
            break
    if shown_del == 0:
        print("  No deleted files larger than threshold.")

    # 7. Top New & Deleted Independent Directories
    print("\n" + "=" * 88)
    print("TOP NEW & DELETED INDEPENDENT DIRECTORIES")
    print("=" * 88)

    new_dirs = {k: dirs_after[k] for k in dirs_after if k not in dirs_before and dirs_after[k] >= min_size_bytes}
    root_new_dirs = get_root_independent_dirs(new_dirs)
    root_new_dirs_sorted = sorted([(k, new_dirs[k]) for k in root_new_dirs], key=lambda x: x[1], reverse=True)[:args.top]

    print("Newly Created Directories:")
    if root_new_dirs_sorted:
        for p, s in root_new_dirs_sorted:
            print(f"  +{format_size(s).strip():>9}  {p}")
    else:
        print("  None above threshold.")

    del_dirs = {k: dirs_before[k] for k in dirs_before if k not in dirs_after and dirs_before[k] >= min_size_bytes}
    root_del_dirs = get_root_independent_dirs(del_dirs)
    root_del_dirs_sorted = sorted([(k, del_dirs[k]) for k in root_del_dirs], key=lambda x: x[1], reverse=True)[:args.top]

    print("\nDeleted Directories:")
    if root_del_dirs_sorted:
        for p, s in root_del_dirs_sorted:
            print(f"  -{format_size(s).strip():>9}  {p}")
    else:
        print("  None above threshold.")

    # 8. Top Unchanged (Same) Large Files
    print("\n" + "=" * 88)
    print(f"TOP {min(10, args.top)} LARGEST UNCHANGED (SAME) FILES")
    print("=" * 88)
    for p, s in same_files[: min(10, args.top)]:
        print(f"{format_size(s):>11}  {p}")

    # Exporting detailed diff files if requested
    if args.export:
        date_str1 = snap_before_info["date"][:10]
        date_str2 = snap_after_info["date"][:10]
        host_target = snap_after_info["host"] or "default"
        export_path = args.export_dir or (Path("data") / host_target / "diffs" / f"{date_str1}_to_{date_str2}")
        export_path.mkdir(parents=True, exist_ok=True)

        print("\n" + "=" * 88)
        print(f"EXPORTING DETAILED DIFFS TO: {export_path}/")
        print("=" * 88)

        # Export increased
        inc_file_path = export_path / "diff_increased.txt"
        with open(inc_file_path, "w", encoding="utf-8") as f:
            for p, s_old, s_new, diff in inc_files:
                diff_gb = diff / (1024 * 1024 * 1024)
                old_gb = s_old / (1024 * 1024 * 1024)
                new_gb = s_new / (1024 * 1024 * 1024)
                f.write(f"{diff_gb:+.4f}\t{old_gb:.4f}\t{new_gb:.4f}\t{p}\n")
        print(f"  → Exported {len(inc_files):,} increased files to: {inc_file_path}")

        # Export decreased
        dec_file_path = export_path / "diff_decreased.txt"
        with open(dec_file_path, "w", encoding="utf-8") as f:
            for p, s_old, s_new, diff in dec_files:
                diff_gb = -diff / (1024 * 1024 * 1024)
                old_gb = s_old / (1024 * 1024 * 1024)
                new_gb = s_new / (1024 * 1024 * 1024)
                f.write(f"{diff_gb:+.4f}\t{old_gb:.4f}\t{new_gb:.4f}\t{p}\n")
        print(f"  → Exported {len(dec_files):,} decreased files to: {dec_file_path}")

        # Export new
        new_file_path = export_path / "diff_new.txt"
        with open(new_file_path, "w", encoding="utf-8") as f:
            for p, s in new_files_list:
                s_gb = s / (1024 * 1024 * 1024)
                f.write(f"{s_gb:.4f}\t{p}\n")
        print(f"  → Exported {len(new_files_list):,} new files to: {new_file_path}")

        # Export deleted
        del_file_path = export_path / "diff_deleted.txt"
        with open(del_file_path, "w", encoding="utf-8") as f:
            for p, s in del_files_list:
                s_gb = s / (1024 * 1024 * 1024)
                f.write(f"{s_gb:.4f}\t{p}\n")
        print(f"  → Exported {len(del_files_list):,} deleted files to: {del_file_path}")

        # Export same
        same_file_path = export_path / "diff_same.txt"
        with open(same_file_path, "w", encoding="utf-8") as f:
            for p, s in same_files:
                s_gb = s / (1024 * 1024 * 1024)
                f.write(f"{s_gb:.4f}\t{p}\n")
        print(f"  → Exported {len(same_files):,} unchanged files to: {same_file_path}")

    print("\nComparison complete!")


if __name__ == "__main__":
    main()
