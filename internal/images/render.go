package images

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"

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
	identityVersion = 1
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
	fromLine             = regexp.MustCompile(`(?i)^(\s*FROM(?:\s+--\S+)*\s+)(\S+)(.*)$`)
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
}

// minorVersion is "3.12" for "3.12" and "3.12.11".
func minorVersion(version string) string {
	parts := strings.SplitN(version, ".", 3)
	return parts[0] + "." + parts[1]
}

func validate(def apitypes.ImageDefinition) (spec, error) {
	if def.Gpu != nil && *def.Gpu != "" {
		return spec{}, fmt.Errorf("%w: GPU builds need GPU capacity, which the platform does not offer yet", ErrUnsupported)
	}
	if def.Secrets != nil && len(*def.Secrets) > 0 {
		return spec{}, fmt.Errorf("%w: build secrets need workspace secrets, which the platform does not offer yet", ErrUnsupported)
	}
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
	}
	if def.Dockerfile != nil && strings.TrimSpace(*def.Dockerfile) != "" {
		if s.base != "" {
			return spec{}, invalid("dockerfile builds cannot also set a base image")
		}
		s.dockerfile = strings.TrimRight(*def.Dockerfile, "\n") + "\n"
	}
	if def.PythonPackages != nil {
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
	if def.Context != nil {
		digest, err := parseSha256(def.Context.Sha256)
		if err != nil {
			return spec{}, invalid("context.sha256 is not a lowercase hex digest")
		}
		s.context = digest
	}
	return s, nil
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
// digest.
func (s spec) render(managedBase string, pin func(string) (string, error)) (string, error) {
	var lines []string
	if s.dockerfile != "" {
		pinned, err := pinDockerfile(s.dockerfile, pin)
		if err != nil {
			return "", err
		}
		lines = strings.Split(strings.TrimRight(pinned, "\n"), "\n")
	} else {
		base, err := pin(s.baseReference(managedBase))
		if err != nil {
			return "", err
		}
		lines = []string{"FROM " + base}
		// Project steps copy their manifests into the working directory.
		if slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool { return projectKind(step.Kind) }) {
			lines = append(lines, "WORKDIR "+workdir)
		}
	}
	for _, key := range sortedKeys(s.env) {
		lines = append(lines, "ENV "+key+"="+dockerValue(s.env[key]))
	}
	python, setup := s.pythonSetup(managedBase)
	lines = append(lines, setup...)
	managed := python != "python"
	for _, cmd := range s.installCommands(python, managed) {
		lines = append(lines, cmd...)
	}
	if slices.ContainsFunc(s.steps, func(step apitypes.ImageStep) bool { return projectKind(step.Kind) }) {
		parts := strings.Split(s.python, ".")
		tuple := strings.Join(parts, ", ")
		check := fmt.Sprintf("import sys; assert sys.version_info[:%d] == (%s), sys.version", len(parts), tuple)
		lines = append(lines, "RUN python -c "+shellQuote(check))
	}
	return strings.Join(lines, "\n") + "\n", nil
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
func (s spec) installCommands(python string, managed bool) [][]string {
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
			out = append(out, []string{runShell(*step.Command)})
		case apitypes.UvProject, apitypes.PoetryProject, apitypes.Pyproject, apitypes.MicromambaEnvironment:
			flush()
			out = append(out, projectInstall(step, python))
			// Later installs go into the project's environment.
			python, managed = "python", true
		}
	}
	flush()
	return out
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

// runShell writes a shell step; a multi-line command becomes a heredoc so its
// lines stay one step.
func runShell(command string) string {
	if !strings.Contains(command, "\n") {
		return "RUN " + command
	}
	return "RUN <<'LAZYCLOUD_STEP'\n" + command + "\nLAZYCLOUD_STEP"
}

// pinDockerfile replaces every FROM image with its pinned reference. Stage
// names and scratch stay as they are.
func pinDockerfile(dockerfile string, pin func(string) (string, error)) (string, error) {
	stages := map[string]bool{"scratch": true}
	lines := strings.Split(dockerfile, "\n")
	for n, line := range lines {
		m := fromLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		image, suffix := m[2], m[3]
		if !stages[strings.ToLower(image)] {
			if strings.Contains(image, "$") {
				return "", invalid("Dockerfile FROM %s uses a variable; name the image directly", image)
			}
			pinned, err := pin(image)
			if err != nil {
				return "", err
			}
			lines[n] = m[1] + pinned + suffix
		}
		if fields := strings.Fields(suffix); len(fields) == 2 && strings.EqualFold(fields[0], "as") {
			stages[strings.ToLower(fields[1])] = true
		}
	}
	return strings.Join(lines, "\n"), nil
}

// dockerfileBase is the image the first FROM names.
func dockerfileBase(dockerfile string) string {
	for _, line := range strings.Split(dockerfile, "\n") {
		if m := fromLine.FindStringSubmatch(line); m != nil {
			return m[2]
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

// imageDigest is the image identity: the rendered Dockerfile, the architecture and
// the context the build reads.
func imageDigest(dockerfile, architecture string, context []byte) []byte {
	encoded, _ := json.Marshal(struct { //nolint:errchkjson // Strings and bytes always encode.
		Version      int    `json:"version"`
		Architecture string `json:"architecture"`
		Dockerfile   string `json:"dockerfile"`
		Context      []byte `json:"context"`
	}{identityVersion, architecture, dockerfile, context})
	sum := sha256.Sum256(encoded)
	return sum[:]
}

// needsBuild reports whether a Dockerfile does more than name its base.
func needsBuild(dockerfile string) bool {
	for _, line := range strings.Split(strings.TrimSpace(dockerfile), "\n") {
		if fromLine.MatchString(line) {
			continue
		}
		return true
	}
	return false
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
