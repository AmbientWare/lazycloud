//go:build !linux

package main

func (s *supervisor) reapAdoptedChildren() {}
