"""Contracts and helpers the SDK shares with the in-container runner.

Modules here import only `lazycloud.contracts` and third-party packages, never
the rest of the SDK, so the runner loads them without SDK workflows.
"""
