package main

import (
	"bytes"
	"encoding/binary"
	"math/bits"
	"runtime"
	"slices"
	"sync"
)

const (
	slabShift = 23 // 8 MiB slabs
	slabSize  = 1 << slabShift
	slabMask  = slabSize - 1
)

type pathEntry struct {
	off  uint32 // high bits: slab index, low 23 bits: offset inside slab
	n    uint32
	size int64 // -1 means directory-only; >= 0 means file size in bytes
}

type hashBucket struct {
	hash uint32
	idx  uint32 // 1-based index into entries; 0 means empty
}

type pathTable struct {
	slabs   [][]byte
	curSlab []byte
	curOff  int
	entries []pathEntry
	buckets []hashBucket
	files   int
}

func newPathTable(expectedEntries int) *pathTable {
	capBuckets := 1 << 23 // 8,388,608 buckets (64 MiB)
	for capBuckets < expectedEntries*2 {
		capBuckets <<= 1
	}
	first := make([]byte, slabSize)
	return &pathTable{
		slabs:   [][]byte{first},
		curSlab: first,
		entries: make([]pathEntry, 0, expectedEntries),
		buckets: make([]hashBucket, capBuckets),
	}
}

func (t *pathTable) allocPath(norm []byte) uint32 {
	n := len(norm)
	if t.curOff+n > slabSize {
		sz := slabSize
		if n > sz {
			sz = n
		}
		ns := make([]byte, sz)
		t.slabs = append(t.slabs, ns)
		t.curSlab = ns
		t.curOff = 0
	}
	off := uint32((len(t.slabs)-1)<<slabShift | t.curOff)
	copy(t.curSlab[t.curOff:], norm)
	t.curOff += n
	return off
}

func (t *pathTable) pathBytes(e pathEntry) []byte {
	s := t.slabs[e.off>>slabShift]
	o := e.off & slabMask
	return s[o : o+e.n]
}

func (t *pathTable) pathString(e pathEntry) string {
	return string(t.pathBytes(e))
}

const fxSeed uint64 = 0x517cc1b727220a95

func hashBytes(b []byte) uint64 {
	var h uint64
	for len(b) >= 8 {
		h = (bits.RotateLeft64(h, 5) ^ binary.LittleEndian.Uint64(b)) * fxSeed
		b = b[8:]
	}
	if len(b) >= 4 {
		h = (bits.RotateLeft64(h, 5) ^ uint64(binary.LittleEndian.Uint32(b))) * fxSeed
		b = b[4:]
	}
	for _, c := range b {
		h = (bits.RotateLeft64(h, 5) ^ uint64(c)) * fxSeed
	}
	return h
}

func (t *pathTable) growBuckets() {
	newCap := len(t.buckets) * 2
	nb := make([]hashBucket, newCap)
	mask := uint32(newCap - 1)
	for i := range t.entries {
		e := t.entries[i]
		h := hashBytes(t.pathBytes(e))
		h32 := uint32(h ^ (h >> 32))
		pos := uint32(h) & mask
		for nb[pos].idx != 0 {
			pos = (pos + 1) & mask
		}
		nb[pos] = hashBucket{hash: h32, idx: uint32(i + 1)}
	}
	t.buckets = nb
}

func (t *pathTable) upsert(norm []byte, size int64, isDir bool) {
	if len(t.entries)*2 >= len(t.buckets) {
		t.growBuckets()
	}
	h := hashBytes(norm)
	h32 := uint32(h ^ (h >> 32))
	mask := uint32(len(t.buckets) - 1)
	pos := uint32(h) & mask

	for {
		b := t.buckets[pos]
		if b.idx == 0 {
			off := t.allocPath(norm)
			val := int64(-1)
			if !isDir {
				val = size
				t.files++
			}
			t.entries = append(t.entries, pathEntry{
				off:  off,
				n:    uint32(len(norm)),
				size: val,
			})
			t.buckets[pos] = hashBucket{hash: h32, idx: uint32(len(t.entries))}
			return
		}
		if b.hash == h32 {
			e := &t.entries[b.idx-1]
			if e.n == uint32(len(norm)) && bytes.Equal(t.pathBytes(*e), norm) {
				if !isDir {
					if e.size < 0 {
						t.files++
					}
					e.size = size
				}
				return
			}
		}
		pos = (pos + 1) & mask
	}
}

func (t *pathTable) releaseHashIndex() {
	t.buckets = nil
}

func (t *pathTable) sortEntriesByPathParallel() {
	n := len(t.entries)
	if n <= 1 {
		return
	}
	slabs := t.slabs
	cmpEntry := func(a, b pathEntry) int {
		sa := slabs[a.off>>slabShift]
		oa := a.off & slabMask
		sb := slabs[b.off>>slabShift]
		ob := b.off & slabMask
		return bytes.Compare(sa[oa:oa+a.n], sb[ob:ob+b.n])
	}

	workers := runtime.GOMAXPROCS(0)
	if workers < 2 || n < 65536 {
		slices.SortFunc(t.entries, cmpEntry)
		return
	}

	chunks := 1
	for chunks*2 <= workers {
		chunks *= 2
	}
	if chunks < 2 {
		slices.SortFunc(t.entries, cmpEntry)
		return
	}

	bounds := make([]int, chunks+1)
	for i := 0; i <= chunks; i++ {
		bounds[i] = i * n / chunks
	}

	var wg sync.WaitGroup
	wg.Add(chunks)
	for i := 0; i < chunks; i++ {
		lo, hi := bounds[i], bounds[i+1]
		go func(lo, hi int) {
			defer wg.Done()
			slices.SortFunc(t.entries[lo:hi], cmpEntry)
		}(lo, hi)
	}
	wg.Wait()

	src := t.entries
	dst := make([]pathEntry, n)
	for step := 1; step < chunks; step *= 2 {
		pairs := chunks / (step * 2)
		wg.Add(pairs)
		for p := 0; p < pairs; p++ {
			lo := bounds[p*step*2]
			mid := bounds[p*step*2+step]
			hi := bounds[(p+1)*step*2]
			go func(lo, mid, hi int) {
				defer wg.Done()
				i, j, k := lo, mid, lo
				for i < mid && j < hi {
					a, b := src[i], src[j]
					if cmpEntry(a, b) <= 0 {
						dst[k] = a
						i++
					} else {
						dst[k] = b
						j++
					}
					k++
				}
				if i < mid {
					copy(dst[k:hi], src[i:mid])
				} else if j < hi {
					copy(dst[k:hi], src[j:hi])
				}
			}(lo, mid, hi)
		}
		wg.Wait()
		src, dst = dst, src
	}
	t.entries = src
}
