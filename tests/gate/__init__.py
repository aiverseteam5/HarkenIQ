"""TEST-ONLY infrastructure for the compose exit gate. Never shipped.

Nothing under `services/*/src` or `src/` imports from here, and no image
copies it; the compose gate mounts it through its own override (A30.38).
"""
