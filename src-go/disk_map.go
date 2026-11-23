package main

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	bigFileSize uint64 = 20 * 1024 * 1024 // files at least this big are kept as their own map tiles
	noMatch     int    = 1 << 30
)

var (
	projectMarkers = map[string]struct{}{
		"Cargo.toml":     {},
		"package.json":   {},
		"pyproject.toml": {},
		"go.mod":         {},
		"pom.xml":        {},
		"build.gradle":   {},
	}

	tierOrder = map[string]int{
		"mine":   0,
		"safe":   1,
		"review": 2,
		"keep":   3,
	}

	shlexSafeRe = regexp.MustCompile(`^[a-zA-Z0-9_@%+=:,./-]+$`)
)

// DiskNode represents a directory or big file in the disk usage tree.
type DiskNode struct {
	name    string
	own     uint64
	kids    map[string]*DiskNode
	size    uint64
	isFile  bool
	markers map[string]struct{}
	match   int
	best    int
}

func newDiskNode(name string, isFile bool) *DiskNode {
	return &DiskNode{
		name:   name,
		isFile: isFile,
		match:  noMatch,
		best:   noMatch,
	}
}

func joinPath(parent, name string) string {
	if parent == "/" {
		return "/" + name
	}
	return parent + "/" + name
}

// --- streaming ncdu JSON -> DiskNode tree -----------------------------------

func (s *jsonScanner) skipArrayRemainder() error {
	for {
		b, err := s.peekNonWS()
		if err != nil {
			return err
		}
		if b != ',' {
			break
		}
		s.pos++
		if err := s.skipValue(); err != nil {
			return err
		}
	}
	return s.expectByte(']')
}

func (s *jsonScanner) walkDiskMapDir(path string, isRoot bool, includeCloud bool) (*DiskNode, error) {
	if err := s.expectByte('['); err != nil {
		return nil, err
	}
	next, err := s.peekNonWS()
	if err != nil {
		return nil, err
	}
	if next == ']' {
		s.pos++
		return nil, nil
	}

	meta, err := s.readNodeMeta()
	if err != nil {
		return nil, err
	}
	name := string(s.nameBuf)

	curPath := path
	if !isRoot {
		curPath = joinPath(path, name)
		if !includeCloud && strings.Contains(curPath, "/Library/CloudStorage") {
			if err := s.skipArrayRemainder(); err != nil {
				return nil, err
			}
			return nil, nil
		}
	}

	node := newDiskNode(name, false)
	node.own = meta.DSize

	for {
		nextByte, err := s.peekNonWS()
		if err != nil {
			return nil, err
		}
		if nextByte != ',' {
			break
		}
		s.pos++

		childFirst, err := s.peekNonWS()
		if err != nil {
			return nil, err
		}
		switch childFirst {
		case '[':
			child, err := s.walkDiskMapDir(curPath, false, includeCloud)
			if err != nil {
				return nil, err
			}
			if child != nil {
				addChild(node, child)
			}
		case '{':
			cmeta, err := s.readNodeMeta()
			if err != nil {
				return nil, err
			}
			if cmeta.Hlnkc {
				if _, seen := s.seenInodes[cmeta.Ino]; seen {
					continue
				}
				s.seenInodes[cmeta.Ino] = struct{}{}
			}
			cname := string(s.nameBuf)
			if _, isMarker := projectMarkers[cname]; isMarker {
				if node.markers == nil {
					node.markers = make(map[string]struct{}, 2)
				}
				node.markers[cname] = struct{}{}
			}
			if cmeta.DSize >= bigFileSize {
				leaf := newDiskNode(cname, true)
				leaf.own = cmeta.DSize
				addChild(node, leaf)
			} else {
				node.own += cmeta.DSize
			}
		default:
			if err := s.skipValue(); err != nil {
				return nil, err
			}
		}
	}

	if err := s.expectByte(']'); err != nil {
		return nil, err
	}
	return node, nil
}

func parseDiskMapSnapshot(r io.Reader, includeCloud bool) (ScanMeta, *DiskNode, error) {
	var scanMeta ScanMeta
	s := newJSONScanner(r)

	if err := s.expectByte('['); err != nil {
		return scanMeta, nil, err
	}
	if err := s.skipValue(); err != nil {
		return scanMeta, nil, err
	}
	if err := s.expectByte(','); err != nil {
		return scanMeta, nil, err
	}
	if err := s.skipValue(); err != nil {
		return scanMeta, nil, err
	}
	if err := s.expectByte(','); err != nil {
		return scanMeta, nil, err
	}
	scanMeta, err := s.readScanMeta()
	if err != nil {
		return scanMeta, nil, err
	}
	if err := s.expectByte(','); err != nil {
		return scanMeta, nil, err
	}

	root, err := s.walkDiskMapDir("/", true, includeCloud)
	if err != nil {
		return scanMeta, nil, err
	}
	_ = s.expectByte(']')
	_, _ = io.Copy(io.Discard, s.r)
	if root == nil {
		root = newDiskNode("/", false)
	}
	return scanMeta, root, nil
}

func addChild(parent, child *DiskNode) {
	if parent.kids == nil {
		parent.kids = make(map[string]*DiskNode, 4)
	}
	existing, ok := parent.kids[child.name]
	if !ok {
		parent.kids[child.name] = child
		return
	}
	mergeNodes(existing, child)
}

func mergeNodes(a, b *DiskNode) {
	if b.own > a.own {
		a.own = b.own
	}
	if len(b.markers) > 0 {
		if a.markers == nil {
			a.markers = make(map[string]struct{}, len(b.markers))
		}
		for m := range b.markers {
			a.markers[m] = struct{}{}
		}
	}
	for name, kid := range b.kids {
		if a.kids == nil {
			a.kids = make(map[string]*DiskNode, len(b.kids))
		}
		mine, ok := a.kids[name]
		if !ok {
			a.kids[name] = kid
		} else if mine.isFile || kid.isFile {
			if kid.own > mine.own {
				a.kids[name] = kid
			}
		} else {
			mergeNodes(mine, kid)
		}
	}
}

func foldDataVolume(root *DiskNode) {
	sys := root.kids["System"]
	if sys == nil {
		return
	}
	volumes := sys.kids["Volumes"]
	if volumes == nil {
		return
	}
	data := volumes.kids["Data"]
	if data == nil {
		return
	}
	delete(volumes.kids, "Data")
	for _, kid := range data.kids {
		addChild(root, kid)
	}
}

func computeSizes(node *DiskNode) uint64 {
	if node == nil {
		return 0
	}
	total := node.own
	for _, kid := range node.kids {
		total += computeSizes(kid)
	}
	node.size = total
	return total
}

// --- glob & matcher ---------------------------------------------------------

func globToRegex(pattern, home string) string {
	if strings.HasPrefix(pattern, "~") {
		pattern = home + pattern[1:]
	} else if !strings.HasPrefix(pattern, "/") {
		pattern = "**/" + pattern
	}

	var out strings.Builder
	out.Grow(len(pattern) * 2)
	for i := 0; i < len(pattern); {
		if strings.HasPrefix(pattern[i:], "**/") {
			out.WriteString("(?:.*/)?")
			i += 3
		} else if strings.HasPrefix(pattern[i:], "**") {
			out.WriteString(".*")
			i += 2
		} else if pattern[i] == '*' {
			out.WriteString("[^/]*")
			i++
		} else if pattern[i] == '?' {
			out.WriteString("[^/]")
			i++
		} else if pattern[i] == '{' {
			end := strings.IndexByte(pattern[i:], '}')
			if end != -1 {
				options := strings.Split(pattern[i+1:i+end], ",")
				out.WriteString("(?:")
				for j, opt := range options {
					if j > 0 {
						out.WriteByte('|')
					}
					out.WriteString(regexp.QuoteMeta(opt))
				}
				out.WriteByte(')')
				i += end + 1
			} else {
				out.WriteString(regexp.QuoteMeta("{"))
				i++
			}
		} else {
			out.WriteString(regexp.QuoteMeta(pattern[i : i+1]))
			i++
		}
	}
	return out.String()
}

type matcherEntry struct {
	owner int
	glob  string
}

type compiledGlob struct {
	idx    int
	prefix string
	re     *regexp.Regexp
}

type Matcher struct {
	owners []int
	exact  map[string][]int
	byBase map[string][]int
	other  []compiledGlob
}

func newMatcher(entries []matcherEntry, home string) (*Matcher, error) {
	m := &Matcher{
		owners: make([]int, len(entries)),
		exact:  make(map[string][]int),
		byBase: make(map[string][]int),
	}
	for k, e := range entries {
		m.owners[k] = e.owner
		pat := e.glob
		if !strings.HasPrefix(pat, "~") && !strings.HasPrefix(pat, "/") &&
			len(pat) > 0 && !strings.ContainsAny(pat, "/*?{") {
			m.byBase[pat] = append(m.byBase[pat], k)
			continue
		}
		expanded := pat
		if strings.HasPrefix(expanded, "~") {
			expanded = home + expanded[1:]
		} else if !strings.HasPrefix(expanded, "/") {
			expanded = "**/" + expanded
		}
		if !strings.ContainsAny(expanded, "*?{") {
			m.exact[expanded] = append(m.exact[expanded], k)
			continue
		}
		prefixEnd := strings.IndexAny(expanded, "*?{")
		prefix := ""
		if prefixEnd > 0 {
			prefix = expanded[:prefixEnd]
		}
		reStr := globToRegex(pat, home)
		re, err := regexp.Compile("^(?:" + reStr + ")$")
		if err != nil {
			return nil, fmt.Errorf("compile glob %q: %w", e.glob, err)
		}
		m.other = append(m.other, compiledGlob{
			idx:    k,
			prefix: prefix,
			re:     re,
		})
	}
	return m, nil
}

func (m *Matcher) first(path string, accept func(int) bool) (int, bool) {
	bestK := len(m.owners)
	if idxs := m.exact[path]; len(idxs) > 0 {
		for _, k := range idxs {
			if k >= bestK {
				break
			}
			if accept == nil || accept(m.owners[k]) {
				bestK = k
				break
			}
		}
	}
	if len(m.byBase) > 0 {
		base := path
		if slash := strings.LastIndexByte(path, '/'); slash >= 0 {
			base = path[slash+1:]
		}
		if idxs := m.byBase[base]; len(idxs) > 0 {
			for _, k := range idxs {
				if k >= bestK {
					break
				}
				if accept == nil || accept(m.owners[k]) {
					bestK = k
					break
				}
			}
		}
	}
	for i := range m.other {
		o := &m.other[i]
		if o.idx >= bestK {
			break
		}
		if o.prefix != "" && !strings.HasPrefix(path, o.prefix) {
			continue
		}
		if o.re.MatchString(path) && (accept == nil || accept(m.owners[o.idx])) {
			bestK = o.idx
			break
		}
	}
	if bestK == len(m.owners) {
		return 0, false
	}
	return m.owners[bestK], true
}

// --- TOML rule & category loading -------------------------------------------

type Rule struct {
	Label   string   `json:"label"`
	Tier    string   `json:"tier"`
	Source  string   `json:"source"`
	Paths   []string `json:"paths"`
	Note    string   `json:"note"`
	Command string   `json:"command"`
	Needs   []string `json:"-"`
	MinSize uint64   `json:"-"`
}

type Category struct {
	Name  string
	Paths []string
}

type ruleFileSpec struct {
	path   string
	source string
}

func loadRules(specs []ruleFileSpec) ([]Rule, []Category, error) {
	var rules []Rule
	var categories []Category

	for _, spec := range specs {
		data, err := os.ReadFile(spec.path)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			return nil, nil, err
		}
		rawRules, rawCats, err := parseRulesTOML(string(data))
		if err != nil {
			return nil, nil, fmt.Errorf("error in %s: %w", spec.path, err)
		}
		for _, r := range rawRules {
			paths := toStringSlice(r["paths"])
			if len(paths) == 0 {
				paths = toStringSlice(r["path"])
			}
			tier := toString(r["tier"])
			if tier == "" {
				if spec.source == "mine" {
					tier = "mine"
				} else {
					tier = "safe"
				}
			}
			if _, ok := tierOrder[tier]; !ok {
				return nil, nil, fmt.Errorf("%s: rule %q has unknown tier %q", spec.path, toString(r["label"]), tier)
			}
			label := toString(r["label"])
			if label == "" {
				if len(paths) > 0 {
					label = paths[0]
				} else {
					label = "?"
				}
			}
			minSizeMB := 1.0
			if v, ok := r["min_size_mb"]; ok {
				if f, ok := v.(float64); ok {
					minSizeMB = f
				}
			}
			if minSizeMB < 0 {
				minSizeMB = 0
			}
			rules = append(rules, Rule{
				Label:   label,
				Tier:    tier,
				Source:  spec.source,
				Paths:   paths,
				Note:    toString(r["note"]),
				Command: toString(r["command"]),
				Needs:   toStringSlice(r["needs"]),
				MinSize: uint64(minSizeMB * 1024 * 1024),
			})
		}
		for _, c := range rawCats {
			categories = append(categories, Category{
				Name:  toString(c["name"]),
				Paths: toStringSlice(c["paths"]),
			})
		}
	}
	return rules, categories, nil
}

func toString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func toStringSlice(v any) []string {
	switch x := v.(type) {
	case string:
		if x == "" {
			return nil
		}
		return []string{x}
	case []string:
		return x
	default:
		return nil
	}
}

// parseRulesTOML parses the subset of TOML used by cleanup_rules.toml and my_cleanup_rules.toml:
// [[rule]] and [[category]] array-of-tables with string, string-array (single/multi-line),
// and numeric values, plus '#' comments.
func parseRulesTOML(src string) ([]map[string]any, []map[string]any, error) {
	var rules []map[string]any
	var categories []map[string]any
	var current map[string]any

	p := tomlParser{src: src}
	for {
		p.skipWSAndComments()
		if p.eof() {
			break
		}
		if strings.HasPrefix(p.src[p.pos:], "[[") {
			p.pos += 2
			p.skipInlineWS()
			name, err := p.readIdent()
			if err != nil {
				return nil, nil, err
			}
			p.skipInlineWS()
			if !strings.HasPrefix(p.src[p.pos:], "]]") {
				return nil, nil, fmt.Errorf("line %d: expected ']]'", p.line())
			}
			p.pos += 2
			current = make(map[string]any)
			switch name {
			case "rule":
				rules = append(rules, current)
			case "category":
				categories = append(categories, current)
			default:
				// ignore other tables cleanly
			}
			continue
		}
		key, err := p.readIdent()
		if err != nil {
			return nil, nil, err
		}
		p.skipInlineWS()
		if p.eof() || p.src[p.pos] != '=' {
			return nil, nil, fmt.Errorf("line %d: expected '=' after key %q", p.line(), key)
		}
		p.pos++
		p.skipInlineWS()
		val, err := p.readValue()
		if err != nil {
			return nil, nil, err
		}
		if current != nil {
			current[key] = val
		}
	}
	return rules, categories, nil
}

type tomlParser struct {
	src string
	pos int
}

func (p *tomlParser) eof() bool { return p.pos >= len(p.src) }

func (p *tomlParser) line() int {
	return 1 + strings.Count(p.src[:p.pos], "\n")
}

func (p *tomlParser) skipInlineWS() {
	for !p.eof() && (p.src[p.pos] == ' ' || p.src[p.pos] == '\t') {
		p.pos++
	}
}

func (p *tomlParser) skipWSAndComments() {
	for !p.eof() {
		c := p.src[p.pos]
		if c == ' ' || c == '\t' || c == '\r' || c == '\n' {
			p.pos++
		} else if c == '#' {
			for !p.eof() && p.src[p.pos] != '\n' {
				p.pos++
			}
		} else {
			break
		}
	}
}

func (p *tomlParser) readIdent() (string, error) {
	start := p.pos
	for !p.eof() {
		c := p.src[p.pos]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' {
			p.pos++
		} else {
			break
		}
	}
	if p.pos == start {
		return "", fmt.Errorf("line %d: unexpected character %q", p.line(), p.src[p.pos])
	}
	return p.src[start:p.pos], nil
}

func (p *tomlParser) readString() (string, error) {
	if p.eof() {
		return "", fmt.Errorf("line %d: unexpected EOF in string", p.line())
	}
	quote := p.src[p.pos]
	if quote == '\'' {
		p.pos++
		start := p.pos
		for !p.eof() && p.src[p.pos] != '\'' {
			p.pos++
		}
		if p.eof() {
			return "", fmt.Errorf("line %d: unterminated literal string", p.line())
		}
		out := p.src[start:p.pos]
		p.pos++
		return out, nil
	}
	if quote != '"' {
		return "", fmt.Errorf("line %d: expected string quote, got %q", p.line(), quote)
	}
	p.pos++
	var b strings.Builder
	for !p.eof() {
		c := p.src[p.pos]
		p.pos++
		if c == '"' {
			return b.String(), nil
		}
		if c == '\\' {
			if p.eof() {
				return "", fmt.Errorf("line %d: unterminated escape", p.line())
			}
			esc := p.src[p.pos]
			p.pos++
			switch esc {
			case '"', '\\':
				b.WriteByte(esc)
			case 'n':
				b.WriteByte('\n')
			case 'r':
				b.WriteByte('\r')
			case 't':
				b.WriteByte('\t')
			case 'b':
				b.WriteByte('\b')
			case 'f':
				b.WriteByte('\f')
			default:
				b.WriteByte(esc)
			}
		} else {
			b.WriteByte(c)
		}
	}
	return "", fmt.Errorf("line %d: unterminated string", p.line())
}

func (p *tomlParser) readValue() (any, error) {
	if p.eof() {
		return nil, fmt.Errorf("line %d: expected value", p.line())
	}
	c := p.src[p.pos]
	if c == '"' || c == '\'' {
		return p.readString()
	}
	if c == '[' {
		p.pos++
		var arr []string
		for {
			p.skipWSAndComments()
			if p.eof() {
				return nil, fmt.Errorf("line %d: unterminated array", p.line())
			}
			if p.src[p.pos] == ']' {
				p.pos++
				return arr, nil
			}
			s, err := p.readString()
			if err != nil {
				return nil, err
			}
			arr = append(arr, s)
			p.skipWSAndComments()
			if !p.eof() && p.src[p.pos] == ',' {
				p.pos++
			}
		}
	}
	start := p.pos
	for !p.eof() {
		ch := p.src[p.pos]
		if ch == '\n' || ch == '\r' || ch == '#' || ch == ' ' || ch == '\t' {
			break
		}
		p.pos++
	}
	raw := strings.ReplaceAll(p.src[start:p.pos], "_", "")
	if raw == "true" {
		return true, nil
	}
	if raw == "false" {
		return false, nil
	}
	f, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return nil, fmt.Errorf("line %d: invalid value %q", p.line(), raw)
	}
	return f, nil
}

// --- rule annotation & candidate collection ---------------------------------

type Candidate struct {
	Path    string
	Size    uint64
	RuleIdx int
	Whole   bool
}

func (c Candidate) MarshalJSON() ([]byte, error) {
	return json.Marshal([]any{c.Path, c.Size, c.RuleIdx, c.Whole})
}

func annotate(node *DiskNode, path string, parent *DiskNode, matcher *Matcher, rules []Rule) int {
	accept := func(idx int) bool {
		needs := rules[idx].Needs
		if len(needs) == 0 {
			return true
		}
		if parent == nil {
			return false
		}
		for _, n := range needs {
			if _, ok := parent.kids[n]; ok {
				return true
			}
			if _, ok := parent.markers[n]; ok {
				return true
			}
		}
		return false
	}

	hit, ok := matcher.first(path, accept)
	if ok {
		node.match = hit
	} else {
		node.match = noMatch
	}

	best := noMatch
	if ok && rules[hit].Source == "mine" {
		best = node.match
	}
	for _, kid := range node.kids {
		b := annotate(kid, joinPath(path, kid.name), node, matcher, rules)
		if b < best {
			best = b
		}
	}
	node.best = best
	return best
}

func collect(node *DiskNode, path string, governing int, rules []Rule, out *[]Candidate) {
	if node.match != noMatch && (governing == noMatch || (node.match < governing && rules[node.match].Source == "mine")) {
		governing = node.match
	}
	if governing == noMatch {
		for _, kid := range node.kids {
			collect(kid, joinPath(path, kid.name), noMatch, rules, out)
		}
		return
	}
	if node.best >= governing {
		rule := rules[governing]
		if rule.Tier != "keep" && node.size >= rule.MinSize {
			*out = append(*out, Candidate{
				Path:    path,
				Size:    node.size,
				RuleIdx: governing,
				Whole:   node.match == governing,
			})
		}
		return
	}
	for _, kid := range node.kids {
		collect(kid, joinPath(path, kid.name), governing, rules, out)
	}
}

// --- serialization & shell script output ------------------------------------

type SerializedNode struct {
	N string           `json:"n"`
	S uint64           `json:"s"`
	C *int             `json:"c,omitempty"`
	F int              `json:"f,omitempty"`
	R *int             `json:"r,omitempty"`
	K []SerializedNode `json:"k,omitempty"`
}

func serializeTree(
	node *DiskNode,
	path string,
	parentCat int,
	catMatcher *Matcher,
	candRule map[string]int,
	minAbs uint64,
	maxKids int,
) SerializedNode {
	cat, ok := catMatcher.first(path, nil)
	if !ok {
		cat = parentCat
	}
	out := SerializedNode{
		N: node.name,
		S: node.size,
	}
	if cat != parentCat {
		c := cat
		out.C = &c
	}
	if node.isFile {
		out.F = 1
	}
	if r, exists := candRule[path]; exists {
		rv := r
		out.R = &rv
	}

	floor := minAbs
	if rel := uint64(float64(node.size) * 0.002); rel > floor {
		floor = rel
	}

	var kids []*DiskNode
	for _, k := range node.kids {
		if k.size >= floor {
			kids = append(kids, k)
		}
	}
	slices.SortFunc(kids, func(a, b *DiskNode) int {
		if c := cmp.Compare(b.size, a.size); c != 0 {
			return c
		}
		return cmp.Compare(a.name, b.name)
	})
	if len(kids) > maxKids {
		kids = kids[:maxKids]
	}
	if len(kids) > 0 {
		out.K = make([]SerializedNode, len(kids))
		for i, k := range kids {
			out.K[i] = serializeTree(k, joinPath(path, k.name), cat, catMatcher, candRule, minAbs, maxKids)
		}
	}
	return out
}

func shlexQuote(s string) string {
	if s == "" {
		return "''"
	}
	if shlexSafeRe.MatchString(s) {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

func fmtGB(n uint64) string {
	return fmt.Sprintf("%.2f GB", float64(n)/(1024*1024*1024))
}

func buildShellScript(rules []Rule, candidates []Candidate) string {
	lines := []string{
		"#!/bin/sh",
		"# Generated by disk_map.go. Review every line before running:",
		"# this permanently deletes data. 'review' items are commented out.",
		"set -u",
	}

	byRule := make(map[int][]Candidate)
	for _, c := range candidates {
		byRule[c.RuleIdx] = append(byRule[c.RuleIdx], c)
	}

	order := make([]int, 0, len(byRule))
	sumByRule := make(map[int]uint64, len(byRule))
	for idx, items := range byRule {
		order = append(order, idx)
		var total uint64
		for _, it := range items {
			total += it.Size
		}
		sumByRule[idx] = total
	}
	slices.SortFunc(order, func(a, b int) int {
		if c := cmp.Compare(tierOrder[rules[a].Tier], tierOrder[rules[b].Tier]); c != 0 {
			return c
		}
		if c := cmp.Compare(sumByRule[b], sumByRule[a]); c != 0 {
			return c
		}
		return cmp.Compare(a, b)
	})

	for _, idx := range order {
		rule := rules[idx]
		items := byRule[idx]
		prefix := ""
		if rule.Tier == "review" {
			prefix = "# "
		}
		lines = append(lines, "", fmt.Sprintf("# %s [%s] - %s", rule.Label, rule.Tier, fmtGB(sumByRule[idx])))
		if rule.Note != "" {
			lines = append(lines, "# "+rule.Note)
		}
		cmd := rule.Command
		allWhole := true
		for _, it := range items {
			if !it.Whole {
				allWhole = false
				break
			}
		}
		if cmd != "" && !strings.Contains(cmd, "{path}") && !strings.Contains(cmd, "{parent}") && allWhole {
			lines = append(lines, prefix+cmd)
			continue
		}
		for _, it := range items {
			var line string
			if cmd != "" && it.Whole && (strings.Contains(cmd, "{path}") || strings.Contains(cmd, "{parent}")) {
				parent := "/"
				if slash := strings.LastIndexByte(it.Path, '/'); slash > 0 {
					parent = it.Path[:slash]
				}
				line = strings.ReplaceAll(cmd, "{path}", shlexQuote(it.Path))
				line = strings.ReplaceAll(line, "{parent}", shlexQuote(parent))
			} else {
				line = "rm -rf -- " + shlexQuote(it.Path)
			}
			lines = append(lines, fmt.Sprintf("%s%s   # %s", prefix, line, fmtGB(it.Size)))
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

type DiskUsageInfo struct {
	Total uint64 `json:"total"`
	Free  uint64 `json:"free"`
}

func getLocalDiskUsage() *DiskUsageInfo {
	target := "/"
	if _, err := os.Stat("/System/Volumes/Data"); err == nil {
		target = "/System/Volumes/Data"
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(target, &st); err != nil {
		return nil
	}
	bsize := uint64(st.Bsize)
	return &DiskUsageInfo{
		Total: st.Blocks * bsize,
		Free:  st.Bavail * bsize,
	}
}

type DiskMapPayload struct {
	Meta       DiskMapMeta    `json:"meta"`
	Categories []string       `json:"categories"`
	Rules      []Rule         `json:"rules"`
	Candidates []Candidate    `json:"candidates"`
	Tree       SerializedNode `json:"tree"`
}

type DiskMapMeta struct {
	Host        string         `json:"host"`
	Home        string         `json:"home"`
	ScannedAt   any            `json:"scanned_at"`
	GeneratedAt int64          `json:"generated_at"`
	Disk        *DiskUsageInfo `json:"disk"`
}

func resolveRepoFile(name string) string {
	if _, err := os.Stat(name); err == nil {
		return name
	}
	parent := filepath.Join("..", name)
	if _, err := os.Stat(parent); err == nil {
		return parent
	}
	return name
}

func parseTimestamp(raw string) any {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "null" {
		return nil
	}
	if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return n
	}
	if unq, err := strconv.Unquote(raw); err == nil {
		return unq
	}
	return raw
}

func runDiskMap(args []string) error {
	homeDefault, err := os.UserHomeDir()
	if err != nil {
		homeDefault = ""
	}
	localHost := defaultHost()

	fs := flag.NewFlagSet("disk-map", flag.ContinueOnError)
	host := fs.String("host", localHost, "data/<host>/ to read and write")
	home := fs.String("home", homeDefault, "home folder of the scanned machine")
	rulesPath := fs.String("rules", resolveRepoFile("cleanup_rules.toml"), "path to built-in cleanup rules TOML")
	myRulesPath := fs.String("my-rules", resolveRepoFile("my_cleanup_rules.toml"), "path to personal cleanup rules TOML")
	includeCloud := fs.Bool("include-cloudstorage", false, "include ~/Library/CloudStorage (cloud placeholders)")
	minTileMB := fs.Float64("min-tile-mb", 5, "smallest folder drawn on the map")
	if err := fs.Parse(args); err != nil {
		return err
	}

	findSnapshot := func(dir string) string {
		for _, name := range []string{"disk_usage.json.zst", "disk_usage.json.gz", "disk_usage.json"} {
			c := filepath.Join(dir, name)
			if _, err := os.Stat(c); err == nil {
				return c
			}
		}
		return ""
	}

	dataDir := filepath.Join("data", *host)
	inputFile := findSnapshot(dataDir)
	if inputFile == "" {
		alt := filepath.Join("..", "data", *host)
		if f := findSnapshot(alt); f != "" {
			dataDir = alt
			inputFile = f
		}
	}
	if inputFile == "" {
		return fmt.Errorf("no snapshot in %s. Run: make snapshot", dataDir)
	}

	t0 := time.Now()
	fmt.Printf("Loading %s ...\n", inputFile)
	r, cleanup, err := openSnapshotStream(inputFile)
	if err != nil {
		return fmt.Errorf("open snapshot: %w", err)
	}

	fmt.Println("Building tree ...")
	scanMeta, root, parseErr := parseDiskMapSnapshot(r, *includeCloud)
	closeErr := cleanup()
	if parseErr != nil {
		return fmt.Errorf("parse ncdu json: %w", parseErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close snapshot stream: %w", closeErr)
	}

	foldDataVolume(root)
	computeSizes(root)

	rules, categories, err := loadRules([]ruleFileSpec{
		{path: *myRulesPath, source: "mine"},
		{path: *rulesPath, source: "builtin"},
	})
	if err != nil {
		return err
	}

	fmt.Printf("Matching %d rules ...\n", len(rules))
	var ruleEntries []matcherEntry
	for i, rl := range rules {
		for _, g := range rl.Paths {
			ruleEntries = append(ruleEntries, matcherEntry{owner: i, glob: g})
		}
	}
	matcher, err := newMatcher(ruleEntries, *home)
	if err != nil {
		return err
	}

	annotate(root, "/", nil, matcher, rules)
	candidates := make([]Candidate, 0)
	collect(root, "/", noMatch, rules, &candidates)
	slices.SortFunc(candidates, func(a, b Candidate) int {
		if c := cmp.Compare(b.Size, a.Size); c != 0 {
			return c
		}
		return cmp.Compare(a.Path, b.Path)
	})

	var catEntries []matcherEntry
	for i, c := range categories {
		for _, g := range c.Paths {
			catEntries = append(catEntries, matcherEntry{owner: i, glob: g})
		}
	}
	catMatcher, err := newMatcher(catEntries, *home)
	if err != nil {
		return err
	}
	categories = append(categories, Category{Name: "Other"})
	otherIdx := len(categories) - 1

	candRule := make(map[string]int, len(candidates))
	for _, c := range candidates {
		candRule[c.Path] = c.RuleIdx
	}
	minAbs := uint64(*minTileMB * 1024 * 1024)
	tree := serializeTree(root, "/", otherIdx, catMatcher, candRule, minAbs, 60)
	tree.C = &otherIdx

	var disk *DiskUsageInfo
	if *host == localHost {
		disk = getLocalDiskUsage()
	}

	catNames := make([]string, len(categories))
	for i, c := range categories {
		catNames[i] = c.Name
	}

	payload := DiskMapPayload{
		Meta: DiskMapMeta{
			Host:        *host,
			Home:        *home,
			ScannedAt:   parseTimestamp(scanMeta.Timestamp),
			GeneratedAt: time.Now().Unix(),
			Disk:        disk,
		},
		Categories: catNames,
		Rules:      rules,
		Candidates: candidates,
		Tree:       tree,
	}

	templatePath := resolveRepoFile("disk_map_template.html")
	templateBytes, err := os.ReadFile(templatePath)
	if err != nil {
		return fmt.Errorf("read template %s: %w", templatePath, err)
	}

	var blobBuf bytes.Buffer
	enc := json.NewEncoder(&blobBuf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(payload); err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}
	blob := bytes.TrimSuffix(blobBuf.Bytes(), []byte("\n"))
	blob = bytes.ReplaceAll(blob, []byte("</"), []byte(`<\/`))

	htmlOut := bytes.Replace(templateBytes, []byte("/*__DATA__*/null"), blob, 1)
	htmlFile := filepath.Join(dataDir, "disk_map.html")
	if err := os.WriteFile(htmlFile, htmlOut, 0o644); err != nil {
		return fmt.Errorf("write %s: %w", htmlFile, err)
	}

	scriptFile := filepath.Join(dataDir, "cleanup_suggested.sh")
	scriptContent := buildShellScript(rules, candidates)
	if err := os.WriteFile(scriptFile, []byte(scriptContent), 0o755); err != nil {
		return fmt.Errorf("write %s: %w", scriptFile, err)
	}

	totals := make(map[string]uint64)
	for _, c := range candidates {
		tier := rules[c.RuleIdx].Tier
		totals[tier] += c.Size
	}
	fmt.Printf("\nScanned %s in %.1fs\n", fmtGB(root.size), time.Since(t0).Seconds())
	for _, item := range [][2]string{
		{"mine", "Your list"},
		{"safe", "Safe to remove"},
		{"review", "Needs review"},
	} {
		fmt.Printf("  %-15s %10s\n", item[1], fmtGB(totals[item[0]]))
	}
	fmt.Printf("\nMap:    %s\nScript: %s\n", htmlFile, scriptFile)
	fmt.Printf("Open with: open '%s'\n", htmlFile)
	return nil
}
