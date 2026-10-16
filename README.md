# macOS Disk Analyzer

A fast Python tool to analyze your macOS disk usage and identify large directories that can be safely cleaned to free up space.

It uses [ncdu](https://dev.yorhel.nl/ncdu) to scan your filesystem and export a JSON snapshot, then analyzes that snapshot to surface cleanable build artifacts, caches, and dependencies grouped by type.

## Features

- Identifies large **node_modules**, **Rust target**, **.venv**, **Maven/Gradle** caches, and more
- Groups similar directories and shows aggregated totals
- Exports per-category path lists (sorted by size) so you can delete selectively
- Deduplicates APFS mount points (`/System/Volumes/Data`) automatically
- Runs entirely offline — no data leaves your machine

## Prerequisites

### Install ncdu

**Homebrew (recommended)**
```bash
brew install ncdu
```

**MacPorts**
```bash
sudo port install ncdu
```

Verify the installation:
```bash
ncdu --version
```

### Python

Python 3.7+ is required. The script uses [uvx](https://docs.astral.sh/uv/) to automatically install the `tqdm` dependency at run time, so no manual `pip install` is needed.

Install `uv` (which provides `uvx`) if you don't have it:
```bash
brew install uv
```

## Quick Start

### Step 1 — Scan your disk with ncdu

```bash
sudo ncdu -o data/$(hostname)/disk_usage.json /
```

> **Why `sudo`?**  
> Running ncdu as root lets it read every directory on your Mac, including system folders and other users' home directories. The scan is **read-only** — ncdu only reads file metadata (names and sizes) and writes the result to the JSON file you specify. No files are modified or deleted.

The scan typically takes 2–10 minutes depending on disk size. You will see a live progress display in your terminal.

### Step 2 — Analyze the output

```bash
uvx --with tqdm python analyze_ncdu_v2.py
```

By default the script reads `data/<hostname>/disk_usage.json` and writes all output files back to the same directory.

#### Specify a different hostname

```bash
uvx --with tqdm python analyze_ncdu_v2.py --host my-macbook
```

This reads `data/my-macbook/disk_usage.json`.

## Output

The script prints three sections to your terminal:

| Section | Description |
|---------|-------------|
| **Top 30 Largest Files** | The biggest individual files on your disk |
| **Cleanable Directories** | Grouped by type (node_modules, .venv, target, …) with totals and top-10 list |
| **Largest Application & User Directories** | Big entries under `/Applications` and `~/Library` |

It also writes one text file per category inside `data/<hostname>/`:

```
data/<hostname>/cleanable_node_modules_paths.txt
data/<hostname>/cleanable_target_paths.txt
data/<hostname>/cleanable_.venv_paths.txt
...
```

Each file contains tab-separated `<size_gb>  <path>` lines, sorted largest first.

### Example: use the exported path lists

```bash
# Preview what would be deleted
cat data/$(hostname)/cleanable_target_paths.txt

# Delete all Rust target directories (careful!)
awk '{print $2}' data/$(hostname)/cleanable_target_paths.txt | xargs rm -rf

# Delete only directories larger than 500 MB
awk '$1 > 0.5 {print $2}' data/$(hostname)/cleanable_target_paths.txt | xargs rm -rf
```

## Disk map & cleanup suggestions

```bash
make map          # or: uvx python disk_map.py && open data/$(hostname)/disk_map.html
```

`disk_map.py` (Python 3.11+, no dependencies) turns the same scan into an interactive page, `data/<hostname>/disk_map.html`:

- **Explorer**: a treemap of the disk sized by space used and coloured by category. Click a tile to open it. Striped tiles are cleanup suggestions. **Cleanup** mode greys out everything except suggestions.
- **Recommendations**: grouped as *Your list*, *Safe to remove* and *Review first*. Tick items and the totals update. **Copy script** / **Download .sh** builds a shell script from your selection.
- `data/<hostname>/cleanup_suggested.sh`: the same suggestions as a script. Review items are commented out.

### Your own "usually deletable" list

Put the things you always delete in **`my_cleanup_rules.toml`**. These rules are checked before the built-in ones, are pre-selected, and are listed first:

```toml
[[rule]]
label = "Scratch projects"
paths = ["~/Desktop/sandbox", "~/works/old-*"]   # ~ home, * one segment, **/ any depth, {a,b}

[[rule]]
label = "Keep my HF models"   # protect something the built-in rules would suggest
tier = "keep"
paths = ["~/.cache/huggingface"]
```

The built-in rules live in `cleanup_rules.toml` (npm/uv/Gradle/Cargo caches, Xcode DerivedData, `.next`, Rust `target/` next to a `Cargo.toml`, node_modules, venvs, Ollama models, simulators, …). Each rule can set a `command` (for example `npm cache clean --force`) that runs instead of `rm -rf`. The same file holds the category colours used on the map.

## Cleanable Directory Categories

| Category | Safe to delete? | Notes |
|----------|----------------|-------|
| `node_modules/` | ✅ Yes | Regenerate with `npm install` |
| `.venv/` / `venv/` | ✅ Yes | Regenerate with `pip install` |
| `target/` (Rust) | ✅ Yes | Regenerate with `cargo build` |
| `.npm/` | ✅ Yes | `npm cache clean --force` |
| `.m2/` (Maven) | ✅ Yes | Redownloaded on next build |
| `.gradle/` | ✅ Yes | Redownloaded on next build |
| `__pycache__/` | ✅ Yes | Python regenerates automatically |
| `.cache/` | ✅ Yes | Apps recreate as needed |
| `Library/Caches` | ⚠️ Review | Safe for unused apps |
| `Library/Logs` | ⚠️ Review | Old logs only |
| `.git/` | ⚠️ Caution | Only delete repos you no longer need |

## Project Structure

```
.
├── analyze_ncdu_v2.py   # Main analysis script (recommended)
├── disk_map.py               # Interactive disk map + cleanup suggestions
├── disk_map_template.html    # Page template used by disk_map.py
├── cleanup_rules.toml        # Built-in "usually deletable" rules + map categories
├── my_cleanup_rules.toml     # YOUR rules: matched first, listed first
└── data/
    └── <hostname>/
        ├── disk_usage.json          # ncdu scan output (not committed)
        ├── disk_map.html            # Generated map (not committed)
        ├── cleanup_suggested.sh     # Generated script (not committed)
        └── cleanable_*_paths.txt    # Generated path lists (not committed)
```

## License

MIT
