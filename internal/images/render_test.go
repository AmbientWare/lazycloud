package images

import (
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	testManagedBase = "registry.example.com/lazycloud/release/python:{version}-1.0.0"
	// pathPython is how an install names the python on PATH.
	pathPython = `"$(command -v python)"`
)

func renderLines(t *testing.T, def apitypes.ImageDefinition) []string {
	t.Helper()
	s, err := validate(def)
	if err != nil {
		t.Fatal(err)
	}
	dockerfile, err := s.render(testManagedBase, func(ref string) (string, error) { return ref, nil })
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(strings.TrimRight(dockerfile, "\n"), "\n")
}

// installs returns the interpreter of each uv pip install, failing on a pip
// install that does not go through uv or a uv run before uv is copied in.
func installs(t *testing.T, lines []string) []string {
	t.Helper()
	var pythons []string
	copied := false
	for _, line := range lines {
		if strings.HasPrefix(line, "COPY --from="+uvImage+" ") {
			copied = true
		}
		if strings.Contains(line, "pip install") && !strings.Contains(line, "uv pip install") {
			t.Errorf("a pip install without uv: %s", line)
		}
		if (strings.Contains(line, "uv pip install") || strings.Contains(line, "uv python install")) && !copied {
			t.Errorf("uv runs before it is copied in: %s", line)
		}
		_, after, ok := strings.Cut(line, "uv pip install --python ")
		if !ok || strings.Contains(line, "poetry==") {
			continue
		}
		python, _, ok := strings.Cut(after, " --break-system-packages --compile-bytecode ")
		if !ok {
			t.Errorf("an install without bytecode: %s", line)
		}
		pythons = append(pythons, python)
	}
	return pythons
}

func installsPython(lines []string, version string) bool {
	for _, line := range lines {
		if strings.Contains(line, "uv python install "+version+" ") {
			return true
		}
	}
	return false
}

func pipSteps(args ...[]string) *[]apitypes.ImageStep {
	steps := make([]apitypes.ImageStep, len(args))
	for n := range args {
		steps[n] = apitypes.ImageStep{Kind: apitypes.Pip, Args: &args[n]}
	}
	return &steps
}

func TestEveryBaseInstallsPackagesWithUV(t *testing.T) {
	for name, tc := range map[string]struct {
		def     apitypes.ImageDefinition
		from    string
		python  string
		install string
	}{
		"managed base": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.12", PythonPackages: &[]string{"numpy"}},
			from: "FROM registry.example.com/lazycloud/release/python:3.12-1.0.0", python: pathPython,
		},
		// The managed base of 3.12 is the newest patch release; another is
		// installed unless the base is it.
		"managed base, patch release": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.12.4", PythonPackages: &[]string{"numpy"}},
			from: "FROM registry.example.com/lazycloud/release/python:3.12-1.0.0", python: pathPython, install: "3.12.4",
		},
		"user base with Python": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.11", BaseImage: new("docker.io/library/python:3.11-slim"), PythonPackages: &[]string{"numpy"}},
			from: "FROM docker.io/library/python:3.11-slim", python: pathPython,
		},
		"user base with another Python": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.12", BaseImage: new("docker.io/library/python:3.11-slim"), PythonPackages: &[]string{"numpy"}},
			from: "FROM docker.io/library/python:3.11-slim", python: "/usr/local/bin/python3.12", install: "3.12",
		},
		"user base without Python": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.13", BaseImage: new("docker.io/library/ubuntu:24.04"), PythonPackages: &[]string{"numpy"}},
			from: "FROM docker.io/library/ubuntu:24.04", python: "/usr/local/bin/python3.13", install: "3.13",
		},
		"Dockerfile without Python": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.10", Dockerfile: new("FROM docker.io/library/debian:trixie-slim\nRUN true\n"), PythonPackages: &[]string{"numpy"}},
			from: "FROM docker.io/library/debian:trixie-slim", python: "/usr/local/bin/python3.10", install: "3.10",
		},
		"Dockerfile with Python": {
			def:  apitypes.ImageDefinition{PythonVersion: "3.14", Dockerfile: new("FROM docker.io/library/python:3.14-slim\n"), PythonPackages: &[]string{"numpy"}},
			from: "FROM docker.io/library/python:3.14-slim", python: pathPython,
		},
		"micromamba": {
			def: apitypes.ImageDefinition{
				PythonVersion: "3.12", Micromamba: new(true), PythonPackages: &[]string{"numpy"},
				Steps: &[]apitypes.ImageStep{{Kind: apitypes.Micromamba, Args: &[]string{"ffmpeg"}}},
			},
			from: "FROM registry.example.com/lazycloud/release/python:3.12-1.0.0", python: pathPython,
		},
	} {
		t.Run(name, func(t *testing.T) {
			lines := renderLines(t, tc.def)
			if lines[0] != tc.from {
				t.Errorf("starts %q, want %q", lines[0], tc.from)
			}
			if pythons := installs(t, lines); len(pythons) != 1 || pythons[0] != tc.python {
				t.Errorf("installs into %q, want %q:\n%s", pythons, tc.python, strings.Join(lines, "\n"))
			}
			for _, version := range []string{"3.10", "3.11", "3.12", "3.12.4", "3.13", "3.14"} {
				if got := installsPython(lines, version); got != (version == tc.install) {
					t.Errorf("installs Python %s: %v, want it only for %q", version, got, tc.install)
				}
			}
		})
	}
}

// An installed Python is the exact release asked for, checked before any
// link replaces the base's, with pip and compiled bytecode.
func TestInstalledPythonIsExactAndLinked(t *testing.T) {
	lines := renderLines(t, apitypes.ImageDefinition{PythonVersion: "3.12.4"})
	var install string
	for _, line := range lines {
		if strings.Contains(line, "uv python install") {
			install = line
		}
	}
	// Only a base that is not 3.12.4 installs it.
	if !strings.HasPrefix(install, "RUN python -c 'import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 4) else 1)' 2>/dev/null || (set -eu; ") {
		t.Fatalf("the managed base's patch release is not checked first: %s", install)
	}
	check := strings.Index(install, `"$python" -c 'import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 4) else 1)'`)
	if link := strings.Index(install, "for link in python python3 python3.12;"); check < 0 || link < check {
		t.Errorf("the version is not checked before linking: %s", install)
	}
	for _, want := range []string{
		"export UV_PYTHON_INSTALL_DIR=/opt/runtime-python UV_NO_CACHE=1",
		`"$python" -m pip --version >/dev/null 2>&1 || "$python" -m ensurepip`,
		`ln -sf "$(dirname "$python")/pip" /usr/local/bin/pip`,
		`/EXTERNALLY-MANAGED"`,
		`"$python" -c ` + shellQuote(compileStdlib),
	} {
		if !strings.Contains(install, want) {
			t.Errorf("the install lacks %s", want)
		}
	}
}

// Consecutive pip steps share one install unless a flag changes how the
// packages around it resolve; a project moves later installs into its
// environment.
func TestPipStepsMergeUntilAFlagOrProject(t *testing.T) {
	steps := *pipSteps([]string{"numpy"}, []string{"pandas"}, []string{"--no-deps", "torch"}, []string{"scipy"})
	steps = append(steps, apitypes.ImageStep{Kind: apitypes.UvProject, Args: &[]string{"."}})
	steps = append(steps, *pipSteps([]string{"rich"})...)
	lines := renderLines(t, apitypes.ImageDefinition{PythonVersion: "3.12", Steps: &steps})
	var got []string
	for _, line := range lines {
		if _, args, ok := strings.Cut(line, "--compile-bytecode "); ok && strings.Contains(line, "uv pip install") {
			got = append(got, args)
		}
	}
	want := []string{"numpy pandas", "--no-deps torch", "scipy", "rich"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("installs %q, want %q", got, want)
	}
	if pythons := installs(t, lines); pythons[len(pythons)-1] != pathPython {
		t.Errorf("the install after the project goes to %s", pythons[len(pythons)-1])
	}
	for _, line := range lines {
		if strings.Contains(line, "uv sync") && !strings.HasPrefix(line, "RUN export UV_NO_CACHE=1 && ") {
			t.Errorf("the project syncs with a cache in its layer: %s", line)
		}
	}
}
