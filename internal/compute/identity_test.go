package compute_test

import (
	"errors"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

const enrollingInstance = "i-0abc1234def567890"

// instanceProof is the presigned GetCallerIdentity request the agent sends,
// signed by accessKey over the given headers.
func instanceProof(host compute.HostID, accessKey, signedHeaders string) compute.IdentityProof {
	q := url.Values{
		"Action": {"GetCallerIdentity"}, "Version": {"2011-06-15"},
		"X-Amz-Algorithm":     {"AWS4-HMAC-SHA256"},
		"X-Amz-Credential":    {accessKey + "/20260930/us-east-2/sts/aws4_request"},
		"X-Amz-Date":          {"20260930T120000Z"},
		"X-Amz-Expires":       {"60"},
		"X-Amz-SignedHeaders": {signedHeaders},
		"X-Amz-Signature":     {strings.Repeat("ab", 32)},
	}
	return compute.IdentityProof{
		URL: "https://sts.us-east-2.amazonaws.com/?" + q.Encode(), Method: http.MethodGet,
		Headers: map[string]string{compute.HostIDHeader: host.String()},
	}
}

func TestCloudHostEnrollsOnceWithItsInstanceIdentity(t *testing.T) {
	ctx := t.Context()
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	host := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseProvisioning, Region: "us-east-2",
		InstanceID: enrollingInstance, Unenrolled: true})
	other := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseProvisioning, Region: "us-east-2",
		InstanceID: "i-0fff0000aaaa11112", Unenrolled: true})
	// STS names the signer of each instance-profile credential.
	signers := map[string][2]string{
		"ASIAINSTANCE": {"111122223333", "arn:aws:sts::111122223333:assumed-role/lazycloud-node/" + enrollingInstance},
		"ASIAROLE":     {"111122223333", "arn:aws:sts::111122223333:assumed-role/lazycloud-builder/" + enrollingInstance},
		"ASIAOTHER":    {"111122223333", "arn:aws:sts::111122223333:assumed-role/lazycloud-node/i-0fff0000aaaa11112"},
		"ASIAACCOUNT":  {"444455556666", "arn:aws:sts::444455556666:assumed-role/lazycloud-node/" + enrollingInstance},
	}
	emulator.on("GetCallerIdentity", func(call awsCall) awsReply {
		key, _, _ := strings.Cut(call.Form.Get("X-Amz-Credential"), "/")
		if call.Host != "sts.us-east-2.amazonaws.com" || call.Header.Get(compute.HostIDHeader) == "" {
			return queryError(http.StatusForbidden, "SignatureDoesNotMatch", "The request signature we calculated does not match")
		}
		signer, ok := signers[key]
		if !ok {
			return queryError(http.StatusForbidden, "InvalidClientTokenId", "The security token included in the request is invalid")
		}
		session := signer[1][strings.LastIndex(signer[1], "/")+1:]
		return callerIdentityReply(signer[0], signer[1], "AROA3XFRBF535EXAMPLE:"+session)
	})
	report := compute.HostReport{Hostname: "ip-10-0-1-5", Capacity: compute.Capacity{CPUMillis: 1500, MemoryBytes: 6 * gib}}
	signed := "host;lazycloud-host-id"

	refuse := func(name string, proof compute.IdentityProof, sent bool) {
		t.Helper()
		before := len(emulator.calls("GetCallerIdentity"))
		var refused *compute.IdentityError
		if _, _, err := o.compute.EnrollCloud(ctx, host, proof, report); !errors.As(err, &refused) {
			t.Errorf("%s: %v, want IdentityError", name, err)
		}
		if after := len(emulator.calls("GetCallerIdentity")); (after > before) != sent {
			t.Errorf("%s: sent to STS %v, want %v", name, after > before, sent)
		}
	}
	refuse("a proof naming another host", instanceProof(other, "ASIAINSTANCE", signed), false)
	refuse("a proof that does not sign the host header", instanceProof(host, "ASIAINSTANCE", "host"), false)
	evil := instanceProof(host, "ASIAINSTANCE", signed)
	evil.URL = strings.Replace(evil.URL, "sts.us-east-2.amazonaws.com", "sts.us-east-2.amazonaws.com.evil.test", 1)
	refuse("a proof for another server", evil, false)
	plain := instanceProof(host, "ASIAINSTANCE", signed)
	plain.URL = strings.Replace(plain.URL, "https://", "http://", 1)
	refuse("a proof over plain HTTP", plain, false)
	refuse("a session of another role", instanceProof(host, "ASIAROLE", signed), true)
	refuse("a session of another instance", instanceProof(host, "ASIAOTHER", signed), true)
	refuse("a session in another account", instanceProof(host, "ASIAACCOUNT", signed), true)
	refuse("a signature STS rejects", instanceProof(host, "ASIAFORGED", signed), true)

	id, token, err := o.compute.EnrollCloud(ctx, host, instanceProof(host, "ASIAINSTANCE", signed), report)
	if err != nil || id != host {
		t.Fatalf("enroll with the instance's proof: %v %v", id, err)
	}
	if got, err := o.compute.AuthenticateHost(ctx, token); err != nil || got != host {
		t.Fatalf("host token: %v %v", got, err)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseJoining) {
		t.Fatalf("enrolled host is %s, want joining", phase)
	}
	refuse("a second enrollment of the host", instanceProof(host, "ASIAINSTANCE", signed), true)
	if got, err := o.compute.AuthenticateHost(ctx, token); err != nil || got != host {
		t.Fatalf("the first token after a refused second enrollment: %v %v", got, err)
	}
}
