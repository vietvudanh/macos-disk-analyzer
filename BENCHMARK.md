# Benchmark: Go vs. Rust vs. Python (`analyze-ncdu`)

Measured with macOS `/usr/bin/time -l` (release binaries pre-built) on host `VietVu-Macbook` (`darwin/arm64`) against `data/VietVu-Macbook/disk_usage.json.zst`:

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
