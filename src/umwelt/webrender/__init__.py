"""Umwelt Web Render — human-facing 2D pixel-art render gateway (M0).

A separate Flask service (gateway.py) that reads from the existing world
server's HTTP API and adds a presentation layer (grid coordinates,
placeholder colors) the world engine itself has no concept of. Read-only in
M0 — no mutation routes, no auth. The world server (server.py) is the
single source of truth and is never modified by this package.
"""
