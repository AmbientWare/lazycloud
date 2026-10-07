package billing

import (
	"context"
	"fmt"
	"slices"
	"strings"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Declared is what one workload declares that its account's plan must allow
// whatever the account's funds. A deploy holds every workload to it before
// anything changes; Admit holds each start to the region and GPU model rules
// again.
type Declared struct {
	// GPUs is the cards each container holds; 0 is a CPU container.
	GPUs int
	// GPUModels are the models the work accepts; empty or GPUAny accepts
	// any model the account may use.
	GPUModels []GPUType
	// Pinned marks placement in a chosen region or zone.
	Pinned bool
	// Machine marks work pinned to a joined machine.
	Machine bool
	// MinContainers is the warm floor the workload keeps running.
	MinContainers int
}

// Refusal is a plan rule a workload breaks. Message names the limit and
// Remedy how the account lifts it.
type Refusal struct {
	Gate    apitypes.DeploymentGate
	Message string
	Remedy  string
}

// sentence is the refusal as one error message.
func (r Refusal) sentence() string { return r.Message + "; " + r.Remedy }

// Allowance is what a workspace's account may deploy, read once for a
// deploy. Funds are not part of it: they change after a deploy, so Admit
// checks them at each start.
type Allowance struct {
	s         standing
	connected bool
}

// ReadAllowance reads the plan entitlements of workspace's owner.
func ReadAllowance(ctx context.Context, db DBTX, workspace uuid.UUID) (Allowance, error) {
	q := New(db)
	ws, err := q.AdmissionWorkspace(ctx, workspace)
	if err != nil {
		return Allowance{}, fmt.Errorf("read workspace owner: %w", err)
	}
	s, err := readStanding(ctx, q, ws.UserID)
	if err != nil {
		return Allowance{}, err
	}
	return Allowance{s: s, connected: ws.Connected}, nil
}

// Refusals are the plan rules d breaks.
func (a Allowance) Refusals(d Declared) []Refusal {
	s, e := a.s, a.s.entitlements
	fleet := !d.Machine && !a.connected
	var out []Refusal
	if d.Pinned && !e.RegionSelection {
		out = append(out, s.regionRefusal())
	}
	if d.GPUs == 0 {
		if d.MinContainers > e.MaxCPUContainers {
			out = append(out, Refusal{
				Gate: apitypes.WarmFloor,
				Message: fmt.Sprintf("min_containers %d keeps more containers running than the %d the plan allows",
					d.MinContainers, e.MaxCPUContainers),
				Remedy: s.lift(func(e Entitlements) bool { return e.MaxCPUContainers >= d.MinContainers },
					fmt.Sprintf("lower min_containers to %d", e.MaxCPUContainers)),
			})
		}
		return out
	}
	if fleet {
		if err := unoffered(d.GPUModels); err != nil {
			out = append(out, Refusal{Gate: apitypes.GpuUnavailable, Message: err.Error(), Remedy: "name a model the platform fleet offers"})
		}
	}
	if _, refused := e.gpuModels(d.GPUModels, fleet); refused != nil {
		out = append(out, s.gpuModelRefusal(*refused))
	}
	if d.GPUs > e.MaxGPUs {
		out = append(out, Refusal{
			Gate:    apitypes.GpuCount,
			Message: fmt.Sprintf("each container holds %d GPUs, more than the %d the plan allows", d.GPUs, e.MaxGPUs),
			Remedy: s.lift(func(e Entitlements) bool { return e.MaxGPUs >= d.GPUs },
				fmt.Sprintf("lower gpu_count to %d", e.MaxGPUs)),
		})
	} else if floor := d.MinContainers * d.GPUs; floor > e.MaxGPUs {
		out = append(out, Refusal{
			Gate: apitypes.WarmFloor,
			Message: fmt.Sprintf("min_containers %d at %d GPUs each keeps %d GPUs running, more than the %d the plan allows",
				d.MinContainers, d.GPUs, floor, e.MaxGPUs),
			Remedy: s.lift(func(e Entitlements) bool { return e.MaxGPUs >= floor },
				fmt.Sprintf("lower min_containers to %d", e.MaxGPUs/d.GPUs)),
		})
	}
	return out
}

// DiskRefusal is the refusal of disks that would declare declaredBytes in
// the workspace, or nil when the plan allows them.
func (a Allowance) DiskRefusal(declaredBytes int64) *Refusal {
	return a.s.diskRefusal(declaredBytes)
}

func (s standing) regionRefusal() Refusal {
	return Refusal{
		Gate:    apitypes.RegionSelection,
		Message: fmt.Sprintf("the %s plan does not include region or availability zone selection", s.plan.Name),
		Remedy:  s.lift(func(e Entitlements) bool { return e.RegionSelection }, "remove the region and availability zone"),
	}
}

func (s standing) gpuModelRefusal(model GPUType) Refusal {
	return Refusal{
		Gate:    apitypes.GpuModel,
		Message: fmt.Sprintf("this account can use %s, not %s", joinModels(s.entitlements.GPUTypes), model),
		Remedy:  s.lift(func(e Entitlements) bool { return slices.Contains(e.GPUTypes, model) }, "choose one of those models"),
	}
}

func (s standing) diskRefusal(declaredBytes int64) *Refusal {
	allows := func(e Entitlements) bool { return declaredBytes <= int64(e.MaxWorkspaceDiskGiB)*bytesPerGiB }
	if allows(s.entitlements) {
		return nil
	}
	return &Refusal{
		Gate: apitypes.DiskAllowance,
		Message: fmt.Sprintf("this workspace's disks would declare %s GiB, more than its plan allows (%d GiB)",
			strings.TrimSuffix(fmt.Sprintf("%.1f", float64(declaredBytes)/float64(bytesPerGiB)), ".0"), s.entitlements.MaxWorkspaceDiskGiB),
		Remedy: s.lift(allows, "delete disks it no longer needs or declare smaller ones"),
	}
}

// lift is how the account gets entitlements that allows: a saved card on
// its plan, else the cheapest plan above its own; fallback when neither
// does.
func (s standing) lift(allows func(Entitlements) bool, fallback string) string {
	if s.complimentary {
		return fallback
	}
	if !s.hasCard && allows(s.plan.Entitlements) {
		return "add a payment method"
	}
	above := false
	for _, plan := range Plans() {
		if above && allows(plan.Entitlements) {
			return "upgrade to " + plan.Name
		}
		above = above || plan.ID == s.plan.ID
	}
	return fallback
}
