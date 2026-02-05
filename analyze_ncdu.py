#!/usr/bin/env python3
"""
Analyze ncdu JSON output to find large directories that can be cleaned.
The ncdu JSON format is: [version, timestamp, metadata, tree_structure]
"""

import json
import duckdb
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple


def parse_ncdu_tree(node: List, parent_path: str = "") -> List[Dict[str, Any]]:
    """
    Recursively parse ncdu tree structure and extract directory/file information.

    ncdu format for each node:
    - First element is metadata dict with 'name', 'asize' (apparent size), 'dsize' (disk size)
    - Subsequent elements are either:
      - File dicts (no children)
      - Lists representing subdirectories (first element is metadata, rest are children)
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

    # Create DuckDB connection and load data
    print("\nAnalyzing with DuckDB...")
    con = duckdb.connect(":memory:")

    # Convert to pandas DataFrame for DuckDB
    df = pd.DataFrame(entries)
    con.register("entries", df)
    con.execute("CREATE TABLE disk_usage AS SELECT * FROM entries")

    # Analysis queries
    print("\n" + "=" * 80)
    print("TOP 30 LARGEST FILES")
    print("=" * 80)

    result = con.execute("""
        SELECT 
            path,
            ROUND(disk_size_bytes / 1024.0 / 1024.0, 2) as size_mb,
            ROUND(disk_size_bytes / 1024.0 / 1024.0 / 1024.0, 2) as size_gb
        FROM disk_usage
        WHERE is_directory = false
        ORDER BY disk_size_bytes DESC
        LIMIT 30
    """).fetchall()

    for row in result:
        if row[2] >= 1.0:
            print(f"{row[2]:>8.2f} GB  {row[0]}")
        else:
            print(f"{row[1]:>8.2f} MB  {row[0]}")

    print("\n" + "=" * 80)
    print("CLEANABLE DIRECTORIES (GROUPED & AGGREGATED)")
    print("=" * 80)

    # Define patterns to find top-level cleanable directories
    # Format: (pattern, name_pattern, description)
    patterns = [
        ("%/node_modules", "/node_modules", "Node.js dependencies (node_modules)"),
        ("%/.venv", "/.venv", "Python virtual environments (.venv)"),
        ("%/venv", "/venv", "Python virtual environments (venv)"),
        ("%/target", "/target", "Rust/Cargo build artifacts (target)"),
        ("%/.m2", "/.m2", "Maven cache (.m2)"),
        ("%/.gradle", "/.gradle", "Gradle cache (.gradle)"),
        ("%/.npm", "/.npm", "NPM cache (.npm)"),
        ("%/__pycache__", "/__pycache__", "Python bytecode cache (__pycache__)"),
        ("%/Library/Caches", "/Library/Caches", "macOS application caches"),
        ("%/Library/Logs", "/Library/Logs", "macOS log files"),
        ("%/.cache", "/.cache", "General cache directories (.cache)"),
        ("%/.git", "/.git", "Git repositories (.git)"),
    ]

    for pattern, name_suffix, description in patterns:
        # Find all files/dirs under the pattern and sum their sizes
        # Group by the top-level directory matching the pattern
        result = con.execute(
            """
            WITH all_entries AS (
                SELECT 
                    CASE 
                        WHEN path LIKE '///System/Volumes/Data/%' 
                        THEN SUBSTR(path, 25)  -- Remove '///System/Volumes/Data'
                        ELSE SUBSTR(path, 2)  -- Remove leading '/'
                    END as clean_path,
                    disk_size_bytes
                FROM disk_usage
                WHERE path LIKE ?
            ),
            -- Extract the root directory that matches our pattern
            grouped AS (
                SELECT 
                    SUBSTR(clean_path, 1, 
                           INSTR(clean_path, ?) + LENGTH(?) - 1
                    ) as root_dir,
                    SUM(disk_size_bytes) as total_bytes
                FROM all_entries
                WHERE INSTR(clean_path, ?) > 0
                GROUP BY root_dir
            )
            SELECT 
                COUNT(DISTINCT root_dir) as num_dirs,
                ROUND(SUM(total_bytes) / 1024.0 / 1024.0 / 1024.0, 2) as total_gb,
                ROUND(AVG(total_bytes) / 1024.0 / 1024.0, 2) as avg_mb
            FROM grouped
        """,
            [pattern, name_suffix, name_suffix, name_suffix],
        ).fetchone()

        if result and result[0] > 0:
            num_dirs = result[0]
            total_gb = result[1]
            avg_mb = result[2]
            if total_gb >= 0.01:  # Only show if total > 10MB
                print(f"{description}:")
                print(
                    f"  {num_dirs} directories, Total: {total_gb:.2f} GB, Avg: {avg_mb:.1f} MB each"
                )

    print("\n" + "=" * 80)
    print("SIZE BY DIRECTORY DEPTH")
    print("=" * 80)

    result = con.execute("""
        SELECT 
            depth,
            COUNT(*) as num_entries,
            ROUND(SUM(disk_size_bytes) / 1024.0 / 1024.0 / 1024.0, 2) as total_gb
        FROM disk_usage
        WHERE is_directory = true
        GROUP BY depth
        ORDER BY depth
        LIMIT 10
    """).fetchall()

    for row in result:
        print(f"Depth {row[0]}: {row[1]:>8} directories, {row[2]:>10.2f} GB total")

    print("\n" + "=" * 80)
    print("LARGEST APPLICATION DIRECTORIES")
    print("=" * 80)

    # Find large application and user directories
    result = con.execute("""
        WITH deduped AS (
            SELECT 
                CASE 
                    WHEN path LIKE '//System/Volumes/Data/%' 
                    THEN SUBSTR(path, 24)
                    ELSE path 
                END as clean_path,
                disk_size_bytes
            FROM disk_usage
            WHERE is_directory = true
                AND (
                    path LIKE '///Applications/%'
                    OR path LIKE '///Users/%/Library/Application Support/%'
                    OR path LIKE '///Users/%/Library/Containers/%'
                    OR path LIKE '///Users/%/.cache/%'
                )
                AND depth BETWEEN 4 AND 10
        )
        SELECT 
            clean_path,
            ROUND(SUM(disk_size_bytes) / 1024.0 / 1024.0 / 1024.0, 2) as size_gb
        FROM deduped
        GROUP BY clean_path
        HAVING SUM(disk_size_bytes) > 100 * 1024 * 1024  -- Only show > 100MB
        ORDER BY SUM(disk_size_bytes) DESC
        LIMIT 20
    """).fetchall()

    for row in result:
        if row[1] >= 1.0:
            print(f"  {row[1]:>8.2f} GB  {row[0]}")
        else:
            print(f"  {row[1] * 1024:>8.2f} MB  {row[0]}")

    # Export results to CSV for further analysis
    print("\n" + "=" * 80)
    print("Exporting detailed results to CSV files...")
    print("=" * 80)

    con.execute("""
        COPY (
            SELECT 
                path,
                disk_size_bytes / 1024.0 / 1024.0 / 1024.0 as size_gb,
                depth
            FROM disk_usage
            WHERE is_directory = true
            ORDER BY disk_size_bytes DESC
            LIMIT 1000
        ) TO 'top_1000_directories.csv' (HEADER, DELIMITER ',')
    """)
    print("✓ Exported: top_1000_directories.csv")

    con.execute("""
        COPY (
            SELECT 
                path,
                disk_size_bytes / 1024.0 / 1024.0 as size_mb
            FROM disk_usage
            WHERE is_directory = false
            ORDER BY disk_size_bytes DESC
            LIMIT 1000
        ) TO 'top_1000_files.csv' (HEADER, DELIMITER ',')
    """)
    print("✓ Exported: top_1000_files.csv")

    con.close()
    print("\nAnalysis complete!")


if __name__ == "__main__":
    main()
