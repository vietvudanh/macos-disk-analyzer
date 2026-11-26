# Benchmark: Go vs. Rust vs. Python (`analyze-ncdu`)

## v2 (2026-11-20) — Fixed `dsize`, `hlnkc`/`ino` Hardlink Dedup & Directory Nodes

Measured with macOS `/usr/bin/time -l` (release binaries pre-built) on host `VietVu-Macbook` (`darwin/arm64`) against `data/VietVu-Macbook/disk_usage.json.zst` (`snapshots/disk_usage_2026-11-19_165630.json.zst`):

- **Snapshot**: 38.3 MiB `.json.zst` (281.3 MiB uncompressed JSON)
- **Dataset**: 5,490,532 entries → 2,426,671 unique file paths (all 3 implementations produce identical counts and totals)
- **Changes vs v1**:
  - Use actual allocated `dsize` only (no fallback to `asize`, preventing sparse/cloud file inflation).
  - Deduplicate hardlinks (`hlnkc` + `ino`) once across all 3 languages (matching `ncdu` and `disk_map.py`).
  - Treat all JSON array nodes as directories (fixes empty directories being counted as files).

| Implementation | Wall Time | User CPU | Sys CPU | Peak RSS | Peak Memory Footprint |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Go (v2, slab + parallel)** (`./src-go/bin/analyze-ncdu`) | **3.64s** | 4.15s | 0.19s | **498.8 MiB** (0.49 GiB) | **497.8 MiB** (0.49 GiB) |
| **Rust (v2, streaming)** (`./rs/target/release/analyze-ncdu`) | 4.85s | 4.20s | 0.25s | 604.9 MiB (0.59 GiB) | 605.0 MiB (0.59 GiB) |
| **Python (v2)** (`uvx --with tqdm python analyze_ncdu_v2.py`) | 23.04s | 17.42s | 3.79s | 2,301.6 MiB (2.25 GiB) | N/A\* |

### v2 Key Takeaways

- **Go vs. Rust**: **1.33× faster** wall time (`3.64s` vs `4.85s`) and **17.5% less RAM** (`498.8 MiB` vs `604.9 MiB`).
- **Go vs. Python**: **6.33× faster** wall time (`3.64s` vs `23.04s`) with **4.61× lower** Peak RSS (`498.8 MiB` vs `2,301.6 MiB`).
- **Rust vs. Python**: **4.75× faster** wall time (`4.85s` vs `23.04s`) with **3.80× lower** Peak RSS (`604.9 MiB` vs `2,301.6 MiB`).

---

## v1 (2026-11-19) — Initial Go Port & Streaming Rust Optimization

Measured with macOS `/usr/bin/time -l` (release binaries pre-built) on host `VietVu-Macbook` (`darwin/arm64`) against `data/VietVu-Macbook/disk_usage.json.zst` (`snapshots/disk_usage_2026-11-03_132850.json.zst`):

- **Snapshot**: 33.3 MiB `.json.zst` (246.6 MiB uncompressed JSON)
- **Dataset**: 4,860,815 entries → 2,326,093 unique paths

| Implementation | Wall Time | User CPU | Sys CPU | Peak RSS | Peak Memory Footprint |
| --- | ---: | ---: | ---: | ---: | ---: |
| **Go (optimized)** (`./src-go/bin/analyze-ncdu`) | **2.69s** | 3.59s | 0.17s | **467.6 MiB** (0.46 GiB) | **466.0 MiB** (0.46 GiB) |
| **Rust (streaming)** (`./rs/target/release/analyze-ncdu`) | 3.75s | 3.33s | 0.17s | 554.5 MiB (0.54 GiB) | 554.1 MiB (0.54 GiB) |
| **Go (v1 streaming)** (two Go maps, single-threaded) | 6.63s | 6.79s | 0.34s | 1,038.8 MiB (1.01 GiB) | 1,047.7 MiB (1.02 GiB) |
| **Rust (old DOM)** (`serde_json::Value` + `.cloned()`) | 20.17s | 9.36s | 4.29s | 2,504.5 MiB (2.45 GiB) | 8,367.4 MiB (8.17 GiB) |
| **Python** (`uvx --with tqdm python analyze_ncdu_v2.py`) | 20.81s | 15.76s | 3.77s | 2,008.9 MiB (1.96 GiB) | N/A\* |

\* `/usr/bin/time -l` reports `peak memory footprint` (`ri_phys_footprint`) for the direct `uvx` wrapper process (11.2 MiB) rather than the spawned `python` child, whereas `Peak RSS` (`ru_maxrss`) reflects the child `python` process.

## Key Takeaways

- **Go (optimized) vs. Rust (streaming)**: **1.39× faster** wall time (`2.69s` vs `3.75s`) and **16% less RAM** (`467.6 MiB` vs `554.5 MiB`) using:
  - Pointer-free 8 MiB byte slabs + open-addressed hash table (`src-go/table.go`) so Go's GC has almost zero pointers to scan and slab growth never doubles/copies.
  - Direct-buffer streaming JSON scanner (`src-go/parser.go`) with zero per-entry string allocations.
  - O(N) Top-30 scan (skipping a 2.33M-file sort), a single parallel mergesort across CPU cores, and concurrent goroutines for all 12 cleanable categories + App/User directory aggregation.
- **Overall speedup vs. Python**: Go is **7.74× faster** (`2.69s` vs `20.81s`) with **4.30× lower** Peak RSS (`467.6 MiB` vs `2,008.9 MiB`).
