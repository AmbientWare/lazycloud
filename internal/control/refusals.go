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
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

const (
	gib = int64(1) << 30
	// minRootDiskBytes is the smallest root disk a devbox takes. Disks bill
	// for what they store, so the floor costs nothing.
	minRootDiskBytes = 10 * gib
	// rootHeadroomBytes is the room a devbox's root disk keeps beyond its
	// image's unpacked root.
	rootHeadroomBytes = 2 * gib
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
	return apitypes.DeploymentPlanWorkload{
		Kind: spec.Kind, Name: spec.Name, Resources: &spec.Resources, Autoscaler: spec.Autoscaler,
		Placement: spec.Placement, Disks: spec.Disks, Secrets: spec.Secrets,
	}
}

// refusals are what would refuse workloads once deployed to workspace: the
// plan rules admission applies at each start, disks past the plan's
// allowance or below a devbox's minimum, and secrets the workspace lacks.
// Funds are left to admission, since they change after a deploy.
func refusals(ctx context.Context, db DBTX, workspace uuid.UUID, workloads []apitypes.DeploymentPlanWorkload) ([]apitypes.DeploymentRefusal, error) {
	allowance, err := billing.ReadAllowance(ctx, db, workspace)
	if err != nil {
		return nil, err
	}
	var out []apitypes.DeploymentRefusal
	add := func(w apitypes.DeploymentPlanWorkload, gate apitypes.DeploymentGate, message, remedy string) {
		out = append(out, apitypes.DeploymentRefusal{Kind: w.Kind, Name: w.Name, Gate: gate, Message: message, Remedy: remedy})
	}
	declared := map[string]int64{}
	var named []string
	for _, w := range workloads {
		for _, r := range allowance.Refusals(declaredOf(w)) {
			add(w, r.Gate, r.Message, r.Remedy)
		}
		for _, d := range disksOf(w) {
			declared[d.Name] = max(declared[d.Name], d.SizeBytes)
			if d.MountPath == "/" && d.SizeBytes < minRootDiskBytes {
				add(w, apitypes.DiskMinimum, fmt.Sprintf("the root disk %s is %s GiB, below the %d GiB a devbox needs", d.Name, gibOf(d.SizeBytes), minRootDiskBytes/gib),
					fmt.Sprintf("set disk to at least %d GiB", minRootDiskBytes/gib))
			}
		}
		if w.Secrets != nil {
			named = append(named, *w.Secrets...)
		}
	}
	if len(declared) > 0 {
		growth, err := storage.DeclaredDiskGrowth(ctx, db, workspace, declared)
		if err != nil {
			return nil, err
		}
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
		return nil, err
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
	return out, nil
}

// checkDeploy refuses, with a RefusedError, resolved specs that would fail
// once deployed to workspace.
func checkDeploy(ctx context.Context, db DBTX, workspace uuid.UUID, specs []apitypes.WorkloadSpec) error {
	workloads := make([]apitypes.DeploymentPlanWorkload, len(specs))
	for n, spec := range specs {
		workloads[n] = planned(spec)
	}
	refused, err := refusals(ctx, db, workspace, workloads)
	if err != nil {
		return err
	}
	sized, err := imageRefusals(ctx, db, workspace, specs)
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
// unpacked root, which a devbox's first start copies onto it. An image is
// sized once its build has converted it, so these follow the builds.
func imageRefusals(ctx context.Context, db DBTX, workspace uuid.UUID, specs []apitypes.WorkloadSpec) ([]apitypes.DeploymentRefusal, error) {
	var out []apitypes.DeploymentRefusal
	for _, spec := range specs {
		root := rootDisk(spec)
		if root == nil || spec.Image.ImageId == nil {
			continue
		}
		unpacked, ok, err := images.UnpackedBytes(ctx, db, workspace, *spec.Image.ImageId)
		if err != nil {
			return nil, err
		}
		need := (unpacked+gib-1)/gib*gib + rootHeadroomBytes
		if !ok || root.SizeBytes >= need {
			continue
		}
		out = append(out, apitypes.DeploymentRefusal{
			Kind: spec.Kind, Name: spec.Name, Gate: apitypes.DiskImage,
			Message: fmt.Sprintf("the root disk %s of %s GiB is too small for its image's unpacked root of up to %s GiB",
				root.Name, gibOf(root.SizeBytes), gibOf(unpacked)),
			Remedy: fmt.Sprintf("set disk to at least %d GiB", need/gib),
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

// declaredOf is what billing holds w to. Without an autoscaler a workload
// keeps no warm floor.
func declaredOf(w apitypes.DeploymentPlanWorkload) billing.Declared {
	var d billing.Declared
	if w.Resources != nil {
		d.GPUs = gpuCount(*w.Resources)
		if w.Resources.Gpu != nil {
			for _, g := range *w.Resources.Gpu {
				d.GPUModels = append(d.GPUModels, billing.GPUType(g))
			}
		}
	}
	if p := w.Placement; p != nil {
		d.Pinned = (p.Region != nil && *p.Region != "") || (p.AvailabilityZone != nil && *p.AvailabilityZone != "")
		d.Machine = p.Machine != nil && *p.Machine != ""
	}
	if a := w.Autoscaler; a != nil && a.MinContainers != nil {
		d.MinContainers = *a.MinContainers
	}
	return d
}

// gpuCount is the cards each container holds: gpu_count, else one when a
// model is named.
func gpuCount(r apitypes.Resources) int {
	if r.GpuCount != nil && *r.GpuCount > 0 {
		return *r.GpuCount
	}
	if r.Gpu != nil && len(*r.Gpu) > 0 {
		return 1
	}
	return 0
}

// gibOf is bytes in GiB to one decimal place, without a trailing ".0".
func gibOf(bytes int64) string {
	return strings.TrimSuffix(fmt.Sprintf("%.1f", float64(bytes)/float64(gib)), ".0")
}
