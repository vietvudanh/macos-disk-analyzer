package main

import (
	"bytes"
	"compress/gzip"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf16"
	"unicode/utf8"
)

type ScanMeta struct {
	ProgName  string
	ProgVer   string
	Timestamp string
}

type NodeMeta struct {
	ASize  uint64
	DSize  uint64
	NotReg bool
}

type Stats struct {
	TotalEntries uint64
}

func openSnapshotStream(path string) (io.Reader, func() error, error) {
	if strings.HasSuffix(path, ".zst") {
		cmd := exec.Command("zstd", "-dcf", path)
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return nil, nil, err
		}
		if err := cmd.Start(); err != nil {
			return nil, nil, err
		}
		return stdout, func() error {
			return cmd.Wait()
		}, nil
	}

	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}

	if strings.HasSuffix(path, ".gz") {
		gz, err := gzip.NewReader(f)
		if err != nil {
			_ = f.Close()
			return nil, nil, err
		}
		return gz, func() error {
			errGz := gz.Close()
			errF := f.Close()
			if errGz != nil {
				return errGz
			}
			return errF
		}, nil
	}

	return f, f.Close, nil
}

type jsonScanner struct {
	r       io.Reader
	buf     []byte
	pos     int
	end     int
	eof     bool
	keyBuf  []byte
	nameBuf []byte
	normBuf []byte
}

func newJSONScanner(r io.Reader) *jsonScanner {
	return &jsonScanner{
		r:       r,
		buf:     make([]byte, 1<<20),
		keyBuf:  make([]byte, 0, 64),
		nameBuf: make([]byte, 0, 256),
		normBuf: make([]byte, 0, 512),
	}
}

func (s *jsonScanner) fill() error {
	if s.eof {
		return io.EOF
	}
	for {
		n, err := s.r.Read(s.buf)
		if n > 0 {
			s.pos = 0
			s.end = n
			if err == io.EOF {
				s.eof = true
			}
			return nil
		}
		if err != nil {
			if err == io.EOF {
				s.eof = true
			}
			return err
		}
	}
}

func (s *jsonScanner) readByte() (byte, error) {
	if s.pos >= s.end {
		if err := s.fill(); err != nil {
			return 0, err
		}
	}
	b := s.buf[s.pos]
	s.pos++
	return b, nil
}

func (s *jsonScanner) peekNonWS() (byte, error) {
	for {
		for s.pos < s.end {
			b := s.buf[s.pos]
			if b != ' ' && b != '\t' && b != '\n' && b != '\r' {
				return b, nil
			}
			s.pos++
		}
		if err := s.fill(); err != nil {
			return 0, err
		}
	}
}

func (s *jsonScanner) nextNonWS() (byte, error) {
	b, err := s.peekNonWS()
	if err != nil {
		return 0, err
	}
	s.pos++
	return b, nil
}

func (s *jsonScanner) expectByte(want byte) error {
	b, err := s.nextNonWS()
	if err != nil {
		return err
	}
	if b != want {
		return fmt.Errorf("expected %q, got %q", want, b)
	}
	return nil
}

func (s *jsonScanner) readHex4() (rune, error) {
	var v rune
	for i := 0; i < 4; i++ {
		b, err := s.readByte()
		if err != nil {
			return 0, err
		}
		var d rune
		switch {
		case b >= '0' && b <= '9':
			d = rune(b - '0')
		case b >= 'a' && b <= 'f':
			d = rune(b - 'a' + 10)
		case b >= 'A' && b <= 'F':
			d = rune(b - 'A' + 10)
		default:
			return 0, fmt.Errorf("invalid hex digit %q in \\u escape", b)
		}
		v = (v << 4) | d
	}
	return v, nil
}

func (s *jsonScanner) readStringInto(dst *[]byte) error {
	*dst = (*dst)[:0]
	if err := s.expectByte('"'); err != nil {
		return err
	}
	for {
		if s.pos >= s.end {
			if err := s.fill(); err != nil {
				return err
			}
		}
		rem := s.buf[s.pos:s.end]
		q := bytes.IndexByte(rem, '"')
		if q >= 0 {
			bs := bytes.IndexByte(rem[:q], '\\')
			if bs == -1 {
				*dst = append(*dst, rem[:q]...)
				s.pos += q + 1
				return nil
			}
			*dst = append(*dst, rem[:bs]...)
			s.pos += bs + 1
		} else {
			bs := bytes.IndexByte(rem, '\\')
			if bs == -1 {
				*dst = append(*dst, rem...)
				s.pos = s.end
				continue
			}
			*dst = append(*dst, rem[:bs]...)
			s.pos += bs + 1
		}

		esc, err := s.readByte()
		if err != nil {
			return err
		}
		switch esc {
		case '"', '\\', '/':
			*dst = append(*dst, esc)
		case 'b':
			*dst = append(*dst, '\b')
		case 'f':
			*dst = append(*dst, '\f')
		case 'n':
			*dst = append(*dst, '\n')
		case 'r':
			*dst = append(*dst, '\r')
		case 't':
			*dst = append(*dst, '\t')
		case 'u':
			r1, err := s.readHex4()
			if err != nil {
				return err
			}
			ru := r1
			if utf16.IsSurrogate(ru) {
				b1, err := s.readByte()
				if err == nil && b1 == '\\' {
					b2, err := s.readByte()
					if err == nil && b2 == 'u' {
						if r2, err := s.readHex4(); err == nil {
							if combined := utf16.DecodeRune(ru, r2); combined != unicode.ReplacementChar {
								ru = combined
							}
						}
					}
				}
			}
			*dst = utf8.AppendRune(*dst, ru)
		default:
			*dst = append(*dst, esc)
		}
	}
}

func (s *jsonScanner) skipString() error {
	if err := s.expectByte('"'); err != nil {
		return err
	}
	for {
		if s.pos >= s.end {
			if err := s.fill(); err != nil {
				return err
			}
		}
		rem := s.buf[s.pos:s.end]
		q := bytes.IndexByte(rem, '"')
		if q >= 0 {
			bs := bytes.IndexByte(rem[:q], '\\')
			if bs == -1 {
				s.pos += q + 1
				return nil
			}
			s.pos += bs + 1
		} else {
			bs := bytes.IndexByte(rem, '\\')
			if bs == -1 {
				s.pos = s.end
				continue
			}
			s.pos += bs + 1
		}
		if _, err := s.readByte(); err != nil {
			return err
		}
	}
}

func (s *jsonScanner) skipValue() error {
	b, err := s.peekNonWS()
	if err != nil {
		return err
	}
	switch b {
	case '"':
		return s.skipString()
	case '{':
		s.pos++
		next, err := s.peekNonWS()
		if err != nil {
			return err
		}
		if next == '}' {
			s.pos++
			return nil
		}
		for {
			if err := s.skipString(); err != nil {
				return err
			}
			if err := s.expectByte(':'); err != nil {
				return err
			}
			if err := s.skipValue(); err != nil {
				return err
			}
			sep, err := s.nextNonWS()
			if err != nil {
				return err
			}
			if sep == '}' {
				return nil
			}
			if sep != ',' {
				return fmt.Errorf("expected ',' or '}' while skipping object, got %q", sep)
			}
		}
	case '[':
		s.pos++
		next, err := s.peekNonWS()
		if err != nil {
			return err
		}
		if next == ']' {
			s.pos++
			return nil
		}
		for {
			if err := s.skipValue(); err != nil {
				return err
			}
			sep, err := s.nextNonWS()
			if err != nil {
				return err
			}
			if sep == ']' {
				return nil
			}
			if sep != ',' {
				return fmt.Errorf("expected ',' or ']' while skipping array, got %q", sep)
			}
		}
	default:
		for {
			for s.pos < s.end {
				switch s.buf[s.pos] {
				case ' ', '\t', '\n', '\r', ',', '}', ']':
					return nil
				}
				s.pos++
			}
			if err := s.fill(); err != nil {
				if err == io.EOF {
					return nil
				}
				return err
			}
		}
	}
}

func (s *jsonScanner) readUint64() (uint64, error) {
	b, err := s.peekNonWS()
	if err != nil {
		return 0, err
	}
	if b < '0' || b > '9' {
		if err := s.skipValue(); err != nil {
			return 0, err
		}
		return 0, nil
	}
	var val uint64
	nonInt := false
	for {
		for s.pos < s.end {
			c := s.buf[s.pos]
			if c >= '0' && c <= '9' {
				val = val*10 + uint64(c-'0')
				s.pos++
				continue
			}
			if c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-' {
				nonInt = true
				s.pos++
				continue
			}
			if nonInt {
				return 0, nil
			}
			return val, nil
		}
		if err := s.fill(); err != nil {
			if err == io.EOF {
				if nonInt {
					return 0, nil
				}
				return val, nil
			}
			return 0, err
		}
	}
}

func (s *jsonScanner) readBool() (bool, error) {
	b, err := s.peekNonWS()
	if err != nil {
		return false, err
	}
	if b == 't' {
		return true, s.skipValue()
	}
	if b == 'f' {
		return false, s.skipValue()
	}
	return true, s.skipValue()
}

func (s *jsonScanner) readRawScalar() (string, error) {
	b, err := s.peekNonWS()
	if err != nil {
		return "", err
	}
	if b == '"' {
		var tmp []byte
		if err := s.readStringInto(&tmp); err != nil {
			return "", err
		}
		return strconv.Quote(string(tmp)), nil
	}
	var out []byte
	for {
		for s.pos < s.end {
			c := s.buf[s.pos]
			switch c {
			case ' ', '\t', '\n', '\r', ',', '}', ']':
				return string(out), nil
			}
			out = append(out, c)
			s.pos++
		}
		if err := s.fill(); err != nil {
			if err == io.EOF {
				return string(out), nil
			}
			return "", err
		}
	}
}

func (s *jsonScanner) readScanMeta() (ScanMeta, error) {
	var meta ScanMeta
	if err := s.expectByte('{'); err != nil {
		return meta, err
	}
	b, err := s.peekNonWS()
	if err != nil {
		return meta, err
	}
	if b == '}' {
		s.pos++
		return meta, nil
	}
	var valBuf []byte
	for {
		if err := s.readStringInto(&s.keyBuf); err != nil {
			return meta, err
		}
		if err := s.expectByte(':'); err != nil {
			return meta, err
		}
		switch string(s.keyBuf) {
		case "progname":
			if err = s.readStringInto(&valBuf); err == nil {
				meta.ProgName = string(valBuf)
			}
		case "progver":
			if err = s.readStringInto(&valBuf); err == nil {
				meta.ProgVer = string(valBuf)
			}
		case "timestamp":
			meta.Timestamp, err = s.readRawScalar()
		default:
			err = s.skipValue()
		}
		if err != nil {
			return meta, err
		}
		sep, err := s.nextNonWS()
		if err != nil {
			return meta, err
		}
		if sep == '}' {
			return meta, nil
		}
		if sep != ',' {
			return meta, fmt.Errorf("expected ',' or '}' in scan metadata, got %q", sep)
		}
	}
}

func (s *jsonScanner) readNodeMeta() (NodeMeta, error) {
	var meta NodeMeta
	s.nameBuf = s.nameBuf[:0]
	if err := s.expectByte('{'); err != nil {
		return meta, err
	}
	b, err := s.peekNonWS()
	if err != nil {
		return meta, err
	}
	if b == '}' {
		s.pos++
		return meta, nil
	}
	for {
		if err := s.readStringInto(&s.keyBuf); err != nil {
			return meta, err
		}
		if err := s.expectByte(':'); err != nil {
			return meta, err
		}
		switch string(s.keyBuf) {
		case "name":
			err = s.readStringInto(&s.nameBuf)
		case "asize":
			meta.ASize, err = s.readUint64()
		case "dsize":
			meta.DSize, err = s.readUint64()
		case "notreg":
			meta.NotReg, err = s.readBool()
		default:
			err = s.skipValue()
		}
		if err != nil {
			return meta, err
		}
		sep, err := s.nextNonWS()
		if err != nil {
			return meta, err
		}
		if sep == '}' {
			return meta, nil
		}
		if sep != ',' {
			return meta, fmt.Errorf("expected ',' or '}' in node metadata, got %q", sep)
		}
	}
}

func (s *jsonScanner) walkDir(
	buf *[]byte,
	table *pathTable,
	includeCloud bool,
	stats *Stats,
) error {
	if err := s.expectByte('['); err != nil {
		return err
	}
	next, err := s.peekNonWS()
	if err != nil {
		return err
	}
	if next == ']' {
		s.pos++
		return nil
	}

	meta, err := s.readNodeMeta()
	if err != nil {
		return err
	}

	prevLen := len(*buf)
	*buf = append(*buf, '/')
	*buf = append(*buf, s.nameBuf...)
	s.normBuf = normalizePathInto(*buf, s.normBuf)

	nextByte, err := s.peekNonWS()
	if err != nil {
		return err
	}
	hasChildren := nextByte == ','
	excluded := !includeCloud && isExcludedBytes(s.normBuf)
	isDir := hasChildren || meta.NotReg

	stats.TotalEntries++
	if !excluded {
		sz := meta.DSize
		if sz == 0 {
			sz = meta.ASize
		}
		table.upsert(s.normBuf, int64(sz), isDir)
	}

	for {
		nextByte, err = s.peekNonWS()
		if err != nil {
			return err
		}
		if nextByte != ',' {
			break
		}
		s.pos++

		childFirst, err := s.peekNonWS()
		if err != nil {
			return err
		}
		switch childFirst {
		case '[':
			if err := s.walkDir(buf, table, includeCloud, stats); err != nil {
				return err
			}
		case '{':
			cmeta, err := s.readNodeMeta()
			if err != nil {
				return err
			}
			prev := len(*buf)
			*buf = append(*buf, '/')
			*buf = append(*buf, s.nameBuf...)
			s.normBuf = normalizePathInto(*buf, s.normBuf)
			stats.TotalEntries++
			if includeCloud || !isExcludedBytes(s.normBuf) {
				sz := cmeta.DSize
				if sz == 0 {
					sz = cmeta.ASize
				}
				table.upsert(s.normBuf, int64(sz), false)
			}
			*buf = (*buf)[:prev]
		default:
			if err := s.skipValue(); err != nil {
				return err
			}
		}
	}

	if err := s.expectByte(']'); err != nil {
		return err
	}
	*buf = (*buf)[:prevLen]
	return nil
}

func parseSnapshot(
	r io.Reader,
	table *pathTable,
	includeCloud bool,
) (ScanMeta, Stats, error) {
	var scanMeta ScanMeta
	var stats Stats
	s := newJSONScanner(r)

	if err := s.expectByte('['); err != nil {
		return scanMeta, stats, err
	}
	if err := s.skipValue(); err != nil {
		return scanMeta, stats, err
	}
	if err := s.expectByte(','); err != nil {
		return scanMeta, stats, err
	}
	if err := s.skipValue(); err != nil {
		return scanMeta, stats, err
	}
	if err := s.expectByte(','); err != nil {
		return scanMeta, stats, err
	}
	scanMeta, err := s.readScanMeta()
	if err != nil {
		return scanMeta, stats, err
	}
	fmt.Printf("ncdu version: %s %s\n", scanMeta.ProgName, scanMeta.ProgVer)
	fmt.Printf("Scan timestamp: %s\n", scanMeta.Timestamp)
	fmt.Println("Parsing directory tree...")

	if err := s.expectByte(','); err != nil {
		return scanMeta, stats, err
	}

	buf := make([]byte, 0, 256)
	if err := s.walkDir(&buf, table, includeCloud, &stats); err != nil {
		return scanMeta, stats, err
	}
	_ = s.expectByte(']')
	_, _ = io.Copy(io.Discard, s.r)

	return scanMeta, stats, nil
}
