package control

import (
	"cmp"
	"context"
	"fmt"
	"slices"
	"strings"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/rootdisk"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// RefusedError refuses a deploy before it changes anything, with every
// reason its workloads would fail once deployed.
type RefusedError struct{ Refusals []apitypes.DeploymentRefusal }

func (e *RefusedError) Error() string {
	lines := make([]string, len(e.Refusals))
	for n, r := range e.Refusals {
		lines[n] = fmt.Sprintf("%s %s: %s; %s", r.Kind, r.Name, r.Message, r.Remedy)
	}
	return "the deploy is refused: " + strings.Join(lines, "; ")
}

// planned is what the deploy checks of a resolved spec.
func planned(spec apitypes.WorkloadSpec) apitypes.DeploymentPlanWorkload {
	w := apitypes.DeploymentPlanWorkload{
		Kind: spec.Kind, Name: spec.Name, Resources: &spec.Resources, Autoscaler: spec.Autoscaler,
		Placement: spec.Placement, Disks: spec.Disks, Secrets: spec.Secrets,
	}
	if spec.Pod != nil {
		w.PodKind = &spec.Pod.Kind
	}
	return w
}

// refusals are what would refuse workloads once deployed to workspace: the
// plan rules admission applies at each start, disks past the plan's
// allowance or a devbox root below the minimum, and secrets the workspace
// lacks. Funds are left to admission, since they change after a deploy.
// growing are the declared disks the deploy would create or grow.
func refusals(ctx context.Context, db DBTX, workspace uuid.UUID, workloads []apitypes.DeploymentPlanWorkload) (out []apitypes.DeploymentRefusal, growing []string, err error) {
	allowance, err := billing.ReadAllowance(ctx, db, workspace)
	if err != nil {
		return nil, nil, err
	}
	add := func(w apitypes.DeploymentPlanWorkload, gate apitypes.DeploymentGate, message, remedy string) {
		out = append(out, apitypes.DeploymentRefusal{Kind: w.Kind, Name: w.Name, Gate: gate, Message: message, Remedy: remedy})
	}
	declared := map[string]int64{}
	var named []string
	for _, w := range workloads {
		for _, r := range allowance.Refusals(billing.DeclaredBy(w.Resources, w.Placement, w.Autoscaler)) {
			add(w, r.Gate, r.Message, r.Remedy)
		}
		for _, d := range disksOf(w) {
			declared[d.Name] = max(declared[d.Name], d.SizeBytes)
			if devbox := w.PodKind != nil && *w.PodKind == apitypes.PodKindDevbox; devbox && d.MountPath == "/" && d.SizeBytes < rootdisk.MinBytes {
				add(w, apitypes.DiskMinimum,
					fmt.Sprintf("the root disk %s is %s GiB, below the %s GiB a devbox needs", d.Name, rootdisk.GiB(d.SizeBytes), rootdisk.GiB(rootdisk.MinBytes)),
					fmt.Sprintf("set disk to at least %s GiB", rootdisk.GiB(rootdisk.MinBytes)))
			}
		}
		if w.Secrets != nil {
			named = append(named, *w.Secrets...)
		}
	}
	if len(declared) > 0 {
		growth, err := storage.DeclaredDiskGrowth(ctx, db, workspace, declared)
		if err != nil {
			return nil, nil, err
		}
		growing = growth.Growing
		if r := allowance.DiskRefusal(growth.TotalBytes); r != nil && len(growth.Growing) > 0 {
			for _, w := range workloads {
				if slices.ContainsFunc(disksOf(w), func(d apitypes.DiskMountSpec) bool { return slices.Contains(growth.Growing, d.Name) }) {
					add(w, r.Gate, r.Message, r.Remedy)
				}
			}
		}
	}
	slices.Sort(named)
	missing, err := secrets.Missing(ctx, db, workspace, slices.Compact(named))
	if err != nil {
		return nil, nil, err
	}
	for _, w := range workloads {
		var lacks []string
		if w.Secrets != nil {
			lacks = slices.DeleteFunc(slices.Clone(*w.Secrets), func(name string) bool { return !slices.Contains(missing, name) })
		}
		if len(lacks) > 0 {
			add(w, apitypes.MissingSecrets, "the workspace has no secret named "+strings.Join(lacks, ", "),
				fmt.Sprintf("create it with `lazycloud secret create %s VALUE`", lacks[0]))
		}
	}
	slices.SortStableFunc(out, byWorkload)
	return out, growing, nil
}

// checkDeploy refuses, with a RefusedError, resolved specs that would fail
// once deployed to workspace.
func checkDeploy(ctx context.Context, db DBTX, workspace uuid.UUID, specs []apitypes.WorkloadSpec) error {
	workloads := make([]apitypes.DeploymentPlanWorkload, len(specs))
	for n, spec := range specs {
		workloads[n] = planned(spec)
	}
	refused, growing, err := refusals(ctx, db, workspace, workloads)
	if err != nil {
		return err
	}
	sized, err := imageRefusals(ctx, db, workspace, specs, growing)
	if err != nil {
		return err
	}
	if refused = append(refused, sized...); len(refused) > 0 {
		slices.SortStableFunc(refused, byWorkload)
		return &RefusedError{Refusals: refused}
	}
	return nil
}

func byWorkload(a, b apitypes.DeploymentRefusal) int {
	return cmp.Or(cmp.Compare(a.Kind, b.Kind), cmp.Compare(a.Name, b.Name))
}

// imageRefusals refuse devboxes whose root disk cannot hold their image's
// unpacked root, which a devbox's first start copies onto it. Only a root
// disk the deploy creates or grows is checked: one that exists at the size
// was seeded already, and later starts copy nothing. An image is sized once
// its build has converted it, so these follow the builds.
func imageRefusals(ctx context.Context, db DBTX, workspace uuid.UUID, specs []apitypes.WorkloadSpec, growing []string) ([]apitypes.DeploymentRefusal, error) {
	var out []apitypes.DeploymentRefusal
	for _, spec := range specs {
		root := rootDisk(spec)
		if root == nil || spec.Image.ImageId == nil || !slices.Contains(growing, root.Name) {
			continue
		}
		unpacked, ok, err := images.UnpackedBytes(ctx, db, workspace, *spec.Image.ImageId)
		if err != nil {
			return nil, err
		}
		need := rootdisk.Needed(unpacked)
		if !ok || root.SizeBytes >= need {
			continue
		}
		out = append(out, apitypes.DeploymentRefusal{
			Kind: spec.Kind, Name: spec.Name, Gate: apitypes.DiskImage,
			Message: fmt.Sprintf("the root disk %s of %s GiB is too small for its image's unpacked root of up to %s GiB",
				root.Name, rootdisk.GiB(root.SizeBytes), rootdisk.GiB(unpacked)),
			Remedy: fmt.Sprintf("set disk to at least %s GiB", rootdisk.GiB(need)),
		})
	}
	return out, nil
}

func rootDisk(spec apitypes.WorkloadSpec) *apitypes.DiskMountSpec {
	if spec.Pod == nil || spec.Pod.Kind != apitypes.PodKindDevbox || spec.Disks == nil {
		return nil
	}
	for n, d := range *spec.Disks {
		if d.MountPath == "/" {
			return &(*spec.Disks)[n]
		}
	}
	return nil
}

func disksOf(w apitypes.DeploymentPlanWorkload) []apitypes.DiskMountSpec {
	if w.Disks == nil {
		return nil
	}
	return *w.Disks
}
