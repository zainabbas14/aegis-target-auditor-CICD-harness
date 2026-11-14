"""Test principals. Keys are test-only values; production refuses short or placeholder keys."""

from __future__ import annotations

KEYS = {
    "clerk.bea": ("clerk", "test-key-clerk-bea-0001"),
    "clerk.carl": ("clerk", "test-key-clerk-carl-0002"),
    "ana": ("analyst", "test-key-analyst-ana-0003"),
    "ap.manager": ("approver", "test-key-approver-mgr-0004"),
    "ian": ("auditor", "test-key-auditor-ian-0005"),
    "multi": ("clerk+analyst+approver", "test-key-multi-role-0006"),  # for four-eyes tests
    "admin": ("clerk+analyst+approver+auditor", "test-key-all-roles-0007"),
}
API_KEYS = ",".join(f"{pid}:{roles}:{key}" for pid, (roles, key) in KEYS.items())


def auth(principal: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[principal][1]}"}
