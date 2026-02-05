# macOS Disk Usage Analysis Summary

**Analysis Date:** February 5, 2026  
**Source File:** mac_mbp13_root.json  
**Tool Used:** ncdu 2.9.2  
**Total Entries Analyzed:** 5,364,510 files and directories

## How to Run the Analysis

```bash
# Generate ncdu JSON file (run as root to scan everything)
sudo ncdu -o mac_mbp13_root.json /

# Run the analysis script
uvx --with tqdm python analyze_ncdu_v2.py
```

## Executive Summary

This analysis scanned the entire macOS root filesystem to identify large directories and files that can potentially be cleaned to free up disk space. The analysis groups similar directories (node_modules, .venv, etc.) and provides aggregated statistics.

## Key Findings

### Largest Individual Files (Top 10)

1. **System Cryptex OS Images** (12.38 GB total)
   - `/System/Volumes/Preboot/.../cryptex1/current/os.dmg` - 6.19 GB
   - `/System/Volumes/Preboot/.../cryptex1/current/os.clone.dmg` - 6.19 GB
   - ⚠️ **Do NOT delete** - These are critical system files

2. **League of Legends Game Files** (15+ GB total)
   - `default-assets2.wad` - 3.01 GB (2 copies)
   - `default-assets.wad` - 2.32 GB (2 copies)
   - `Map11.wad.client` - 2.24 GB (2 copies)
   - `Map22.wad.client` - 2.24 GB (2 copies)
   - `Map22.wad.mobile` - 1.74 GB (2 copies)
   - `Companions.wad.client` - 952 MB (2 copies)
   - `Map12.wad.client` - 948 MB (2 copies)
   - ✅ **Can delete** if you don't play League of Legends anymore

3. **Virtual Memory/Swap Files** (6.00 GB total)
   - `/private/var/vm/sleepimage` - 2.00 GB (2 copies)
   - `/System/Volumes/VM/swapfile[0-3]` - 1.00 GB each (4 files)
   - ⚠️ **Do NOT delete manually** - Managed by the OS

4. **Chrome Cache**
   - `WebStorage/.../a6fd1eaed0efea00_0` - 1.66 GB (2 copies)
   - ✅ **Can clean** through Chrome settings or by deleting cache

5. **LM Studio Model Cache**
   - `LFM2.5-1.2B-Instruct-MLX-8bit/model.safetensors` - 1.16 GB (2 copies)
   - ✅ **Can delete** if you don't use this AI model anymore

6. **System Shared Cache**
   - `dyld_shared_cache_arm64e` - 926 MB (2 copies)
   - `dyld_shared_cache_arm64e.01` - 923 MB (2 copies)
   - ⚠️ **Do NOT delete** - Critical system files

### Cleanable Directory Categories (Aggregated)

These statistics show the total size of all matching directories combined:

1. **Rust/Cargo Build Artifacts** - 21.92 GB
   - 264 `target/` directories
   - ✅ Safe to delete, regenerate with `cargo build`

2. **NPM Cache** - 14.67 GB
   - 2 `.npm/` directories  
   - ✅ Clear with `npm cache clean --force`

3. **Node.js Dependencies** - 14.00 GB
   - 266 `node_modules/` directories
   - ✅ Safe to delete from old projects, regenerate with `npm install`

4. **Python Virtual Environments (.venv)** - 13.97 GB
   - 66 `.venv/` directories
   - ✅ Safe to delete from old projects, regenerate with `pip install`

5. **macOS Application Caches** - 13.01 GB
   - 2,147 directories under `Library/Caches`
   - ✅ Safe to clear for unused apps

6. **Maven Cache** - 12.11 GB
   - 4 `.m2/` directories
   - ✅ Can clear, may slow down next build

7. **General Cache Directories** - 10.38 GB
   - 24 `.cache/` directories
   - ✅ Safe to clear, apps will recreate

8. **Git Repositories** - 7.68 GB
   - 598 `.git/` directories
   - ⚠️ Only delete if you don't need the repo history

9. **Python Bytecode Cache** - 1.99 GB
   - 17,312 `__pycache__/` directories
   - ✅ Safe to delete, Python will regenerate

10. **macOS Log Files** - 0.41 GB
    - 1,920 directories under `Library/Logs`
    - ✅ Old logs can be deleted

11. **Python Virtual Environments (venv)** - 0.24 GB
    - 96 `venv/` directories
    - ✅ Safe to delete from old projects

## Recommendations for Cleanup

### High Priority (Safe & High Impact)

1. **Rust Build Artifacts** (~22 GB)
   - Find and delete: `find ~/works -type d -name "target" -exec du -sh {} \;`
   - Clean specific project: `cargo clean` in project directory

2. **League of Legends** (~15 GB)
   - If not playing: Uninstall the application completely
   - Location: `/Applications/League of Legends.app`

3. **NPM Cache** (~15 GB)
   - Clear with: `npm cache clean --force`

4. **Node.js Dependencies** (~14 GB)
   - Find old projects: `find ~/works -name "node_modules" -type d`
   - Delete from unused projects

5. **Python Virtual Environments** (~14 GB)
   - Find: `find ~/works -name ".venv" -o -name "venv" -type d`
   - Delete from unused projects

### Medium Priority (Review First)

6. **macOS Application Caches** (~13 GB)
   - Clear: `rm -rf ~/Library/Caches/*` (careful, review first)
   - Or use CleanMyMac/similar tools

7. **Maven Cache** (~12 GB)
   - Clear: `rm -rf ~/.m2/repository`

8. **General Caches** (~10 GB)
   - Review: `du -sh ~/.cache/*`
   - Clear specific caches as needed

### Low Priority (Maintenance)

9. **Git Repositories** (~8 GB)
   - Run `git gc` to compress: `find ~/works -name ".git" -type d -execdir git gc \;`

10. **Python Cache & Logs** (~2 GB)
    - Clear: `find ~/works -name "__pycache__" -type d -exec rm -rf {} +`
    - Clear logs: `rm -rf ~/Library/Logs/*`

## Potential Disk Space Recovery

**Estimated recoverable space (conservative):**
- Development artifacts (Rust, Node, Python): ~50 GB
- League of Legends: ~15 GB (if not needed)
- Various caches: ~25 GB
- Chrome cache + LM Studio: ~3 GB

**Total: ~90+ GB potentially recoverable**

## Technical Notes

- The analysis uses Python with optimized algorithms (binary search, tqdm progress bars)
- Paths are deduplicated by removing `/System/Volumes/Data` prefix (APFS mount point)
- Directory sizes include all descendants (files and subdirectories)
- Run with: `uvx --with tqdm python analyze_ncdu_v2.py`
- Processing ~5.4M entries takes about 5-10 seconds

## Files Generated

1. **analyze_ncdu_v2.py** - Optimized Python script with progress bars
2. **analysis_output_v2.txt** - Full analysis output
3. **cleanable_*_paths.txt** - Text files containing all paths for each category:
   - `cleanable_target_paths.txt` - Rust/Cargo build artifacts (264 directories)
   - `cleanable_node_modules_paths.txt` - Node.js dependencies (266 directories)
   - `cleanable_.venv_paths.txt` - Python virtual environments (66 directories)
   - `cleanable_.npm_paths.txt` - NPM cache (2 directories)
   - `cleanable_.m2_paths.txt` - Maven cache (4 directories)
   - `cleanable_.cache_paths.txt` - General cache directories (24 directories)
   - `cleanable_.git_paths.txt` - Git repositories (598 directories)
   - `cleanable_pycache_paths.txt` - Python bytecode cache (17,312 directories)
   - `cleanable_Library_Caches_paths.txt` - macOS app caches (2,147 directories)
   - `cleanable_Library_Logs_paths.txt` - macOS log files (1,920 directories)
   - `cleanable_venv_paths.txt` - Python venv directories (96 directories)

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

*Generated by Python analysis script on macOS*
