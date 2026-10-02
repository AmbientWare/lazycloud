package images

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/moby/buildkit/frontend/dockerfile/instructions"
	"github.com/moby/buildkit/frontend/dockerfile/parser"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Tools copied into images that need them, pinned by digest.
const (
	uvImage         = "ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc"
	micromambaImage = "docker.io/mambaorg/micromamba:2.9.0@sha256:e0a99b0f17a759e14c2f967dc0ca2d3a3c1ca3c62955f4d20bba770eaaf0184d"
	poetryVersion   = "2.2.1"
	// managedPython is where a base without Python gets one.
	managedPython      = "/opt/runtime-python"
	projectEnvironment = "/opt/lazycloud/.venv"
	poetryEnvironment  = "/opt/lazycloud/poetry"
	workdir            = "/workspace"
	// identityVersion changes when rendering changes what an equal
	// definition builds, so old identities are not reused.
	identityVersion = 2
)

// pipBoundaryFlags end a group of merged pip installs: they change how the
// packages around them resolve.
func pipBoundaryFlags() []string {
	return []string{
		"--no-deps", "--only-binary", "--no-binary", "--prefer-binary", "--require-hashes", "--pre",
		"--ignore-requires-python", "--no-pin", "--force-reinstall", "--freeze-installed",
		"--update-deps", "--no-update-deps",
	}
}

var (
	pythonVersionPattern = regexp.MustCompile(`^3\.(10|11|12|13|14)(\.[0-9]{1,3})?$`)
	envNamePattern       = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	plainEnvValue        = regexp.MustCompile(`^[A-Za-z0-9_./:@+-]+$`)
)

// spec is a validated definition with its base images still unpinned.
type spec struct {
	python       string
	micromamba   bool
	base         string
	architecture string
	packages     []string
	steps        []apitypes.ImageStep
	commands     []string
	env          map[string]string
	dockerfile   string
	context      []byte
	// secrets are the workspace secrets rendered steps read, sorted.
	secrets []string
	// secretVersionsKey is a digest of the versions of secrets, set once
	// they are read.
	secretVersionsKey string
	// gpu is the GPU model the build runs on, or empty.
	gpu string
}

// minorVersion is "3.12" for "3.12" and "3.12.11".
func minorVersion(version string) string {
	parts := strings.SplitN(version, ".", 3)
	return parts[0] + "." + parts[1]
}

func validate(def apitypes.ImageDefinition) (spec, error) {
	if !pythonVersionPattern.MatchString(def.PythonVersion) {
		return spec{}, invalid("python_version %q is not 3.10 to 3.14 or a patch release of them", def.PythonVersion)
	}
	s := spec{python: def.PythonVersion, architecture: string(apitypes.ImageDefinitionArchitectureAmd64)}
	if def.Micromamba != nil {
		s.micromamba = *def.Micromamba
	}
	if def.Architecture != nil {
		s.architecture = string(*def.Architecture)
	}
	if def.BaseImage != nil {
		s.base = strings.TrimSpace(*def.BaseImage)
		if err := oneLine("base_image", s.base); err != nil {
			return spec{}, err
		}
	}
	if def.Dockerfile != nil && strings.TrimSpace(*def.Dockerfile) != "" {
		if s.base != "" {
			return spec{}, invalid("dockerfile builds cannot also set a base image")
		}
		s.dockerfile = strings.TrimRight(*def.Dockerfile, "\n") + "\n"
	}
	if def.PythonPackages != nil {
		for _, p := range *def.PythonPackages {
			if err := oneLine("python_packages", p); err != nil {
				return spec{}, err
			}
		}
		s.packages = cleanPackages(*def.PythonPackages)
	}
	if def.Steps != nil {
		for n, step := range *def.Steps {
			clean, err := cleanStep(step)
			if err != nil {
				return spec{}, invalid("step %d: %v", n, err)
			}
			if clean != nil {
				s.steps = append(s.steps, *clean)
			}
		}
	}
	if !s.micromamba && slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool {
		return step.Kind == apitypes.Micromamba || step.Kind == apitypes.MicromambaEnvironment
	}) {
		return spec{}, invalid("micromamba steps need micromamba enabled")
	}
	if def.Commands != nil {
		for _, command := range *def.Commands {
			if strings.ContainsRune(command, 0) {
				return spec{}, invalid("commands must not contain NUL")
			}
			if c := strings.TrimSpace(command); c != "" {
				s.commands = append(s.commands, c)
			}
		}
	}
	if def.Env != nil {
		for key := range *def.Env {
			if !envNamePattern.MatchString(key) {
				return spec{}, invalid("environment variable name %q is invalid", key)
			}
		}
		s.env = *def.Env
	}
	if def.Secrets != nil {
		for _, name := range *def.Secrets {
			if !envNamePattern.MatchString(name) {
				return spec{}, invalid("secret name %q is invalid", name)
			}
		}
		s.secrets = slices.Compact(slices.Sorted(slices.Values(*def.Secrets)))
	}
	if def.Gpu != nil && *def.Gpu != "" {
		// One model, not a preference: it is the machine the image is
		// built on.
		if gpu := apitypes.GpuType(*def.Gpu); !gpu.Valid() || gpu == apitypes.Any {
			return spec{}, invalid("gpu %q is not a GPU model; name one such as L4 or A100-80", *def.Gpu)
		}
		s.gpu = *def.Gpu
	}
	if def.Context != nil {
		digest, err := parseSha256(def.Context.Sha256)
		if err != nil {
			return spec{}, invalid("context.sha256 is not a lowercase hex digest")
		}
		s.context = digest
	}
	return s, nil
}

// oneLine refuses a value with a line break or NUL, which could end the
// Dockerfile instruction it is written into.
func oneLine(field, value string) error {
	if strings.ContainsAny(value, "\r\n\x00") {
		return invalid("%s must not contain line breaks", field)
	}
	return nil
}

func cleanPackages(values []string) []string {
	var out []string
	for _, raw := range values {
		line := stripComment(strings.TrimSpace(raw))
		switch {
		case line == "":
		case strings.HasPrefix(line, "-"):
			out = append(out, line)
		default:
			out = append(out, strings.Join(strings.Fields(line), ""))
		}
	}
	return out
}

func stripComment(value string) string {
	for n, c := range value {
		if c == '#' && (n == 0 || value[n-1] == ' ' || value[n-1] == '\t') {
			return strings.TrimSpace(value[:n])
		}
	}
	return value
}

func projectKind(kind apitypes.ImageStepKind) bool {
	switch kind {
	case apitypes.UvProject, apitypes.PoetryProject, apitypes.Pyproject, apitypes.MicromambaEnvironment:
		return true
	case apitypes.Shell, apitypes.Pip, apitypes.Micromamba:
	}
	return false
}

// cleanStep normalizes a step; nil means it does nothing.
func cleanStep(step apitypes.ImageStep) (*apitypes.ImageStep, error) {
	var args, groups []string
	if step.Args != nil {
		args = *step.Args
	}
	if step.Groups != nil {
		groups = *step.Groups
	}
	for _, value := range append(slices.Clone(args), groups...) {
		if err := oneLine("step arguments", value); err != nil {
			return nil, err
		}
	}
	out := apitypes.ImageStep{Kind: step.Kind}
	switch step.Kind {
	case apitypes.Shell:
		if step.Command == nil || strings.TrimSpace(*step.Command) == "" {
			return nil, nil //nolint:nilnil // An empty command is no step.
		}
		command := strings.TrimSpace(*step.Command)
		out.Command = &command
	case apitypes.Pip:
		args = cleanPackages(args)
	case apitypes.Micromamba:
		var kept []string
		for _, a := range args {
			if a = strings.TrimSpace(a); a != "" {
				kept = append(kept, a)
			}
		}
		args = kept
	case apitypes.UvProject, apitypes.PoetryProject, apitypes.Pyproject, apitypes.MicromambaEnvironment:
		if len(args) == 0 || args[0] == "" {
			return nil, fmt.Errorf("%s needs a project path", step.Kind)
		}
		if step.Kind == apitypes.MicromambaEnvironment && len(args) != 2 {
			return nil, fmt.Errorf("micromamba_environment needs the environment file and the Python version")
		}
	default:
		return nil, fmt.Errorf("unknown step kind %q", step.Kind)
	}
	if step.Kind != apitypes.Shell {
		if len(args) == 0 {
			return nil, nil //nolint:nilnil // An install of nothing is no step.
		}
		out.Args = &args
	}
	if len(groups) > 0 {
		out.Groups = &groups
	}
	return &out, nil
}

// baseReference is the base image the definition starts from, or "" for a
// Dockerfile, which names its own.
func (s spec) baseReference(managedBase string) string {
	if s.dockerfile != "" {
		return ""
	}
	if s.base != "" {
		return s.base
	}
	return strings.ReplaceAll(managedBase, "{version}", s.python)
}

// render writes the Dockerfile. pin maps each FROM image to its reference by
// digest; see pinDockerfile for what else the result must satisfy.
func (s spec) render(managedBase string, pin func(string) (string, error)) (string, error) {
	var lines []string
	if s.dockerfile != "" {
		lines = strings.Split(strings.TrimRight(s.dockerfile, "\n"), "\n")
	} else {
		lines = []string{"FROM " + s.baseReference(managedBase)}
		// Project steps copy their manifests into the working directory.
		if slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool { return projectKind(step.Kind) }) {
			lines = append(lines, "WORKDIR "+workdir)
		}
	}
	user := len(lines)
	for _, key := range sortedKeys(s.env) {
		lines = append(lines, "ENV "+key+"="+dockerValue(s.env[key]))
	}
	python, setup := s.pythonSetup(managedBase)
	lines = append(lines, setup...)
	managed := python != "python"
	commands, err := s.installCommands(python, managed)
	if err != nil {
		return "", err
	}
	for _, cmd := range commands {
		lines = append(lines, cmd...)
	}
	if slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool { return projectKind(step.Kind) }) {
		parts := strings.Split(s.python, ".")
		tuple := strings.Join(parts, ", ")
		check := fmt.Sprintf("import sys; assert sys.version_info[:%d] == (%s), sys.version", len(parts), tuple)
		lines = append(lines, "RUN python -c "+shellQuote(check))
	}
	s.addRunFlags(lines[user:])
	return pinDockerfile(strings.Join(s.withSecretVersions(lines), "\n")+"\n", pin)
}

// secretVersionsArg is the build argument that carries a digest of the
// build secrets' versions.
const secretVersionsArg = "LAZYCLOUD_BUILD_SECRET_VERSIONS" //nolint:gosec // A name, not a credential.

// withSecretVersions declares secretVersionsArg after every FROM. BuildKit
// keeps a secret's value out of a step's cache key, so without it a rotated
// secret would reuse the workspace's cached steps; a declared argument is
// part of the environment of every RUN after it, which makes the version
// part of their key. The digest names versions, not values.
func (s spec) withSecretVersions(lines []string) []string {
	if s.secretVersionsKey == "" {
		return lines
	}
	out := make([]string, 0, len(lines)+1)
	for _, line := range lines {
		out = append(out, line)
		if fields := strings.Fields(line); len(fields) > 0 && strings.EqualFold(fields[0], "FROM") {
			out = append(out, "ARG "+secretVersionsArg+"="+s.secretVersionsKey)
		}
	}
	return out
}

// gpuDevice names every GPU in the build container's CDI spec, which lists
// only the GPUs the container holds.
const gpuDevice = "nvidia.com/gpu=*"

// addRunFlags gives each RUN instruction of the rendered steps every build
// secret, as a secret mount the step sees as an environment variable, and
// for a GPU build the container's GPUs. Neither reaches a layer or the
// image's history. A Dockerfile's own RUN lines name the mounts they need.
func (s spec) addRunFlags(lines []string) {
	var flags []string
	for _, name := range s.secrets {
		flags = append(flags, "--mount=type=secret,id="+name+",env="+name+",required=true")
	}
	if s.gpu != "" {
		flags = append(flags, "--device="+gpuDevice)
	}
	if len(flags) == 0 {
		return
	}
	prefix := "RUN " + strings.Join(flags, " ") + " "
	for n, line := range lines {
		if strings.HasPrefix(line, "RUN ") {
			lines[n] = prefix + strings.TrimPrefix(line, "RUN ")
		}
	}
}

// pythonSetup returns the interpreter later steps install with and the
// instructions that provide it.
func (s spec) pythonSetup(managedBase string) (string, []string) {
	needsUV := slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool {
		return projectKind(step.Kind) && step.Kind != apitypes.MicromambaEnvironment
	})
	uv := []string{}
	if needsUV {
		uv = []string{"COPY --from=" + uvImage + " /uv /uvx /usr/local/bin/"}
	}
	if s.micromamba {
		lines := append([]string{
			"COPY --from=" + micromambaImage + " /bin/micromamba /usr/local/bin/",
			"ENV MAMBA_ROOT_PREFIX=/opt/micromamba",
			"ENV PATH=${MAMBA_ROOT_PREFIX}/bin:${PATH}",
		}, uv...)
		hasEnvironment := slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool { return step.Kind == apitypes.MicromambaEnvironment })
		if !hasEnvironment {
			lines = append(lines, "RUN micromamba create -y -n base -c conda-forge python="+s.python+" pip && micromamba clean --all --yes")
		}
		return "python", lines
	}
	if s.baseProvidesPython(managedBase) {
		return "python", uv
	}
	runtime := "/usr/local/bin/python" + minorVersion(s.python)
	return runtime, []string{
		"COPY --from=" + uvImage + " /uv /uvx /usr/local/bin/",
		"ENV UV_PYTHON_INSTALL_DIR=/opt/python",
		"RUN " + managedPythonInstall(s.python),
	}
}

// baseProvidesPython reports whether the base is a python image of the
// requested version, as the managed base is.
func (s spec) baseProvidesPython(managedBase string) bool {
	ref := s.baseReference(managedBase)
	if s.dockerfile != "" {
		ref = dockerfileBase(s.dockerfile)
	}
	repository, tag, digest := splitReference(ref)
	if repository[strings.LastIndex(repository, "/")+1:] != "python" {
		return false
	}
	if digest != "" && tag == "" {
		return true
	}
	return tag == s.python || strings.HasPrefix(tag, s.python+"-") || strings.HasPrefix(tag, s.python+".")
}

// managedPythonInstall installs exactly version with uv and links it as
// python, python3, python3.X and pip.
func managedPythonInstall(version string) string {
	minor := minorVersion(version)
	parts := strings.Split(version, ".")
	check := shellQuote(fmt.Sprintf("import sys; raise SystemExit(0 if sys.version_info[:%d] == (%s) else 1)", len(parts), strings.Join(parts, ", ")))
	prefix := managedPython + "/bin/python"
	return strings.Join([]string{
		"/usr/local/bin/uv python install " + shellQuote(version) + " --managed-python --no-bin",
		"managed=$(/usr/local/bin/uv python find " + shellQuote(version) + " --managed-python --no-project)",
		`"$managed" -c ` + check,
		`"$managed" -m pip --version >/dev/null 2>&1 || "$managed" -m ensurepip`,
		"mkdir -p " + managedPython + "/bin",
		`ln -sf "$managed" ` + prefix,
		"for link in python python3 python" + minor + "; do ln -sf " + prefix + " /usr/local/bin/$link; done",
		`ln -sf "$(dirname "$(readlink -f ` + prefix + `)")/pip" /usr/local/bin/pip`,
	}, " && ")
}

// installCommands renders packages, steps and commands in that order.
// Consecutive pip or micromamba installs merge into one RUN unless a flag
// changes how they resolve.
func (s spec) installCommands(python string, managed bool) ([][]string, error) {
	ordered := make([]apitypes.ImageStep, 0, len(s.steps)+len(s.commands)+1)
	if len(s.packages) > 0 {
		packages := s.packages
		ordered = append(ordered, apitypes.ImageStep{Kind: apitypes.Pip, Args: &packages})
	}
	ordered = append(ordered, s.steps...)
	for _, c := range s.commands {
		command := c
		ordered = append(ordered, apitypes.ImageStep{Kind: apitypes.Shell, Command: &command})
	}

	var out [][]string
	var pendingKind apitypes.ImageStepKind
	var pending []string
	flush := func() {
		if len(pending) > 0 {
			out = append(out, []string{"RUN " + installCommand(pendingKind, pending, python, managed)})
		}
		pendingKind, pending = "", nil
	}
	for _, step := range ordered {
		switch step.Kind {
		case apitypes.Pip, apitypes.Micromamba:
			args := *step.Args
			isolated := slices.ContainsFunc(args, func(a string) bool {
				return slices.ContainsFunc(pipBoundaryFlags(), func(flag string) bool { return strings.Contains(a, flag) })
			})
			if pendingKind != step.Kind || isolated {
				flush()
				pendingKind = step.Kind
			}
			pending = append(pending, args...)
			if isolated {
				flush()
			}
		case apitypes.Shell:
			flush()
			run, err := runShell(*step.Command)
			if err != nil {
				return nil, err
			}
			out = append(out, []string{run})
		case apitypes.UvProject, apitypes.PoetryProject, apitypes.Pyproject, apitypes.MicromambaEnvironment:
			flush()
			out = append(out, projectInstall(step, python))
			// Later installs go into the project's environment.
			python, managed = "python", true
		}
	}
	flush()
	return out, nil
}

func installCommand(kind apitypes.ImageStepKind, args []string, python string, managed bool) string {
	var tokens []string
	for _, a := range args {
		if strings.HasPrefix(a, "-") {
			for _, f := range strings.Fields(a) {
				tokens = append(tokens, shellQuote(f))
			}
			continue
		}
		tokens = append(tokens, shellQuote(a))
	}
	if kind == apitypes.Micromamba {
		return "micromamba install -y -n base " + strings.Join(tokens, " ")
	}
	if managed {
		return "uv pip install --python " + python + " --break-system-packages --compile-bytecode " + strings.Join(tokens, " ")
	}
	return python + " -m pip install --no-cache-dir " + strings.Join(tokens, " ")
}

func projectInstall(step apitypes.ImageStep, python string) []string {
	args := *step.Args
	path, extras := args[0], args[1:]
	var groups []string
	if step.Groups != nil {
		groups = *step.Groups
	}
	if step.Kind == apitypes.MicromambaEnvironment {
		return []string{
			`COPY ["./", "./"]`,
			"RUN " + shellJoin("micromamba", "create", "-y", "-n", "base", "--file", path, "python="+extras[0], "pip") + " && micromamba clean --all --yes",
		}
	}
	var command string
	switch step.Kind {
	case apitypes.UvProject, apitypes.Pyproject:
		parts := []string{"uv", "sync", "--no-default-groups", "--no-install-project", "--no-editable", "--no-python-downloads", "--compile-bytecode", "--python", python, "--project", path}
		if step.Kind == apitypes.UvProject {
			parts = append(parts, "--locked")
		} else {
			parts = append(parts, "--no-config", "--no-sources", "--upgrade")
		}
		for _, e := range extras {
			parts = append(parts, "--extra", e)
		}
		for _, g := range groups {
			parts = append(parts, "--group", g)
		}
		command = shellJoin(parts...)
	case apitypes.PoetryProject:
		poetry := poetryEnvironment + "/bin/poetry"
		sync := []string{poetry, "--directory", path, "sync", "--no-root", "--no-interaction", "--only", strings.Join(append([]string{"main"}, groups...), ",")}
		for _, e := range extras {
			sync = append(sync, "--extras", e)
		}
		env := "VIRTUAL_ENV=" + projectEnvironment + " POETRY_VIRTUALENVS_CREATE=false "
		command = strings.Join([]string{
			shellJoin("uv", "venv", "--python", python, "--no-python-downloads", poetryEnvironment),
			shellJoin("uv", "pip", "install", "--python", poetryEnvironment+"/bin/python", "poetry=="+poetryVersion),
			shellJoin("uv", "venv", "--python", python, "--no-python-downloads", projectEnvironment),
			env + shellJoin(poetry, "--directory", path, "check", "--lock"),
			env + shellJoin(sync...),
		}, " && ")
	case apitypes.Shell, apitypes.Pip, apitypes.Micromamba, apitypes.MicromambaEnvironment:
	}
	return []string{
		`COPY ["./", "./"]`,
		"ENV UV_PROJECT_ENVIRONMENT=" + projectEnvironment,
		"RUN " + command,
		"ENV VIRTUAL_ENV=${UV_PROJECT_ENVIRONMENT}",
		"ENV PATH=${VIRTUAL_ENV}/bin:${PATH}",
	}
}

// runShell writes a shell step as a heredoc, so the Dockerfile parser never
// reads the command. The delimiter derives from the command, which keeps the
// rendering deterministic; a command holding that line is refused.
func runShell(command string) (string, error) {
	sum := sha256.Sum256([]byte(command))
	delimiter := "LAZYCLOUD_" + hex.EncodeToString(sum[:8])
	if slices.Contains(strings.Split(command, "\n"), delimiter) {
		return "", invalid("a command contains the line %s", delimiter)
	}
	return "RUN <<'" + delimiter + "'\n" + command + "\n" + delimiter, nil
}

// toolImages are the images rendered steps copy tools from. They are pinned
// by digest and need no access check.
func toolImage(ref string) bool { return ref == uvImage || ref == micromambaImage }

// directive matches a parser directive such as "# syntax=...".
var directive = regexp.MustCompile(`^#\s*[A-Za-z][A-Za-z0-9]*\s*=`)

// pinDockerfile parses the whole Dockerfile with BuildKit's parser and pins
// every FROM image through pin, which also checks the caller may read it.
// Every other way to name an image is refused, because nothing would check
// access to it: parser directives (a "# syntax=" frontend runs as code),
// COPY --from and RUN --mount from= naming anything but an earlier stage or a
// tool image, and variables in image names.
func pinDockerfile(dockerfile string, pin func(string) (string, error)) (string, error) {
	data := []byte(dockerfile)
	if _, _, _, found := parser.DetectSyntax(data); found {
		return "", invalid("Dockerfile parser directives such as # syntax= are not supported")
	}
	for _, line := range strings.Split(dockerfile, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if directive.MatchString(line) {
			return "", invalid("Dockerfile parser directives such as %q are not supported", line)
		}
		if !strings.HasPrefix(line, "#") {
			break
		}
	}
	result, err := parser.Parse(bytes.NewReader(data))
	if err != nil {
		return "", invalid("the Dockerfile does not parse: %v", err)
	}
	lines := strings.Split(dockerfile, "\n")
	stages := map[string]bool{}
	count := 0
	checkFrom := func(from, what string) error {
		if from == "" || stages[strings.ToLower(from)] || toolImage(from) {
			return nil
		}
		if n, err := strconv.Atoi(from); err == nil && n >= 0 && n < count {
			return nil
		}
		return invalid("%s names %q; name an image in FROM ... AS <stage> and use the stage", what, from)
	}
	for _, node := range result.AST.Children {
		instruction, err := instructions.ParseInstruction(node)
		if err != nil {
			return "", invalid("the Dockerfile does not parse: %v", err)
		}
		switch c := instruction.(type) {
		case *instructions.Stage:
			base := c.BaseName
			if strings.Contains(base, "$") {
				return "", invalid("FROM %s uses a variable; name the image directly", base)
			}
			if !stages[strings.ToLower(base)] && !strings.EqualFold(base, "scratch") {
				pinned, err := pin(base)
				if err != nil {
					return "", err
				}
				from := append([]string{"FROM"}, node.Flags...)
				from = append(from, pinned)
				if c.Name != "" {
					from = append(from, "AS", c.Name)
				}
				lines[node.StartLine-1] = strings.Join(from, " ")
				for n := node.StartLine; n < node.EndLine; n++ {
					lines[n] = ""
				}
			}
			if c.Name != "" {
				stages[strings.ToLower(c.Name)] = true
			}
			count++
		case *instructions.CopyCommand:
			if err := checkFrom(c.From, "COPY --from"); err != nil {
				return "", err
			}
		case *instructions.RunCommand:
			for _, mount := range instructions.GetMounts(c) {
				if err := checkFrom(mount.From, "RUN --mount from="); err != nil {
					return "", err
				}
			}
		}
	}
	return strings.Join(lines, "\n"), nil
}

// dockerfileBase is the image the first FROM names, or "" when there is none.
func dockerfileBase(dockerfile string) string {
	result, err := parser.Parse(strings.NewReader(dockerfile))
	if err != nil {
		return ""
	}
	for _, node := range result.AST.Children {
		if strings.EqualFold(node.Value, "from") && node.Next != nil {
			return node.Next.Value
		}
	}
	return ""
}

// splitReference splits name[:tag][@digest].
func splitReference(ref string) (repository, tag, digest string) {
	repository, digest, _ = strings.Cut(ref, "@")
	if slash := strings.LastIndex(repository, "/"); strings.LastIndex(repository, ":") > slash {
		colon := strings.LastIndex(repository, ":")
		repository, tag = repository[:colon], repository[colon+1:]
	}
	return repository, tag, digest
}

// buildInputs are what a build reads besides its Dockerfile and context.
// Empty fields leave the identity of an image without them unchanged.
type buildInputs struct {
	// Workspace is set when the build reads its secrets.
	Workspace string `json:"workspace,omitempty"`
	// Secrets maps each secret to its version.
	Secrets map[string]string `json:"secrets,omitempty"`
	GPU     string            `json:"gpu,omitempty"`
}

// imageDigest is the image identity: the rendered Dockerfile, the
// architecture, the context the build reads and its other inputs.
func imageDigest(dockerfile, architecture string, context []byte, inputs buildInputs) []byte {
	encoded, _ := json.Marshal(struct { //nolint:errchkjson // Strings, bytes and string maps always encode.
		Version      int    `json:"version"`
		Architecture string `json:"architecture"`
		Dockerfile   string `json:"dockerfile"`
		Context      []byte `json:"context"`
		buildInputs
	}{identityVersion, architecture, dockerfile, context, inputs})
	sum := sha256.Sum256(encoded)
	return sum[:]
}

// needsBuild reports whether a pinned Dockerfile does more than name its
// base.
func needsBuild(dockerfile string) bool {
	result, err := parser.Parse(strings.NewReader(dockerfile))
	if err != nil {
		return true
	}
	return len(result.AST.Children) != 1 || !strings.EqualFold(result.AST.Children[0].Value, "from")
}

func dockerValue(value string) string {
	if plainEnvValue.MatchString(value) {
		return value
	}
	quoted, _ := json.Marshal(value) //nolint:errchkjson // A string always encodes.
	return string(quoted)
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

// shellQuote quotes s for sh unless it is plainly safe.
func shellQuote(s string) string {
	if s != "" && strings.IndexFunc(s, func(r rune) bool {
		return (r < 'a' || r > 'z') && (r < 'A' || r > 'Z') && (r < '0' || r > '9') && !strings.ContainsRune("@%+=:,./-_", r)
	}) < 0 {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

func shellJoin(parts ...string) string {
	quoted := make([]string, len(parts))
	for n, p := range parts {
		quoted[n] = shellQuote(p)
	}
	return strings.Join(quoted, " ")
}

func parseSha256(s string) ([]byte, error) {
	if len(s) != 64 {
		return nil, fmt.Errorf("digest has %d characters", len(s))
	}
	out := make([]byte, 32)
	for n := range out {
		v, err := strconv.ParseUint(s[2*n:2*n+2], 16, 8)
		if err != nil || strings.ToLower(s[2*n:2*n+2]) != s[2*n:2*n+2] {
			return nil, fmt.Errorf("digest is not lowercase hex")
		}
		out[n] = byte(v)
	}
	return out, nil
}
