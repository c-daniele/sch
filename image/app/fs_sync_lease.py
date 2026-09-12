"""In-memory exclusive lease for one workspace fs-sync writer."""

from __future__ import annotations


class FsSyncLeaseRegistry:
    """Caller serializes access; this class keeps lease rules testable."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def acquire(self, workspace: str, tunnel_id: str) -> bool:
        owner = self._owners.get(workspace)
        if owner is not None and owner != tunnel_id:
            return False
        self._owners[workspace] = tunnel_id
        return True

    def release(self, workspace: str, tunnel_id: str) -> None:
        if self._owners.get(workspace) == tunnel_id:
            self._owners.pop(workspace, None)

    def owner(self, workspace: str) -> str | None:
        return self._owners.get(workspace)
