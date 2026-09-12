package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"unicode/utf8"
)

const maxFileBytes = 1 << 30
const maxControlMessageBytes = ((maxFileBytes+2)/3)*4 + 1024*1024

type fileInfo struct {
	Name        string `json:"name"`
	Mode        uint32 `json:"mode"`
	Size        int64  `json:"size"`
	ModTime     int64  `json:"mod_time"`
	Owner       string `json:"owner"`
	Group       string `json:"group"`
	IsDir       bool   `json:"is_dir"`
	Permissions uint32 `json:"permissions"`
}

type fileSearchMatch struct {
	Path   string `json:"path"`
	Text   string `json:"text"`
	Line   int    `json:"line"`
	Column int    `json:"column"`
}

func (s *supervisor) handleFile(encoder *json.Encoder, command request) {
	result, err := executeFileOperation(command)
	if err != nil {
		writeError(encoder, err)
		return
	}
	_ = encoder.Encode(result)
}

func executeFileOperation(command request) (response, error) {
	result := response{Version: protocolVersion, Type: "file"}
	path := strings.TrimSpace(command.Path)
	if path == "" {
		return result, errors.New("sandbox container path is required")
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join("/", command.Cwd, path)
	}
	path = filepath.Clean(path)
	if command.Mode > 07777 {
		return result, errors.New("sandbox file mode is invalid")
	}
	switch command.FileOperation {
	case "upload-file":
		if len(command.Data) > maxFileBytes {
			return result, errors.New("sandbox file exceeds transfer limit")
		}
		return result, writeUploadedFile(path, command.Data, command.Mode)
	case "download-file":
		data, err := readSandboxFile(path)
		result.Data = data
		return result, err
	case "create-directory":
		if err := os.MkdirAll(path, permissionMode(command.Mode)); err != nil {
			return result, err
		}
		return result, os.Chmod(path, permissionMode(command.Mode))
	case "delete-file":
		return result, removeSandboxPath(path, false)
	case "delete-directory":
		return result, removeSandboxPath(path, true)
	case "stat-file":
		info, err := describeFile(path)
		result.FileInfo = info
		return result, err
	case "list-files":
		info, err := os.Stat(path)
		if errors.Is(err, os.ErrNotExist) {
			return result, nil
		}
		if err != nil {
			return result, err
		}
		if !info.IsDir() {
			item, err := describeFile(path)
			if err != nil {
				return result, err
			}
			result.Files = []fileInfo{*item}
			return result, nil
		}
		entries, err := os.ReadDir(path)
		if err != nil {
			return result, err
		}
		for _, entry := range entries {
			item, err := describeFile(filepath.Join(path, entry.Name()))
			if err != nil {
				return result, err
			}
			result.Files = append(result.Files, *item)
		}
		return result, nil
	case "find-in-files", "replace-in-files":
		err := visitTextFiles(path, func(path string, data []byte, info fs.FileInfo) error {
			content := string(data)
			if !strings.Contains(content, command.Pattern) {
				return nil
			}
			if command.FileOperation == "replace-in-files" {
				return os.WriteFile(path, []byte(strings.ReplaceAll(content, command.Pattern, command.NewString)), info.Mode().Perm())
			}
			match := fileSearchMatch{Path: strings.TrimPrefix(path, "/"), Text: command.Pattern}
			for index, line := range textLines(content) {
				if column := strings.Index(line, command.Pattern); column >= 0 {
					match.Line = index + 1
					match.Column = utf8.RuneCountInString(line[:column]) + 1
					break
				}
			}
			result.Matches = append(result.Matches, match)
			return nil
		})
		return result, err
	default:
		return result, fmt.Errorf("unknown sandbox file operation %q", command.FileOperation)
	}
}

func writeUploadedFile(path string, data []byte, mode uint32) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".lazycloud-upload-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Chmod(permissionMode(mode)); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func permissionMode(mode uint32) os.FileMode {
	permissions := os.FileMode(mode & 0777)
	if mode&04000 != 0 {
		permissions |= os.ModeSetuid
	}
	if mode&02000 != 0 {
		permissions |= os.ModeSetgid
	}
	if mode&01000 != 0 {
		permissions |= os.ModeSticky
	}
	return permissions
}

func textLines(content string) []string {
	if content == "" {
		return nil
	}
	normalized := strings.Map(func(character rune) rune {
		switch character {
		case '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029':
			return '\n'
		default:
			return character
		}
	}, strings.ReplaceAll(content, "\r\n", "\n"))
	return strings.Split(strings.TrimSuffix(normalized, "\n"), "\n")
}

func readSandboxFile(path string) ([]byte, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxFileBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maxFileBytes {
		return nil, errors.New("sandbox file exceeds transfer limit")
	}
	return data, nil
}

func removeSandboxPath(path string, directoryOnly bool) error {
	if path == "/" {
		return errors.New("sandbox root cannot be deleted")
	}
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if directoryOnly && !info.IsDir() {
		if info.Mode()&os.ModeSymlink != 0 {
			target, err := os.Stat(path)
			if err == nil && target.IsDir() {
				return errors.New("cannot delete a directory through a symbolic link")
			}
			if err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
		return nil
	}
	return os.RemoveAll(path)
}

func describeFile(path string) (*fileInfo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, errors.New("sandbox file metadata is unavailable")
	}
	return &fileInfo{
		Name: filepath.Base(path), Mode: stat.Mode, Size: info.Size(), ModTime: info.ModTime().Unix(),
		Owner: strconv.FormatUint(uint64(stat.Uid), 10), Group: strconv.FormatUint(uint64(stat.Gid), 10),
		IsDir: info.IsDir(), Permissions: stat.Mode & 0777,
	}, nil
}

func visitTextFiles(path string, visit func(string, []byte, fs.FileInfo) error) error {
	visitFile := func(path string) error {
		info, err := os.Stat(path)
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		data, err := readSandboxFile(path)
		if err != nil {
			return err
		}
		if !utf8.Valid(data) {
			return nil
		}
		return visit(path, data, info)
	}
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return visitFile(path)
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if err := filepath.WalkDir(filepath.Join(path, entry.Name()), func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if entry.IsDir() {
				return nil
			}
			return visitFile(path)
		}); err != nil {
			return err
		}
	}
	return nil
}
