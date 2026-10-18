package compute

import "context"

// EnsurePlatformNetwork runs ensureNetwork with the platform's own credentials.
func (c *Compute) EnsurePlatformNetwork(ctx context.Context, region string) (Network, error) {
	return c.ensureNetwork(ctx, awsScope{key: string(KindPlatform)}, region)
}

// DeletePlatformNetwork runs deleteNetwork with the platform's own credentials.
func (c *Compute) DeletePlatformNetwork(ctx context.Context, region string) (bool, error) {
	return c.deleteNetwork(ctx, awsScope{key: string(KindPlatform)}, region)
}
