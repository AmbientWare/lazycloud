package supervisor

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"unicode/utf8"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	maxListEntries  = 10000
	maxDownloadSize = 64 << 20
	maxFindMatches  = 10000
	// maxSearchedFile is the largest file find and replace read; larger
	// files are skipped so a search cannot exhaust the container's memory.
	maxSearchedFile = 64 << 20
	// TruncatedHeader marks a download cut at max_bytes.
	TruncatedHeader = "Lazycloud-Truncated"
)

func containerFile(info fs.FileInfo) apitypes.ContainerFile {
	f := apitypes.ContainerFile{Name: info.Name(), Size: info.Size(), IsDir: info.IsDir()}
	modTime := info.ModTime().UTC()
	f.ModTime = &modTime
	if st, ok := info.Sys().(*syscall.Stat_t); ok {
		f.Mode = int64(st.Mode)
		f.Permissions = int(st.Mode & 0o777)
		f.Owner = strconv.FormatUint(uint64(st.Uid), 10)
		f.Group = strconv.FormatUint(uint64(st.Gid), 10)
	}
	return f
}

// queryInt reads an integer query parameter within [lo, hi].
func queryInt(r *http.Request, name string, def, lo, hi int64) (int64, error) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def, nil
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || v < lo || v > hi {
		return 0, invalid("%s must be an integer from %d to %d", name, lo, hi)
	}
	return v, nil
}

// fileMode turns permission bits such as 0o4755 into an os.FileMode.
func fileMode(bits int64) os.FileMode {
	mode := os.FileMode(bits & 0o777) //nolint:gosec // bits is validated to 0..4095
	if bits&0o4000 != 0 {
		mode |= os.ModeSetuid
	}
	if bits&0o2000 != 0 {
		mode |= os.ModeSetgid
	}
	if bits&0o1000 != 0 {
		mode |= os.ModeSticky
	}
	return mode
}

func (c *control) listFiles(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	limit, err := queryInt(r, "limit", maxListEntries, 1, maxListEntries)
	if err != nil {
		return err
	}
	info, err := os.Stat(path) //nolint:gosec // the control API reads any path in the container
	if err != nil {
		return fileError(err)
	}
	list := apitypes.ContainerFileList{Files: []apitypes.ContainerFile{}}
	if !info.IsDir() {
		list.Files = append(list.Files, containerFile(info))
		writeJSON(w, http.StatusOK, list)
		return nil
	}
	dir, err := os.Open(path) //nolint:gosec // see above
	if err != nil {
		return fileError(err)
	}
	names, err := dir.Readdirnames(-1)
	_ = dir.Close()
	if err != nil {
		return fileError(err)
	}
	// Sorting needs every name, which is cheap; only listed names are
	// stat'ed.
	slices.Sort(names)
	if int64(len(names)) > limit {
		names, list.Truncated = names[:limit], true
	}
	for _, name := range names {
		child := filepath.Join(path, name)
		info, err := os.Stat(child) //nolint:gosec // see above
		if err != nil {
			// A link whose target is gone is still an entry.
			info, err = os.Lstat(child) //nolint:gosec // see above
		}
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return fileError(err)
		}
		list.Files = append(list.Files, containerFile(info))
	}
	writeJSON(w, http.StatusOK, list)
	return nil
}

func (c *control) statFile(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	info, err := os.Stat(path) //nolint:gosec // the control API reads any path in the container
	if err != nil {
		return fileError(err)
	}
	writeJSON(w, http.StatusOK, containerFile(info))
	return nil
}

// remove deletes path, which must be a directory when dir is set and must
// not be one otherwise. A missing path is not an error.
func remove(path string, dir bool) error {
	info, err := os.Lstat(path) //nolint:gosec // and deletes any
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fileError(err)
	}
	switch {
	case dir && !info.IsDir():
		return apiErr(http.StatusConflict, apitypes.Conflict, "%s is not a directory", path)
	case !dir && info.IsDir():
		return apiErr(http.StatusConflict, apitypes.Conflict, "%s is a directory", path)
	case dir:
		err = os.RemoveAll(path) //nolint:gosec // see above
	default:
		err = os.Remove(path) //nolint:gosec // see above
	}
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fileError(err)
	}
	return nil
}

func (c *control) deleteFile(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	if err := remove(path, false); err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

func (c *control) deleteDirectory(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	if path == "/" {
		return invalid("refusing to delete /")
	}
	if err := remove(path, true); err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

func (c *control) createDirectory(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	bits, err := queryInt(r, "mode", 0o755, 0, 0o7777)
	if err != nil {
		return err
	}
	mode := fileMode(bits)
	if err := os.MkdirAll(path, mode); err != nil { //nolint:gosec // the control API writes any path in the container
		return fileError(err)
	}
	// MkdirAll applies the umask and leaves an existing directory alone.
	if err := os.Chmod(path, mode); err != nil { //nolint:gosec // see above
		return fileError(err)
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

func (c *control) downloadFile(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	limit, err := queryInt(r, "max_bytes", maxDownloadSize, 1, maxDownloadSize)
	if err != nil {
		return err
	}
	truncate := false
	if raw := r.URL.Query().Get("truncate"); raw != "" {
		if truncate, err = strconv.ParseBool(raw); err != nil {
			return invalid("truncate must be true or false")
		}
	}
	file, err := os.Open(path) //nolint:gosec // the control API reads any path in the container
	if err != nil {
		return fileError(err)
	}
	defer func() { _ = file.Close() }()
	info, err := file.Stat()
	if err != nil {
		return fileError(err)
	}
	if info.IsDir() {
		return apiErr(http.StatusConflict, apitypes.Conflict, "%s is a directory", path)
	}
	var body io.Reader
	var size int64
	if info.Mode().IsRegular() && info.Size() > 0 {
		// The size at open is the download: a file growing while it is read
		// sends what it held then.
		size, body = info.Size(), io.LimitReader(file, info.Size())
	} else {
		// /proc and device files report no size, so they are read, one byte
		// past the limit to tell a file that fits from a larger one.
		data, err := io.ReadAll(io.LimitReader(file, limit+1))
		if err != nil {
			return fileError(err)
		}
		size, body = int64(len(data)), strings.NewReader(string(data))
	}
	if size > limit {
		if !truncate {
			return apiErr(http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge,
				"%s is larger than %d bytes", path, limit)
		}
		size, body = limit, io.LimitReader(body, limit)
		w.Header().Set(TruncatedHeader, "true")
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
	w.WriteHeader(http.StatusOK)
	if _, err := io.Copy(w, body); err != nil {
		// The status is sent; a short body tells the client.
		panic(http.ErrAbortHandler)
	}
	return nil
}

func (c *control) uploadFile(w http.ResponseWriter, r *http.Request) error {
	path, err := c.queryPath(r)
	if err != nil {
		return err
	}
	bits, err := queryInt(r, "mode", 0o644, 0, 0o7777)
	if err != nil {
		return err
	}
	if err := writeFileAtomic(path, r.Body, fileMode(bits)); err != nil {
		return fileError(err)
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// writeFileAtomic writes body to a temporary file beside path and renames it
// over path, creating the parent directories.
func writeFileAtomic(path string, body io.Reader, mode os.FileMode) error {
	if info, err := os.Stat(path); err == nil && info.IsDir() { //nolint:gosec // see above
		return apiErr(http.StatusConflict, apitypes.Conflict, "%s is a directory", path)
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // directories in the container are world-readable as with mkdir -p
		return fmt.Errorf("create parent directories: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".lazycloud-upload-*")
	if err != nil {
		return fmt.Errorf("create upload file: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }() //nolint:gosec // see above
	defer func() { _ = tmp.Close() }()
	if _, err := io.Copy(tmp, body); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return apiErr(http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "%s", err.Error())
		}
		return fmt.Errorf("write upload: %w", err)
	}
	if err := tmp.Chmod(mode); err != nil {
		return fmt.Errorf("set upload mode: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close upload: %w", err)
	}
	if err := os.Rename(tmp.Name(), path); err != nil { //nolint:gosec // see above
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}

// eachTextFile calls fn with every regular UTF-8 file under root that could
// hold a match. Entries that vanish or cannot be read during the walk are
// skipped; a missing root is an error.
func eachTextFile(root string, fn func(path string, info fs.FileInfo, content string) (bool, error)) error {
	if _, err := os.Stat(root); err != nil {
		return fileError(err)
	}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			if path != root && (errors.Is(err, fs.ErrNotExist) || errors.Is(err, fs.ErrPermission)) {
				return nil
			}
			return err
		}
		if entry.IsDir() {
			return nil
		}
		info, err := os.Stat(path)
		// Pseudo-files such as /proc's report size 0 and may block; an
		// empty file holds no match.
		if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > maxSearchedFile {
			return nil //nolint:nilerr // vanished and unreadable entries are skipped
		}
		data, err := os.ReadFile(path) //nolint:gosec // see above
		if err != nil || !utf8.Valid(data) {
			return nil //nolint:nilerr // see above
		}
		more, err := fn(path, info, string(data))
		if err != nil {
			return err
		}
		if !more {
			return filepath.SkipAll
		}
		return nil
	})
	if err != nil {
		return fileError(err)
	}
	return nil
}

func (c *control) findInFiles(w http.ResponseWriter, r *http.Request) error {
	var req apitypes.FindInFilesRequest
	if err := readJSON(r, &req, false); err != nil {
		return err
	}
	if req.Pattern == "" {
		return invalid("pattern is required")
	}
	root, err := c.resolve(req.Path)
	if err != nil {
		return err
	}
	found := apitypes.FileMatches{Matches: []apitypes.FileMatch{}}
	err = eachTextFile(root, func(path string, _ fs.FileInfo, content string) (bool, error) {
		line, lineStart, scanned := 1, 0, 0
		for offset := 0; ; {
			i := strings.Index(content[offset:], req.Pattern)
			if i < 0 {
				return true, nil
			}
			at := offset + i
			if len(found.Matches) == maxFindMatches {
				found.Truncated = true
				return false, nil
			}
			// Lines and columns advance incrementally over the file.
			between := content[scanned:at]
			if n := strings.Count(between, "\n"); n > 0 {
				line += n
				lineStart = scanned + strings.LastIndexByte(between, '\n') + 1
			}
			scanned = at
			found.Matches = append(found.Matches, apitypes.FileMatch{
				Path: path, Line: line, Column: utf8.RuneCountInString(content[lineStart:at]) + 1, Text: req.Pattern,
			})
			offset = at + len(req.Pattern)
		}
	})
	if err != nil {
		return err
	}
	writeJSON(w, http.StatusOK, found)
	return nil
}

func (c *control) replaceInFiles(w http.ResponseWriter, r *http.Request) error {
	var req apitypes.ReplaceInFilesRequest
	if err := readJSON(r, &req, false); err != nil {
		return err
	}
	if req.Pattern == "" {
		return invalid("pattern is required")
	}
	root, err := c.resolve(req.Path)
	if err != nil {
		return err
	}
	var replaced apitypes.ReplacedFiles
	err = eachTextFile(root, func(path string, info fs.FileInfo, content string) (bool, error) {
		n := strings.Count(content, req.Pattern)
		if n == 0 {
			return true, nil
		}
		// Rewriting in place keeps the file's owner, mode and links.
		//nolint:gosec // the control API writes any path in the container
		if err := os.WriteFile(path, []byte(strings.ReplaceAll(content, req.Pattern, req.Replacement)), info.Mode()); err != nil {
			return false, fmt.Errorf("rewrite %s: %w", path, err)
		}
		replaced.Files++
		replaced.Replacements += n
		return true, nil
	})
	if err != nil {
		return err
	}
	writeJSON(w, http.StatusOK, replaced)
	return nil
}
