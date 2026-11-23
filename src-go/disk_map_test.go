package main

import (
	"bytes"
	"cmp"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func TestGlobToRegexAndMatcher(t *testing.T) {
	home := "/Users/alice"
	entries := []matcherEntry{
		{owner: 0, glob: "~/Downloads/*.{dmg,pkg,iso}"},
		{owner: 1, glob: "target"},
		{owner: 2, glob: "~/Library/Application Support/{Code,Cursor}/{Cache,Code Cache}"},
		{owner: 3, glob: "/System"},
		{owner: 4, glob: "~/foo/?/bar"},
	}
	m, err := newMatcher(entries, home)
	if err != nil {
		t.Fatalf("newMatcher: %v", err)
	}

	cases := []struct {
		path string
		want int
		ok   bool
	}{
		{"/Users/alice/Downloads/app.dmg", 0, true},
		{"/Users/alice/Downloads/app.pkg", 0, true},
		{"/Users/alice/Downloads/app.zip", 0, false},
		{"/Users/alice/work/proj/target", 1, true},
		{"/target", 1, true},
		{"/Users/alice/Library/Application Support/Code/Code Cache", 2, true},
		{"/Users/alice/Library/Application Support/Cursor/Cache", 2, true},
		{"/System", 3, true},
		{"/System/Volumes", 0, false},
		{"/Users/alice/foo/x/bar", 4, true},
		{"/Users/alice/foo/xy/bar", 0, false},
	}

	for _, tc := range cases {
		got, ok := m.first(tc.path, nil)
		if ok != tc.ok || (ok && got != tc.want) {
			t.Errorf("first(%q) = (%d, %v), want (%d, %v)", tc.path, got, ok, tc.want, tc.ok)
		}
	}

	// Test accept predicate fallback when first matching rule fails `needs`.
	entries2 := []matcherEntry{
		{owner: 0, glob: "target"},
		{owner: 1, glob: "**/target"},
	}
	m2, err := newMatcher(entries2, home)
	if err != nil {
		t.Fatalf("newMatcher 2: %v", err)
	}
	got, ok := m2.first("/a/b/target", func(owner int) bool { return owner == 1 })
	if !ok || got != 1 {
		t.Fatalf("fallback accept: got (%d, %v), want (1, true)", got, ok)
	}
}

func TestLoadRepoTOMLRules(t *testing.T) {
	rules, cats, err := loadRules([]ruleFileSpec{
		{path: "../my_cleanup_rules.toml", source: "mine"},
		{path: "../cleanup_rules.toml", source: "builtin"},
	})
	if err != nil {
		t.Fatalf("loadRules: %v", err)
	}
	if len(rules) < 20 {
		t.Fatalf("expected >= 20 rules, got %d", len(rules))
	}
	if len(cats) < 5 {
		t.Fatalf("expected >= 5 categories, got %d", len(cats))
	}
	// Verify Rust target rule parsed `needs = "Cargo.toml"` and `command`.
	foundRust := false
	for _, r := range rules {
		if r.Label == "Rust target/ folders" {
			foundRust = true
			if len(r.Needs) != 1 || r.Needs[0] != "Cargo.toml" {
				t.Errorf("Rust rule needs = %v, want [Cargo.toml]", r.Needs)
			}
			if r.Command != "cd {parent} && cargo clean" {
				t.Errorf("Rust rule command = %q", r.Command)
			}
		}
	}
	if !foundRust {
		t.Error("Rust target/ folders rule not found")
	}
}

func TestParseAndPipeline(t *testing.T) {
	// Synthetic ncdu JSON with:
	// - hardlink deduplication
	// - big file (>= 20 MiB) kept as leaf node
	// - CloudStorage skipped when includeCloud == false
	// - /System/Volumes/Data folded into /
	// - Rust project with Cargo.toml marker + target/ dir
	// - Non-Rust project with target/ dir (should not match `needs = "Cargo.toml"`)
	const big = 25 * 1024 * 1024
	const small = 5 * 1024 * 1024
	ncduJSON := `[1,0,{"progname":"ncdu","progver":"2.6","timestamp":1700000000},[` +
		`{"name":"/","dsize":4096},` +
		`[{"name":"Users","dsize":0},` +
		`[{"name":"alice","dsize":0},` +
		`[{"name":"Downloads","dsize":0},{"name":"app.dmg","dsize":26214400},{"name":"hl1","dsize":26214400,"hlnkc":true,"ino":99},{"name":"hl2","dsize":26214400,"hlnkc":true,"ino":99}],` +
		`[{"name":"Library","dsize":0},[{"name":"CloudStorage","dsize":0},{"name":"huge","dsize":104857600}]],` +
		`[{"name":"rustproj","dsize":0},{"name":"Cargo.toml","dsize":100},[{"name":"target","dsize":0},{"name":"bin","dsize":5242880}]],` +
		`[{"name":"otherproj","dsize":0},[{"name":"target","dsize":0},{"name":"bin","dsize":5242880}]]` +
		`]],` +
		`[{"name":"System","dsize":0},[{"name":"Volumes","dsize":0},[{"name":"Data","dsize":0},[{"name":"extra","dsize":0},{"name":"f","dsize":2097152}]]]]` +
		`]]`

	meta, root, err := parseDiskMapSnapshot(strings.NewReader(ncduJSON), false)
	if err != nil {
		t.Fatalf("parseDiskMapSnapshot: %v", err)
	}
	if meta.Timestamp != "1700000000" {
		t.Errorf("timestamp = %q, want 1700000000", meta.Timestamp)
	}
	foldDataVolume(root)
	computeSizes(root)

	// /extra should be folded into root
	if root.kids["extra"] == nil || root.kids["extra"].size != 2097152 {
		t.Fatalf("expected /extra folded into root with size 2097152, got %+v", root.kids["extra"])
	}
	// CloudStorage should be skipped
	if root.kids["Users"].kids["alice"].kids["Library"].kids["CloudStorage"] != nil {
		t.Fatal("expected CloudStorage to be skipped")
	}
	// Downloads should have app.dmg + hl1 (hl2 deduplicated) = 2 * big
	dl := root.kids["Users"].kids["alice"].kids["Downloads"]
	if dl.size != 2*big {
		t.Fatalf("Downloads size = %d, want %d", dl.size, 2*big)
	}

	rules := []Rule{
		{Label: "Installers", Tier: "mine", Source: "mine", Paths: []string{"~/Downloads/*.dmg"}, MinSize: 1024 * 1024},
		{Label: "Rust target", Tier: "safe", Source: "builtin", Paths: []string{"target"}, Needs: []string{"Cargo.toml"}, Command: "cd {parent} && cargo clean", MinSize: 1024 * 1024},
	}
	var entries []matcherEntry
	for i, r := range rules {
		for _, g := range r.Paths {
			entries = append(entries, matcherEntry{owner: i, glob: g})
		}
	}
	m, err := newMatcher(entries, "/Users/alice")
	if err != nil {
		t.Fatalf("newMatcher: %v", err)
	}
	annotate(root, "/", nil, m, rules)
	var cands []Candidate
	collect(root, "/", noMatch, rules, &cands)
	if len(cands) != 2 {
		t.Fatalf("expected 2 candidates, got %d: %+v", len(cands), cands)
	}

	// Verify Candidate JSON marshals as a 4-tuple array [path, size, ruleIdx, whole].
	b, err := json.Marshal(cands[0])
	if err != nil {
		t.Fatalf("marshal candidate: %v", err)
	}
	if !strings.HasPrefix(string(b), "[") || !strings.HasSuffix(string(b), "]") {
		t.Errorf("candidate JSON = %s, expected JSON array", b)
	}

	// Verify shell script generation
	script := buildShellScript(rules, cands)
	if !strings.Contains(script, "cd /Users/alice/rustproj && cargo clean") {
		t.Errorf("expected cargo clean command in script:\n%s", script)
	}
	if !strings.Contains(script, "rm -rf -- /Users/alice/Downloads/app.dmg") {
		t.Errorf("expected rm -rf for app.dmg in script:\n%s", script)
	}
	_ = small
}

func TestShlexQuote(t *testing.T) {
	cases := map[string]string{
		"":                    "''",
		"/Users/alice/foo":    "/Users/alice/foo",
		"/Users/alice/my dir": "'/Users/alice/my dir'",
		"a'b":                 `'a'"'"'b'`,
	}
	for in, want := range cases {
		if got := shlexQuote(in); got != want {
			t.Errorf("shlexQuote(%q) = %q, want %q", in, got, want)
		}
	}
}

// --- Parity tests comparing Go disk_map against Python disk_map.py ----------

type parsedPayload struct {
	Meta struct {
		Host      string `json:"host"`
		Home      string `json:"home"`
		ScannedAt any    `json:"scanned_at"`
	} `json:"meta"`
	Categories []string       `json:"categories"`
	Rules      []Rule         `json:"rules"`
	Candidates [][]any        `json:"candidates"`
	Tree       SerializedNode `json:"tree"`
}

func extractEmbeddedPayload(t *testing.T, htmlPath string) parsedPayload {
	t.Helper()
	b, err := os.ReadFile(htmlPath)
	if err != nil {
		t.Fatalf("read %s: %v", htmlPath, err)
	}
	const marker = "const DATA = "
	start := bytes.Index(b, []byte(marker))
	if start == -1 {
		t.Fatalf("%s: %q not found", htmlPath, marker)
	}
	rest := b[start+len(marker):]
	end := bytes.Index(rest, []byte(";\n"))
	if end == -1 {
		t.Fatalf("%s: end of DATA JSON not found", htmlPath)
	}
	raw := bytes.ReplaceAll(rest[:end], []byte(`<\/`), []byte("</"))
	var p parsedPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		t.Fatalf("unmarshal DATA from %s: %v", htmlPath, err)
	}
	return p
}

func normalizeCandidates(cands [][]any) {
	slices.SortFunc(cands, func(a, b []any) int {
		sa, _ := a[1].(float64)
		sb, _ := b[1].(float64)
		if c := cmp.Compare(sb, sa); c != 0 {
			return c
		}
		pa, _ := a[0].(string)
		pb, _ := b[0].(string)
		return cmp.Compare(pa, pb)
	})
}

func normalizeTree(n *SerializedNode) {
	for i := range n.K {
		normalizeTree(&n.K[i])
	}
	slices.SortFunc(n.K, func(a, b SerializedNode) int {
		if c := cmp.Compare(b.S, a.S); c != 0 {
			return c
		}
		return cmp.Compare(a.N, b.N)
	})
}

func normalizeScriptLines(s string) []string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	var out []string
	var block []string
	flushBlock := func() {
		if len(block) > 0 {
			slices.Sort(block)
			out = append(out, block...)
			block = block[:0]
		}
	}
	for _, line := range lines {
		if strings.HasPrefix(line, "# Generated by disk_map.") {
			continue
		}
		if strings.HasPrefix(line, "rm -rf -- ") || strings.HasPrefix(line, "# rm -rf -- ") ||
			strings.HasPrefix(line, "cd ") || strings.HasPrefix(line, "# cd ") {
			block = append(block, line)
		} else {
			flushBlock()
			out = append(out, line)
		}
	}
	flushBlock()
	return out
}

func findPythonForDiskMap(t *testing.T) (string, []string) {
	t.Helper()
	// disk_map.py requires Python 3.11+ (tomllib).
	if p, err := exec.LookPath("python3"); err == nil {
		if err := exec.Command(p, "-c", "import tomllib").Run(); err == nil {
			return p, nil
		}
	}
	if p, err := exec.LookPath("uvx"); err == nil {
		return p, []string{"python"}
	}
	t.Skip("no Python 3.11+ (with tomllib) or uvx available for parity test")
	return "", nil
}

func assertDiskMapParity(t *testing.T, snapshotSrc, home, rulesPath, myRulesPath string) {
	t.Helper()
	pyBin, pyPrefix := findPythonForDiskMap(t)

	repoRoot, err := filepath.Abs("..")
	if err != nil {
		t.Fatalf("abs repo root: %v", err)
	}
	dataRoot := filepath.Join(repoRoot, "data")
	if err := os.MkdirAll(dataRoot, 0o755); err != nil {
		t.Fatalf("mkdir data root: %v", err)
	}

	pyDir, err := os.MkdirTemp(dataRoot, "_parity_py_*")
	if err != nil {
		t.Fatalf("mkdirtemp py: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(pyDir) })

	goDir, err := os.MkdirTemp(dataRoot, "_parity_go_*")
	if err != nil {
		t.Fatalf("mkdirtemp go: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(goDir) })

	ext := ".json"
	if strings.HasSuffix(snapshotSrc, ".json.zst") {
		ext = ".json.zst"
	} else if strings.HasSuffix(snapshotSrc, ".json.gz") {
		ext = ".json.gz"
	}
	snapName := "disk_usage" + ext
	absSnap, err := filepath.Abs(snapshotSrc)
	if err != nil {
		t.Fatalf("abs snapshot: %v", err)
	}
	if err := os.Symlink(absSnap, filepath.Join(pyDir, snapName)); err != nil {
		t.Fatalf("symlink py snapshot: %v", err)
	}
	if err := os.Symlink(absSnap, filepath.Join(goDir, snapName)); err != nil {
		t.Fatalf("symlink go snapshot: %v", err)
	}

	pyHost := filepath.Base(pyDir)
	goHost := filepath.Base(goDir)

	pyArgs := append(append([]string{}, pyPrefix...),
		filepath.Join(repoRoot, "disk_map.py"),
		"--host", pyHost,
		"--home", home,
		"--rules", rulesPath,
		"--my-rules", myRulesPath,
	)
	cmd := exec.Command(pyBin, pyArgs...)
	cmd.Dir = repoRoot
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("python disk_map.py failed: %v\n%s", err, out)
	}

	if err := runDiskMap([]string{
		"--host", goHost,
		"--home", home,
		"--rules", rulesPath,
		"--my-rules", myRulesPath,
	}); err != nil {
		t.Fatalf("go runDiskMap failed: %v", err)
	}

	pyPayload := extractEmbeddedPayload(t, filepath.Join(pyDir, "disk_map.html"))
	goPayload := extractEmbeddedPayload(t, filepath.Join(goDir, "disk_map.html"))

	if pyPayload.Meta.Home != goPayload.Meta.Home {
		t.Errorf("meta.home mismatch: py=%q go=%q", pyPayload.Meta.Home, goPayload.Meta.Home)
	}
	if !reflect.DeepEqual(pyPayload.Meta.ScannedAt, goPayload.Meta.ScannedAt) {
		t.Errorf("meta.scanned_at mismatch: py=%#v go=%#v", pyPayload.Meta.ScannedAt, goPayload.Meta.ScannedAt)
	}
	if !reflect.DeepEqual(pyPayload.Categories, goPayload.Categories) {
		t.Errorf("categories mismatch:\npy=%v\ngo=%v", pyPayload.Categories, goPayload.Categories)
	}
	if !reflect.DeepEqual(pyPayload.Rules, goPayload.Rules) {
		t.Errorf("rules mismatch:\npy=%+v\ngo=%+v", pyPayload.Rules, goPayload.Rules)
	}

	normalizeCandidates(pyPayload.Candidates)
	normalizeCandidates(goPayload.Candidates)
	if !reflect.DeepEqual(pyPayload.Candidates, goPayload.Candidates) {
		t.Errorf("candidates count: py=%d go=%d", len(pyPayload.Candidates), len(goPayload.Candidates))
		limit := min(len(pyPayload.Candidates), len(goPayload.Candidates))
		for i := 0; i < limit; i++ {
			if !reflect.DeepEqual(pyPayload.Candidates[i], goPayload.Candidates[i]) {
				t.Errorf("first candidate diff at %d:\npy=%v\ngo=%v", i, pyPayload.Candidates[i], goPayload.Candidates[i])
				break
			}
		}
	}

	normalizeTree(&pyPayload.Tree)
	normalizeTree(&goPayload.Tree)
	if !reflect.DeepEqual(pyPayload.Tree, goPayload.Tree) {
		pyJSON, _ := json.Marshal(pyPayload.Tree)
		goJSON, _ := json.Marshal(goPayload.Tree)
		t.Errorf("tree mismatch (py len=%d, go len=%d)", len(pyJSON), len(goJSON))
	}

	pyScript, err := os.ReadFile(filepath.Join(pyDir, "cleanup_suggested.sh"))
	if err != nil {
		t.Fatalf("read py script: %v", err)
	}
	goScript, err := os.ReadFile(filepath.Join(goDir, "cleanup_suggested.sh"))
	if err != nil {
		t.Fatalf("read go script: %v", err)
	}
	pyLines := normalizeScriptLines(string(pyScript))
	goLines := normalizeScriptLines(string(goScript))
	if !reflect.DeepEqual(pyLines, goLines) {
		t.Errorf("cleanup_suggested.sh mismatch: py=%d lines, go=%d lines", len(pyLines), len(goLines))
		for i := 0; i < min(len(pyLines), len(goLines)); i++ {
			if pyLines[i] != goLines[i] {
				t.Errorf("first script diff at line %d:\npy: %s\ngo: %s", i, pyLines[i], goLines[i])
				break
			}
		}
	}
}

func TestDiskMapParityWithPythonFixture(t *testing.T) {
	tmp := t.TempDir()
	snapPath := filepath.Join(tmp, "disk_usage.json")
	myRulesPath := filepath.Join(tmp, "my_cleanup_rules.toml")
	builtinRulesPath, err := filepath.Abs("../cleanup_rules.toml")
	if err != nil {
		t.Fatalf("abs cleanup_rules.toml: %v", err)
	}

	// Custom personal rules that test carving out of a built-in rule and tier="keep".
	myRules := `
[[rule]]
label = "Old installers in Downloads"
paths = ["~/Downloads/*.{dmg,pkg,iso,xip}"]
note = "Apps are already installed"

[[rule]]
label = "Keep my Hugging Face cache"
tier = "keep"
paths = ["~/.cache/huggingface"]

[[rule]]
label = "Scratch folder"
paths = ["~/works/scratch"]
`
	if err := os.WriteFile(myRulesPath, []byte(myRules), 0o644); err != nil {
		t.Fatalf("write my rules: %v", err)
	}

	ncduJSON := fmt.Sprintf(`[1,0,{"progname":"ncdu","progver":"2.6","timestamp":1763627534},[`+
		`{"name":"/","dsize":4096},`+
		`[{"name":"Users","dsize":0},`+
		`[{"name":"alice","dsize":0},`+
		`[{"name":"Downloads","dsize":0},`+
		`{"name":"Installer 1.dmg","dsize":%d},`+
		`{"name":"Archive.zip","dsize":%d},`+
		`{"name":"hardlink_a","dsize":%d,"hlnkc":true,"ino":42},`+
		`{"name":"hardlink_b","dsize":%d,"hlnkc":true,"ino":42}`+
		`],`+
		`[{"name":".Trash","dsize":0},{"name":"old.bin","dsize":%d}],`+
		`[{"name":".npm","dsize":0},[{"name":"_cacache","dsize":0},{"name":"blob","dsize":%d}]],`+
		`[{"name":".cache","dsize":0},`+
		`[{"name":"huggingface","dsize":0},{"name":"model.safetensors","dsize":%d}],`+
		`[{"name":"uv","dsize":0},{"name":"wheel.whl","dsize":%d}],`+
		`[{"name":"misc","dsize":0},{"name":"data","dsize":%d}]`+
		`],`+
		`[{"name":"Library","dsize":0},`+
		`[{"name":"CloudStorage","dsize":0},{"name":"cloud.bin","dsize":%d}],`+
		`[{"name":"Caches","dsize":0},[{"name":"com.example.app","dsize":0},{"name":"cache.db","dsize":%d}]],`+
		`[{"name":"Application Support","dsize":0},[{"name":"Code","dsize":0},[{"name":"Code Cache","dsize":0},{"name":"js.bin","dsize":%d}]]]`+
		`],`+
		`[{"name":"works","dsize":0},`+
		`[{"name":"rust-app","dsize":0},{"name":"Cargo.toml","dsize":128},[{"name":"target","dsize":0},{"name":"debug.o","dsize":%d}]],`+
		`[{"name":"non-rust","dsize":0},[{"name":"target","dsize":0},{"name":"keep.o","dsize":%d}]],`+
		`[{"name":"web-app","dsize":0},[{"name":"node_modules","dsize":0},{"name":"pkg.js","dsize":%d},[{"name":".cache","dsize":0},{"name":"nested","dsize":%d}]]],`+
		`[{"name":"scratch","dsize":0},{"name":"tmp.dat","dsize":%d}]`+
		`]`+
		`]],`+
		`[{"name":"System","dsize":0},`+
		`[{"name":"Library","dsize":0},{"name":"core","dsize":%d}],`+
		`[{"name":"Volumes","dsize":0},[{"name":"Data","dsize":0},[{"name":"Applications","dsize":0},{"name":"App.bin","dsize":%d}]]]`+
		`]`+
		`]]`,
		30*1024*1024,  // Installer 1.dmg
		25*1024*1024,  // Archive.zip
		22*1024*1024,  // hardlink_a
		22*1024*1024,  // hardlink_b (deduped)
		8*1024*1024,   // .Trash/old.bin
		12*1024*1024,  // .npm/_cacache/blob
		50*1024*1024,  // .cache/huggingface (kept by my_cleanup_rules)
		10*1024*1024,  // .cache/uv
		9*1024*1024,   // .cache/misc
		200*1024*1024, // CloudStorage (skipped)
		15*1024*1024,  // Library/Caches/com.example.app
		11*1024*1024,  // Code/Code Cache
		18*1024*1024,  // rust-app/target
		16*1024*1024,  // non-rust/target
		14*1024*1024,  // web-app/node_modules
		6*1024*1024,   // web-app/node_modules/.cache (nested, ignored)
		7*1024*1024,   // works/scratch
		40*1024*1024,  // System/Library
		35*1024*1024,  // System/Volumes/Data/Applications (folded to /Applications)
	)
	if err := os.WriteFile(snapPath, []byte(ncduJSON), 0o644); err != nil {
		t.Fatalf("write fixture snapshot: %v", err)
	}

	assertDiskMapParity(t, snapPath, "/Users/alice", builtinRulesPath, myRulesPath)
}

func TestDiskMapParityWithPythonRealSnapshot(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping real snapshot parity test in -short mode")
	}
	host := defaultHost()
	snapPath := ""
	for _, name := range []string{"disk_usage.json.zst", "disk_usage.json.gz", "disk_usage.json"} {
		c := filepath.Join("..", "data", host, name)
		if _, err := os.Stat(c); err == nil {
			snapPath = c
			break
		}
	}
	if snapPath == "" {
		t.Skipf("no local snapshot found for host %s", host)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("UserHomeDir: %v", err)
	}
	rulesPath, _ := filepath.Abs("../cleanup_rules.toml")
	myRulesPath, _ := filepath.Abs("../my_cleanup_rules.toml")

	assertDiskMapParity(t, snapPath, home, rulesPath, myRulesPath)
}
