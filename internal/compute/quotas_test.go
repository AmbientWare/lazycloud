package compute_test

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

func TestQuotaRoomIsTheQuotaLessRunningPlatformVCPUsAndARefusalCoolsTheClass(t *testing.T) {
	o, emulator, ec2 := reserveFleet(t)
	quotas := map[string]float64{
		"L-1216C47A": 64, "L-34B43A08": 32, "L-DB2E81BA": 8, "L-3819A6DF": 0, "L-417A185B": 0, "L-7212CCBC": 0,
	}
	emulator.on("GetServiceQuota", func(call awsCall) awsReply {
		if !strings.Contains(call.Header.Get("Authorization"), "/us-east-2/") {
			return awsReply{status: http.StatusBadRequest, body: `{"__type":"AccessDeniedException","Message":"denied"}`}
		}
		var in struct{ ServiceCode, QuotaCode string }
		if err := json.Unmarshal(call.Body, &in); err != nil || in.ServiceCode != "ec2" {
			t.Errorf("GetServiceQuota %s, want an EC2 quota", call.Body)
		}
		return ok(fmt.Sprintf(`{"Quota":{"ServiceCode":"ec2","QuotaCode":%q,"Value":%g,"Unit":"None"}}`, in.QuotaCode, quotas[in.QuotaCode]))
	})
	stored, err := o.compute.RefreshQuotas(t.Context(), discard())
	if stored != 6 || err == nil || !strings.Contains(err.Error(), "us-west-1") {
		t.Fatalf("refresh stored %d (%v), want six us-east-2 quotas and the us-west-1 failure", stored, err)
	}
	for _, instance := range []string{"i-0000000000000f001", "i-0000000000000f002"} {
		host := cloudHost(t, o, compute.MarketSpot, instance)
		run(t, o.pool, "update hosts set instance_type = 'm7i.large' where id = $1", uuid.UUID(host))
	}
	// A stopped reserve holds no running quota.
	reserve(t, o, compute.PhaseStopped, compute.MarketSpot, compute.ReserveStop, "i-0000000000000f003")

	// A G Spot launch refused for quota cools every G Spot type there.
	ec2.refuse = func(call awsCall) (awsReply, bool) {
		return ec2Error(http.StatusBadRequest, "VcpuLimitExceeded", "You have requested more vCPU capacity than your current vCPU limit"),
			call.Action == "RunInstances"
	}
	run(t, o.pool, `insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, gpu_type, gpu_count, region, instance_type, market)
values ('g', 'offline', 'platform', 'aws', 'requested', 3500, 14::bigint << 30, 'T4', 1, 'us-east-2', 'g4dn.xlarge', 'spot')`)
	if n := launch(t, o); n != 0 {
		t.Fatalf("launched %d past the quota", n)
	}

	rooms, err := o.compute.QuotaRooms(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]compute.QuotaRoom{}
	for _, r := range rooms {
		got[r.Region+"/"+string(r.Class)+"/"+string(r.Market)] = r
	}
	if r := got["us-east-2/standard/spot"]; !r.Known || r.VCPUs != 28 {
		t.Errorf("standard spot room %+v, want 32 less two running m7i.large", r)
	}
	if r := got["us-east-2/standard/on_demand"]; !r.Known || r.VCPUs != 64 {
		t.Errorf("standard on-demand room %+v, want the whole 64", r)
	}
	if r := got["us-east-2/g/spot"]; !r.Known || !r.Refused || r.VCPUs != 0 {
		t.Errorf("G spot room %+v, want cooled by the refusal", r)
	}
	if r := got["us-east-2/g/on_demand"]; !r.Known || r.Refused || r.VCPUs != 8 {
		t.Errorf("G on-demand room %+v, want 8 and not cooled", r)
	}
	if _, read := got["us-west-1/standard/spot"]; read {
		t.Errorf("us-west-1 quotas %v stored after a failed read", got)
	}
}

func TestQuotaClassFollowsTheInstanceFamily(t *testing.T) {
	for instanceType, want := range map[string]compute.QuotaClass{
		"m7i.large": compute.QuotaStandard, "c6a.8xlarge": compute.QuotaStandard, "r6a.2xlarge": compute.QuotaStandard,
		"g4dn.xlarge": compute.QuotaG, "g6e.12xlarge": compute.QuotaG, "p5.48xlarge": compute.QuotaP, "p4de.24xlarge": compute.QuotaP,
	} {
		if got, ok := compute.QuotaClassOf(instanceType); !ok || got != want {
			t.Errorf("QuotaClassOf(%s) = %s, want %s", instanceType, got, want)
		}
	}
	for _, instanceType := range []string{"inf2.xlarge", "trn1.2xlarge", "dl1.24xlarge", ""} {
		if got, ok := compute.QuotaClassOf(instanceType); ok {
			t.Errorf("QuotaClassOf(%s) = %s, want none", instanceType, got)
		}
	}
}
