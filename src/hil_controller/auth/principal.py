"""Principal dataclass: the normalised identity produced by any auth path."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Principal:
    kind: str  # 'static' | 'db-token' | 'oidc' (future)
    subject: str  # token id or OIDC sub
    repo: str  # token's pinned repo or OIDC repository claim
    allowed_pools: list[str]
    allowed_profiles: list[str]
    default_profile: str
    capabilities: list[str]

    def allows_pool(self, pool: str) -> bool:
        return "*" in self.allowed_pools or pool in self.allowed_pools

    def allows_profile(self, profile: str) -> bool:
        return "*" in self.allowed_profiles or profile in self.allowed_profiles

    def has_capability(self, cap: str) -> bool:
        return "*" in self.capabilities or cap in self.capabilities
