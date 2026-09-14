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

type filesystemRequest struct {
	Operation   string `json:"operation"`
	Path        string `json:"path"`
	Source      string `json:"source"`
	Mode        uint32 `json:"mode"`
	Pattern     string `json:"pattern"`
	Replacement string `json:"replacement"`
}

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
}

func runFilesystem(payload string) error {
	var request filesystemRequest
	decoder := json.NewDecoder(strings.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return err
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
		_, err = io.Copy(os.Stdout, input)
		return response, err
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
		paths := []string{path}
		if info.IsDir() {
			entries, err := os.ReadDir(path)
			if err != nil {
				return response, err
			}
			paths = nil
			for _, entry := range entries {
				paths = append(paths, filepath.Join(path, entry.Name()))
			}
		}
		for _, child := range paths {
			info, err := filesystemStat(child)
			if err != nil {
				return response, err
			}
			response.Files = append(response.Files, info)
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

func filesystemStat(path string) (filesystemInfo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return filesystemInfo{}, err
	}
	stat := info.Sys().(*syscall.Stat_t)
	return filesystemInfo{
		Name: info.Name(), Mode: stat.Mode, Size: info.Size(), ModTime: info.ModTime().Unix(),
		Owner: strconv.FormatUint(uint64(stat.Uid), 10), Group: strconv.FormatUint(uint64(stat.Gid), 10),
		IsDir: info.IsDir(), Permissions: stat.Mode & 0777,
	}, nil
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
