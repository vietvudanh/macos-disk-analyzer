# macOS Disk Usage Analysis Summary

**Tool Used:** ncdu (https://dev.yorhel.nl/ncdu)  
**Script:** analyze_ncdu_v2.py

## How to Run the Analysis

```bash
# Step 1: Generate ncdu JSON file (run as root to scan everything)
sudo ncdu -o data/$(hostname)/disk_usage.json /

# Step 2: Run the analysis script
# default works with file in data/$(hostname)/, temp files are written to there too
uvx --with tqdm python analyze_ncdu_v2.py
```

## Executive Summary

This tool analyzes the entire macOS root filesystem to identify large directories and files that can potentially be cleaned to free up disk space. The analysis groups similar directories (node_modules, .venv, etc.) and provides aggregated statistics.

## Common Findings & What to Expect

### Typical Large Files

1. **System Files** ⚠️ **Do NOT delete**
   - System Cryptex OS images (several GB)
   - Virtual memory/swap files (several GB)
   - System shared caches (`dyld_shared_cache_*`)

2. **Application Data** ✅ Review before deleting
   - Game files (often 10+ GB per game)
   - Browser caches (Chrome, Safari, Firefox)
   - Application-specific caches

3. **Development Cache/Build Artifacts** ✅ Safe to delete

### Cleanable Directory Categories

The tool aggregates the total size of all matching directories:

1. **Rust/Cargo Build Artifacts** (`target/`)
   - ✅ Safe to delete, regenerate with `cargo build`

2. **NPM Cache** (`.npm/`)
   - ✅ Clear with `npm cache clean --force`

3. **Node.js Dependencies** (`node_modules/`)
   - ✅ Safe to delete from old projects, regenerate with `npm install`

4. **Python Virtual Environments** (`.venv/`, `venv/`)
   - ✅ Safe to delete from old projects, regenerate with `pip install`

5. **macOS Application Caches** (`Library/Caches`)
   - ✅ Safe to clear for unused apps

6. **Maven Cache** (`.m2/`)
   - ✅ Can clear, may slow down next build

7. **Gradle Cache** (`.gradle/`)
   - ✅ Can clear, will redownload on next build

8. **General Cache Directories** (`.cache/`)
   - ✅ Safe to clear, apps will recreate

9. **Git Repositories** (`.git/`)
   - ⚠️ Only delete if you don't need the repo history
   - Consider running `git gc` to compress instead

10. **Python Bytecode Cache** (`__pycache__/`)
    - ✅ Safe to delete, Python will regenerate

11. **macOS Log Files** (`Library/Logs`)
    - ✅ Old logs can be deleted

## Cleanup Recommendations

### High Priority (Safe & High Impact)

1. **Rust Build Artifacts**
   - Find and delete: `find ~ -type d -name "target" -exec du -sh {} \;`
   - Clean specific project: `cargo clean` in project directory
   - Use the generated `cleanable_target_paths.txt` file

2. **NPM Cache**
   - Clear with: `npm cache clean --force`
   - Use the generated `cleanable_.npm_paths.txt` file

3. **Node.js Dependencies**
   - Find old projects: `find ~ -name "node_modules" -type d`
   - Delete from unused projects
   - Use the generated `cleanable_node_modules_paths.txt` file

4. **Python Virtual Environments**
   - Find: `find ~ -name ".venv" -o -name "venv" -type d`
   - Delete from unused projects
   - Use the generated `cleanable_.venv_paths.txt` and `cleanable_venv_paths.txt` files

### Medium Priority (Review First)

5. **macOS Application Caches**
   - Clear: `rm -rf ~/Library/Caches/*` (⚠️ Review first!)
   - Or use CleanMyMac/DaisyDisk/similar tools
   - Use the generated `cleanable_Library_Caches_paths.txt` file

6. **Maven/Gradle Cache**
   - Clear Maven: `rm -rf ~/.m2/repository`
   - Clear Gradle: `rm -rf ~/.gradle/caches`
   - Use the generated `cleanable_.m2_paths.txt` or `cleanable_.gradle_paths.txt` files

7. **General Caches**
   - Review: `du -sh ~/.cache/*`
   - Clear specific caches as needed
   - Use the generated `cleanable_.cache_paths.txt` file

### Low Priority (Maintenance)

8. **Git Repositories**
   - Run `git gc` to compress: `find ~ -name ".git" -type d -execdir git gc \;`
   - Consider shallow clones for repos you don't actively develop

9. **Python Cache & Logs**
   - Clear Python cache: `find ~ -name "__pycache__" -type d -exec rm -rf {} +`
   - Clear logs: `rm -rf ~/Library/Logs/*` (⚠️ Review first!)
   - Use the generated `cleanable_pycache_paths.txt` and `cleanable_Library_Logs_paths.txt` files

10. **Unused Applications**
    - Review installed applications in `/Applications/`
    - Uninstall games or apps you no longer use
    - Large games can be 10-50+ GB each

## Estimated Space Recovery

Typical recoverable space varies by usage patterns:

- **Developers:** 30-100+ GB (build artifacts, dependencies, caches)
- **Casual users:** 5-30 GB (app caches, old downloads, logs)
- **Gamers:** 50-200+ GB (games, game assets, screenshots)

Run the analysis to see your specific breakdown!

## Technical Notes

- The analysis uses Python with optimized algorithms (binary search, tqdm progress bars)
- Paths are deduplicated by removing `/System/Volumes/Data` prefix (APFS mount point)
- Directory sizes include all descendants (files and subdirectories)
- Run with: `uvx --with tqdm python analyze_ncdu_v2.py`
- Processing time: ~5-10 seconds per million entries
- Requires: Python 3.7+, tqdm package (auto-installed via uvx)

## Output Files Generated

The analysis generates multiple output files:

1. **analyze_ncdu_v2.py** - Optimized Python script with progress bars
2. **cleanable\_\*\_paths.txt** - Text files containing all paths for each category:
   - `cleanable_target_paths.txt` - Rust/Cargo build artifacts
   - `cleanable_node_modules_paths.txt` - Node.js dependencies
   - `cleanable_.venv_paths.txt` - Python virtual environments (.venv)
   - `cleanable_venv_paths.txt` - Python virtual environments (venv)
   - `cleanable_.npm_paths.txt` - NPM cache
   - `cleanable_.m2_paths.txt` - Maven cache
   - `cleanable_.gradle_paths.txt` - Gradle cache
   - `cleanable_.cache_paths.txt` - General cache directories
   - `cleanable_.git_paths.txt` - Git repositories
   - `cleanable_pycache_paths.txt` - Python bytecode cache
   - `cleanable_Library_Caches_paths.txt` - macOS app caches
   - `cleanable_Library_Logs_paths.txt` - macOS log files

Each path file has format: `<size_in_gb>\t<path>`, sorted by size (largest first).

### Example: How to use the path files

```bash
# View all Rust target directories sorted by size
cat cleanable_target_paths.txt

# Delete all Rust target directories (CAREFUL!)
awk '{print $2}' cleanable_target_paths.txt | xargs rm -rf

# Delete only target directories larger than 100MB
awk '$1 > 0.1 {print $2}' cleanable_target_paths.txt | xargs rm -rf

# Preview what would be deleted (dry run)
awk '{print $2}' cleanable_target_paths.txt | head -10

# Delete specific directories by line number (e.g., first 5)
head -5 cleanable_target_paths.txt | awk '{print $2}' | xargs rm -rf
```

---

_Generated by Python analysis script on macOS_
