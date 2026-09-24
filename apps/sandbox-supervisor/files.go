package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"unicode/utf8"
)

type filesystemRequest struct {
	Operation   string `json:"operation"`
	Path        string `json:"path"`
	Source      string `json:"source"`
	Mode        uint32 `json:"mode"`
	Pattern     string `json:"pattern"`
	Replacement string `json:"replacement"`
	// Limit caps the entries a listing returns and the bytes a download
	// writes; zero leaves both unbounded. A download over the limit is
	// refused unless Truncate asks for its first Limit bytes instead.
	Limit    int64 `json:"limit"`
	Truncate bool  `json:"truncate"`
}

// maxFilesystemLimit keeps Limit+1 far from overflowing; the control plane
// sends much smaller limits.
const maxFilesystemLimit = 1 << 40

// errOverLimit is the refusal a caller maps to its own client error rather
// than a failure of the container.
var errOverLimit = errors.New("over the download limit")

// exitOverLimit is the exit status of a filesystem call refused with
// errOverLimit; the worker reads it as a typed refusal.
const exitOverLimit = 3

type filesystemInfo struct {
	Name        string `json:"name"`
	Mode        uint32 `json:"mode"`
	Size        int64  `json:"size"`
	ModTime     int64  `json:"mod_time"`
	Owner       string `json:"owner"`
	Group       string `json:"group"`
	IsDir       bool   `json:"is_dir"`
	Permissions uint32 `json:"permissions"`
}

type filesystemMatch struct {
	Path   string `json:"path"`
	Text   string `json:"text"`
	Line   int    `json:"line"`
	Column int    `json:"column"`
}

type filesystemResponse struct {
	OK       bool              `json:"ok"`
	FileInfo *filesystemInfo   `json:"file_info,omitempty"`
	Files    []filesystemInfo  `json:"files,omitempty"`
	Results  []filesystemMatch `json:"results,omitempty"`
	// Truncated says a listing returned its first Limit names and more remain.
	Truncated bool `json:"truncated,omitempty"`
}

func runFilesystem(payload string) error {
	var request filesystemRequest
	decoder := json.NewDecoder(strings.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return err
	}
	if request.Limit < 0 || request.Limit > maxFilesystemLimit {
		return fmt.Errorf("limit %d is outside 0..%d", request.Limit, int64(maxFilesystemLimit))
	}
	response, err := filesystemOperation(request)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return fmt.Errorf("path not found: %q", request.Path)
		}
		return err
	}
	if request.Operation == "download-file" {
		return nil
	}
	return json.NewEncoder(os.Stdout).Encode(response)
}

func filesystemOperation(request filesystemRequest) (filesystemResponse, error) {
	response := filesystemResponse{OK: true}
	path := request.Path
	mode := os.FileMode(request.Mode & 0777)
	if request.Mode&04000 != 0 {
		mode |= os.ModeSetuid
	}
	if request.Mode&02000 != 0 {
		mode |= os.ModeSetgid
	}
	if request.Mode&01000 != 0 {
		mode |= os.ModeSticky
	}
	switch request.Operation {
	case "upload-file":
		return response, uploadFilesystemFile(request.Source, path, mode)
	case "download-file":
		input, err := os.Open(path)
		if err != nil {
			return response, err
		}
		defer input.Close()
		if request.Limit == 0 {
			_, err = io.Copy(os.Stdout, input)
			return response, err
		}
		if request.Truncate {
			_, err = io.CopyN(os.Stdout, input, request.Limit)
			if errors.Is(err, io.EOF) {
				err = nil
			}
			return response, err
		}
		// The size is only known by reading: a file can grow while it is
		// read, and /proc and device files report none. Reading one byte past
		// the limit is what tells a file that fits exactly from a larger one.
		written, err := io.CopyN(os.Stdout, input, request.Limit+1)
		if err != nil && !errors.Is(err, io.EOF) {
			return response, err
		}
		if written > request.Limit {
			return response, fmt.Errorf("%q is larger than %d bytes: %w", path, request.Limit, errOverLimit)
		}
		return response, nil
	case "create-directory":
		if err := os.MkdirAll(path, mode); err != nil {
			return response, err
		}
		return response, os.Chmod(path, mode)
	case "delete-file", "delete-directory":
		info, err := os.Lstat(path)
		if errors.Is(err, fs.ErrNotExist) {
			return response, nil
		}
		if err != nil {
			return response, err
		}
		if request.Operation == "delete-directory" {
			if !info.IsDir() {
				return response, fmt.Errorf("not a directory: %q", path)
			}
			return response, os.RemoveAll(path)
		}
		if info.IsDir() {
			return response, fmt.Errorf("is a directory: %q", path)
		}
		return response, os.Remove(path)
	case "stat-file":
		info, err := filesystemStat(path)
		response.FileInfo = &info
		return response, err
	case "list-files":
		info, err := os.Stat(path)
		if err != nil {
			return response, err
		}
		if !info.IsDir() {
			response.Files = []filesystemInfo{filesystemInfoOf(info)}
			return response, nil
		}
		names, truncated, err := directoryNames(path, request.Limit)
		if err != nil {
			return response, err
		}
		response.Truncated = truncated
		for _, name := range names {
			child := filepath.Join(path, name)
			info, err := os.Stat(child)
			if err != nil {
				// A link whose target is gone is still an entry, so the
				// link itself is reported.
				info, err = os.Lstat(child)
			}
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}
			if err != nil {
				return response, err
			}
			response.Files = append(response.Files, filesystemInfoOf(info))
		}
		return response, nil
	case "find-in-files", "replace-in-files":
		err := filepath.WalkDir(path, func(current string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			info, err := os.Stat(current)
			if err != nil {
				return err
			}
			if !info.Mode().IsRegular() {
				return nil
			}
			data, err := os.ReadFile(current)
			if err != nil {
				return err
			}
			if !utf8.Valid(data) {
				return nil
			}
			content := string(data)
			index := strings.Index(content, request.Pattern)
			if index < 0 {
				return nil
			}
			if request.Operation == "replace-in-files" {
				return os.WriteFile(current, []byte(strings.ReplaceAll(content, request.Pattern, request.Replacement)), 0600)
			}
			prefix := content[:index]
			response.Results = append(response.Results, filesystemMatch{
				Path: strings.TrimPrefix(current, "/"), Text: request.Pattern,
				Line:   strings.Count(prefix, "\n") + 1,
				Column: utf8.RuneCountInString(prefix[strings.LastIndex(prefix, "\n")+1:]) + 1,
			})
			return nil
		})
		return response, err
	default:
		return response, fmt.Errorf("unknown filesystem operation: %s", request.Operation)
	}
}

// directoryNames returns the first limit names in sorted order, and whether
// more remain. Sorting needs every name, which is cheap; only the names
// returned are stat'ed.
func directoryNames(path string, limit int64) ([]string, bool, error) {
	directory, err := os.Open(path)
	if err != nil {
		return nil, false, err
	}
	defer directory.Close()
	names, err := directory.Readdirnames(-1)
	if err != nil {
		return nil, false, err
	}
	sort.Strings(names)
	truncated := limit > 0 && int64(len(names)) > limit
	if truncated {
		names = names[:limit]
	}
	return names, truncated, nil
}

func filesystemStat(path string) (filesystemInfo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return filesystemInfo{}, err
	}
	return filesystemInfoOf(info), nil
}

func filesystemInfoOf(info os.FileInfo) filesystemInfo {
	stat := info.Sys().(*syscall.Stat_t)
	return filesystemInfo{
		Name: info.Name(), Mode: stat.Mode, Size: info.Size(), ModTime: info.ModTime().Unix(),
		Owner: strconv.FormatUint(uint64(stat.Uid), 10), Group: strconv.FormatUint(uint64(stat.Gid), 10),
		IsDir: info.IsDir(), Permissions: stat.Mode & 0777,
	}
}

func uploadFilesystemFile(source, target string, mode os.FileMode) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return err
	}
	output, err := os.CreateTemp(filepath.Dir(target), ".lazycloud-upload-*")
	if err != nil {
		return err
	}
	defer os.Remove(output.Name())
	defer output.Close()
	if _, err := io.Copy(output, input); err != nil {
		return err
	}
	if err := output.Chmod(mode); err != nil {
		return err
	}
	if err := output.Close(); err != nil {
		return err
	}
	return os.Rename(output.Name(), target)
}
