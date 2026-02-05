#!/usr/bin/env python3
"""
Analyze ncdu JSON output to find large directories that can be cleaned.
The ncdu JSON format is: [version, timestamp, metadata, tree_structure]

Run with: uvx --with tqdm python analyze_ncdu_v2.py
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Tuple
from collections import defaultdict
from tqdm import tqdm


def parse_ncdu_tree(node: List, parent_path: str = "") -> List[Dict[str, Any]]:
    """
    Recursively parse ncdu tree structure and extract directory/file information.
    """
    results = []

    if not isinstance(node, list) or len(node) == 0:
        return results

    # First element is the directory metadata
    metadata = node[0]
    if not isinstance(metadata, dict):
        return results

    name = metadata.get("name", "")
    current_path = f"{parent_path}/{name}" if parent_path else f"/{name}"

    # Get size information (in bytes, ncdu stores as blocks)
    asize = metadata.get("asize", 0)  # Apparent size
    dsize = metadata.get("dsize", 0)  # Disk size (actual space used)
    is_dir = len(node) > 1 or metadata.get("notreg", False)

    # Add current node
    results.append(
        {
            "path": current_path,
            "name": name,
            "apparent_size_bytes": asize,
            "disk_size_bytes": dsize if dsize else asize,
            "is_directory": is_dir,
            "depth": current_path.count("/"),
        }
    )

    # Process children
    for i in range(1, len(node)):
        child = node[i]
        if isinstance(child, list):
            # It's a subdirectory
            results.extend(parse_ncdu_tree(child, current_path))
        elif isinstance(child, dict):
            # It's a file
            child_name = child.get("name", "")
            child_path = f"{current_path}/{child_name}"
            child_asize = child.get("asize", 0)
            child_dsize = child.get("dsize", 0)

            results.append(
                {
                    "path": child_path,
                    "name": child_name,
                    "apparent_size_bytes": child_asize,
                    "disk_size_bytes": child_dsize if child_dsize else child_asize,
                    "is_directory": False,
                    "depth": child_path.count("/"),
                }
            )

    return results


def normalize_path(path: str) -> str:
    """Remove System/Volumes/Data prefix and extra leading slashes to deduplicate paths."""
    # First, normalize multiple leading slashes to single slash
    path = "/" + path.lstrip("/")

    # Then remove System/Volumes/Data prefix if present
    if path.startswith("/System/Volumes/Data/"):
        return path[20:]  # Remove '/System/Volumes/Data'
    return path


def main():
    # Load ncdu JSON
    print("Loading ncdu JSON file...")
    with open("mac_mbp13_root.json", "r") as f:
        data = json.load(f)

    # Parse metadata
    version = data[0]
    timestamp = data[1]
    metadata = data[2]
    tree = data[3] if len(data) > 3 else []

    print(f"ncdu version: {metadata.get('progname')} {metadata.get('progver')}")
    print(f"Scan timestamp: {metadata.get('timestamp')}")

    # Parse the tree
    print("Parsing directory tree...")
    entries = parse_ncdu_tree(tree)
    print(f"Found {len(entries)} entries")

    # Normalize paths and aggregate sizes
    print("Aggregating sizes by path...")
    path_sizes = defaultdict(int)
    for entry in tqdm(entries, desc="Processing entries", unit=" entries"):
        norm_path = normalize_path(entry["path"])
        path_sizes[norm_path] += entry["disk_size_bytes"]

    print(f"After deduplication: {len(path_sizes)} unique paths")

    # Analysis
    print("\n" + "=" * 80)
    print("TOP 30 LARGEST FILES")
    print("=" * 80)

    # Find largest files
    files = [
        (path, size)
        for path, size in path_sizes.items()
        if not any(path_sizes.get(f"{path}/{x}") for x in ["dummy"])
    ]  # Simple heuristic
    files_sorted = sorted(files, key=lambda x: x[1], reverse=True)[:30]

    for path, size in files_sorted:
        size_gb = size / 1024 / 1024 / 1024
        size_mb = size / 1024 / 1024
        if size_gb >= 1.0:
            print(f"{size_gb:>8.2f} GB  {path}")
        else:
            print(f"{size_mb:>8.2f} MB  {path}")

    print("\n" + "=" * 80)
    print("CLEANABLE DIRECTORIES (GROUPED & AGGREGATED)")
    print("=" * 80)

    # Sort paths once for efficient lookup
    sorted_paths = sorted(path_sizes.keys())

    # Define patterns to search for
    patterns = [
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
    ]

    for suffix, description in patterns:
        # Find all root directories ending with this pattern (not nested)
        matching_roots = []
        for path in sorted_paths:
            if path.endswith(suffix):
                # Check it's not a nested occurrence (e.g., /a/node_modules/b/node_modules)
                # by seeing if the suffix appears more than once
                if path.count(suffix) == 1:
                    matching_roots.append(path)

        if not matching_roots:
            continue

        # For each root, sum it + all children using binary search range
        root_totals = {}
        for root in tqdm(
            matching_roots,
            desc=f"  {description[:30]}",
            leave=False,
            disable=len(matching_roots) < 10,
        ):
            total = path_sizes[root]
            root_prefix = root + "/"

            # Use binary search to find range of paths starting with root_prefix
            # This is much faster than checking all paths
            import bisect

            start_idx = bisect.bisect_left(sorted_paths, root_prefix)
            # Find where paths no longer start with root_prefix
            end_prefix = root_prefix[:-1] + chr(ord(root_prefix[-1]) + 1)
            end_idx = bisect.bisect_left(sorted_paths, end_prefix)

            # Sum only the paths in this range
            for path in sorted_paths[start_idx:end_idx]:
                total += path_sizes[path]

            root_totals[root] = total

        if root_totals:
            total_size = sum(root_totals.values())
            total_gb = total_size / 1024 / 1024 / 1024
            if total_gb >= 0.01:  # Only show if > 10 MB
                print(f"\n{description}:")
                print(f"  {len(root_totals)} directories, Total: {total_gb:.2f} GB")

                # Sort by size and show top directories
                sorted_roots = sorted(
                    root_totals.items(), key=lambda x: x[1], reverse=True
                )

                # Show top 10 largest
                print(f"  Top 10 largest:")
                for path, size in sorted_roots[:10]:
                    size_gb = size / 1024 / 1024 / 1024
                    size_mb = size / 1024 / 1024
                    if size_gb >= 1.0:
                        print(f"    {size_gb:>8.2f} GB  {path}")
                    else:
                        print(f"    {size_mb:>8.2f} MB  {path}")

                # Export all paths to a file for easy deletion
                filename = f"cleanable_{suffix.replace('/', '_').strip('_')}_paths.txt"
                with open(filename, "w") as f:
                    for path, size in sorted_roots:
                        size_gb = size / 1024 / 1024 / 1024
                        f.write(f"{size_gb:.4f}\t{path}\n")
                print(f"  → Exported all {len(sorted_roots)} paths to: {filename}")

    print("\n" + "=" * 80)
    print("LARGEST APPLICATION & USER DIRECTORIES")
    print("=" * 80)

    # Find large application directories
    app_dirs = {}
    for path, size in tqdm(path_sizes.items(), desc="Finding app directories"):
        # Look for top-level application or user data directories
        if (
            path.startswith("/Applications/")
            or path.startswith("/Users/")
            and "/Library/" in path
        ):
            # Get the first few path components
            parts = path.split("/")
            if path.startswith("/Applications/") and len(parts) >= 3:
                key = "/".join(parts[:3])  # e.g., /Applications/App.app
            elif path.startswith("/Users/") and len(parts) >= 5:
                key = "/".join(parts[:5])  # e.g., /Users/name/Library/Caches
            else:
                continue

            if key not in app_dirs:
                app_dirs[key] = 0
            app_dirs[key] += size

    # Sort and display
    app_dirs_sorted = sorted(app_dirs.items(), key=lambda x: x[1], reverse=True)[:20]
    for path, size in app_dirs_sorted:
        size_gb = size / 1024 / 1024 / 1024
        if size_gb >= 0.1:  # Only show > 100 MB
            print(f"  {size_gb:>8.2f} GB  {path}")

    print("\n" + "=" * 80)
    print("Analysis complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
