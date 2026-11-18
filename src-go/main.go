package main

import (
	"bufio"
	"bytes"
	"cmp"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"
)

type PathSize struct {
	Path string
	Size uint64
}

var (
	cloudSlashBytes = []byte("/Library/CloudStorage/")
	cloudEndBytes   = []byte("/Library/CloudStorage")
	sysDataPrefix   = []byte("System/Volumes/Data/")
	appsPrefix      = []byte("/Applications/")
	usersPrefix     = []byte("/Users/")
	libraryBytes    = []byte("/Library/")
)

func defaultHost() string {
	out, err := exec.Command("hostname").Output()
	if err == nil {
		if s := strings.TrimSpace(string(out)); s != "" {
			return s
		}
	}
	return "localhost"
}

func isExcludedBytes(path []byte) bool {
	return bytes.Contains(path, cloudSlashBytes) || bytes.HasSuffix(path, cloudEndBytes)
}

func normalizePathInto(raw []byte, dst []byte) []byte {
	for len(raw) > 0 && raw[0] == '/' {
		raw = raw[1:]
	}
	dst = dst[:0]
	if bytes.HasPrefix(raw, sysDataPrefix) {
		return append(dst, raw[len(sysDataPrefix)-1:]...)
	}
	dst = append(dst, '/')
	return append(dst, raw...)
}

func fmtSize(size uint64) string {
	gb := float64(size) / 1024.0 / 1024.0 / 1024.0
	if gb >= 1.0 {
		return fmt.Sprintf("%8.2f GB", gb)
	}
	return fmt.Sprintf("%8.2f MB", float64(size)/1024.0/1024.0)
}

func sortPathSizesDesc(items []PathSize) {
	slices.SortFunc(items, func(a, b PathSize) int {
		if c := cmp.Compare(b.Size, a.Size); c != 0 {
			return c
		}
		return cmp.Compare(a.Path, b.Path)
	})
}

func topNFiles(table *pathTable, k int) []PathSize {
	top := make([]PathSize, 0, k+1)
	for _, e := range table.entries {
		if e.size < 0 {
			continue
		}
		sz := uint64(e.size)
		if len(top) == k {
			last := top[k-1]
			if sz < last.Size {
				continue
			}
			if sz == last.Size && string(table.pathBytes(e)) >= last.Path {
				continue
			}
		}
		p := table.pathString(e)
		item := PathSize{Path: p, Size: sz}
		pos := len(top)
		for pos > 0 {
			prev := top[pos-1]
			if prev.Size > sz || (prev.Size == sz && prev.Path < p) {
				break
			}
			pos--
		}
		if len(top) < k {
			top = append(top, PathSize{})
		}
		copy(top[pos+1:], top[pos:])
		top[pos] = item
	}
	return top
}

type patternResult struct {
	desc   string
	totals []PathSize
	grand  uint64
	out    string
	err    error
}

func nthSlashPrefix(b []byte, slashes int) ([]byte, bool) {
	// b starts with '/'. Return prefix up to (not including) the (slashes+1)-th '/',
	// requiring at least `slashes` path components after the leading '/'.
	count := 0
	for i := 1; i < len(b); i++ {
		if b[i] == '/' {
			count++
			if count == slashes {
				return b[:i], true
			}
		}
	}
	if count == slashes-1 {
		return b, true
	}
	return nil, false
}

func main() {
	t0 := time.Now()
	host := defaultHost()
	includeCloud := false

	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--host":
			if i+1 < len(args) {
				i++
				host = args[i]
			}
		case strings.HasPrefix(a, "--host="):
			host = a[len("--host="):]
		case a == "--include-cloudstorage":
			includeCloud = true
		}
	}

	dataDir := filepath.Join("data", host)
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "create data dir: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("Host: %s\n", host)
	fmt.Printf("Output directory: %s/\n", dataDir)

	candidates := []string{
		filepath.Join(dataDir, "disk_usage.json.zst"),
		filepath.Join(dataDir, "disk_usage.json.gz"),
		filepath.Join(dataDir, "disk_usage.json"),
	}
	var input string
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			input = c
			break
		}
	}
	if input == "" {
		fmt.Printf("Error: no snapshot found (.json.zst, .json.gz, .json) in %s.\n", dataDir)
		return
	}

	fmt.Printf("Loading snapshot file: %s...\n", input)
	r, cleanup, err := openSnapshotStream(input)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open snapshot: %v\n", err)
		os.Exit(1)
	}

	table := newPathTable(3_000_000)

	_, stats, parseErr := parseSnapshot(r, table, includeCloud)
	closeErr := cleanup()
	if parseErr != nil {
		fmt.Fprintf(os.Stderr, "parse ncdu json: %v\n", parseErr)
		os.Exit(1)
	}
	if closeErr != nil {
		fmt.Fprintf(os.Stderr, "close snapshot stream: %v\n", closeErr)
		os.Exit(1)
	}
	table.releaseHashIndex()

	fmt.Printf("Found %d entries\n", stats.TotalEntries)
	fmt.Println("Aggregating sizes by path...")
	fmt.Printf("After deduplication: %d unique paths\n", table.files)

	sepLine := strings.Repeat("=", 80)

	// Top 30 largest files in O(N) without sorting all 2.3M files.
	top30 := topNFiles(table, 30)
	fmt.Printf("\n%s\n", sepLine)
	fmt.Println("TOP 30 LARGEST FILES")
	fmt.Println(sepLine)
	for _, item := range top30 {
		fmt.Printf("%s  %s\n", fmtSize(item.Size), item.Path)
	}

	// Single parallel sort of all entries by path.
	table.sortEntriesByPathParallel()

	patterns := [][2]string{
		{"/node_modules", "Node.js dependencies (node_modules)"},
		{"/.venv", "Python virtual environments (.venv)"},
		{"/venv", "Python virtual environments (venv)"},
		{"/target", "Rust/Cargo build artifacts (target)"},
		{"/.m2", "Maven cache (.m2)"},
		{"/.gradle", "Gradle cache (.gradle)"},
		{"/.npm", "NPM cache (.npm)"},
		{"/__pycache__", "Python bytecode cache (__pycache__)"},
		{"/Library/Caches", "macOS application caches"},
		{"/Library/Logs", "macOS log files"},
		{"/.cache", "General cache directories (.cache)"},
		{"/.git", "Git repositories (.git)"},
	}

	results := make([]patternResult, len(patterns))
	var appv []PathSize

	var wg sync.WaitGroup
	wg.Add(len(patterns) + 1)

	entries := table.entries

	for idx, pat := range patterns {
		go func(idx int, suffixStr, desc string) {
			defer wg.Done()
			suffix := []byte(suffixStr)
			var roots []pathEntry
			for _, e := range entries {
				p := table.pathBytes(e)
				if bytes.HasSuffix(p, suffix) && bytes.Count(p, suffix) == 1 {
					roots = append(roots, e)
				}
			}
			if len(roots) == 0 {
				return
			}

			totals := make([]PathSize, 0, len(roots))
			var grand uint64
			prefixBuf := make([]byte, 0, 256)
			for _, root := range roots {
				rp := table.pathBytes(root)
				prefixBuf = append(prefixBuf[:0], rp...)
				prefixBuf = append(prefixBuf, '/')
				start := sort.Search(len(entries), func(i int) bool {
					return bytes.Compare(table.pathBytes(entries[i]), prefixBuf) >= 0
				})
				var total uint64
				for i := start; i < len(entries); i++ {
					e := entries[i]
					if !bytes.HasPrefix(table.pathBytes(e), prefixBuf) {
						break
					}
					if e.size >= 0 {
						total += uint64(e.size)
					}
				}
				totals = append(totals, PathSize{Path: string(rp), Size: total})
				grand += total
			}

			gb := float64(grand) / 1024.0 / 1024.0 / 1024.0
			if gb < 0.01 {
				return
			}
			sortPathSizesDesc(totals)

			sanitized := strings.Trim(strings.ReplaceAll(suffixStr, "/", "_"), "_")
			fname := fmt.Sprintf("cleanable_%s_paths.txt", sanitized)
			out := filepath.Join(dataDir, fname)
			of, err := os.Create(out)
			if err != nil {
				results[idx].err = fmt.Errorf("create output %s: %w", out, err)
				return
			}
			w := bufio.NewWriter(of)
			for _, item := range totals {
				if _, err := fmt.Fprintf(w, "%.4f\t%s\n", float64(item.Size)/1024.0/1024.0/1024.0, item.Path); err != nil {
					_ = of.Close()
					results[idx].err = fmt.Errorf("write output %s: %w", out, err)
					return
				}
			}
			if err := w.Flush(); err != nil {
				_ = of.Close()
				results[idx].err = fmt.Errorf("flush output %s: %w", out, err)
				return
			}
			if err := of.Close(); err != nil {
				results[idx].err = fmt.Errorf("close output %s: %w", out, err)
				return
			}

			results[idx] = patternResult{
				desc:   desc,
				totals: totals,
				grand:  grand,
				out:    out,
			}
		}(idx, pat[0], pat[1])
	}

	// Compute Largest Application & User Directories concurrently.
	go func() {
		defer wg.Done()
		app := make(map[string]uint64, 512)
		for _, e := range entries {
			if e.size < 0 {
				continue
			}
			p := table.pathBytes(e)
			var keyBytes []byte
			var ok bool
			if bytes.HasPrefix(p, appsPrefix) {
				keyBytes, ok = nthSlashPrefix(p, 2)
			} else if bytes.HasPrefix(p, usersPrefix) && bytes.Contains(p, libraryBytes) {
				keyBytes, ok = nthSlashPrefix(p, 4)
			} else {
				continue
			}
			if !ok {
				continue
			}
			app[string(keyBytes)] += uint64(e.size)
		}
		list := make([]PathSize, 0, len(app))
		for p, s := range app {
			list = append(list, PathSize{Path: p, Size: s})
		}
		sortPathSizesDesc(list)
		appv = list
	}()

	wg.Wait()

	fmt.Printf("\n%s\n", sepLine)
	fmt.Println("CLEANABLE DIRECTORIES (GROUPED & AGGREGATED)")
	fmt.Println(sepLine)
	for _, res := range results {
		if res.err != nil {
			fmt.Fprintf(os.Stderr, "%v\n", res.err)
			os.Exit(1)
		}
		if len(res.totals) == 0 {
			continue
		}
		gb := float64(res.grand) / 1024.0 / 1024.0 / 1024.0
		fmt.Printf("\n%s:\n", res.desc)
		fmt.Printf("  %d directories, Total: %.2f GB\n", len(res.totals), gb)
		fmt.Println("  Top 10 largest:")
		for _, item := range res.totals[:min(10, len(res.totals))] {
			fmt.Printf("    %s  %s\n", fmtSize(item.Size), item.Path)
		}
		fmt.Printf("  → Exported all %d paths to: %s\n", len(res.totals), res.out)
	}

	fmt.Printf("\n%s\n", sepLine)
	fmt.Println("LARGEST APPLICATION & USER DIRECTORIES")
	fmt.Println(sepLine)
	for _, item := range appv[:min(20, len(appv))] {
		if float64(item.Size)/1024.0/1024.0/1024.0 >= 0.1 {
			fmt.Printf("  %s  %s\n", fmtSize(item.Size), item.Path)
		}
	}

	fmt.Printf("\n%s\n", sepLine)
	fmt.Println("Analysis complete!")
	fmt.Printf("All output files saved to: %s/\n", dataDir)
	fmt.Printf("Done in %.1fs\n", time.Since(t0).Seconds())
	fmt.Println(sepLine)
}
