// Package platformimages names the images agents run on their own, outside
// any workload: the image builder and the volume mount image. Agents name
// them at session open, and the server converts them, as it does the
// managed Python images, but no other image an agent names. It has no
// database access, so host runtime code may import it.
package platformimages

import "slices"

const (
	// Builder runs image builds: BuildKit rootless, with buildkitd and
	// buildctl in one container that exits when its build ends.
	Builder = "docker.io/moby/buildkit:v0.33.1-rootless@sha256:f8a833b2de9d68e27f0815e4a737abdfaf8a2e4c615650557df11025101557b4"
	// Mount runs volume mount containers, network holders and network
	// policy helpers; GeeseFS needs only sh, cat, mkdir and umount beside
	// it.
	Mount = "docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

// All is every platform image.
func All() []string { return []string{Builder, Mount} }

// Known reports whether reference is a platform image.
func Known(reference string) bool { return slices.Contains(All(), reference) }
