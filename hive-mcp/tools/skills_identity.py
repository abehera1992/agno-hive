"""Identity-provider abstraction for the persistent skills registry (Phase 1.57a).

Real authentication does not exist yet anywhere in hive-mcp. This module is the
ONE seam every skills_registry.py caller resolves identity through, so that when
real auth eventually arrives, it replaces THIS module's internals only — the
registry and its ownership model never need to change, and callers never need to
change either (they still just call resolve_identity()).

Two modes, selected by HIVE_SKILLS_IDENTITY_MODE (hive-mcp/config.py):

  development  -- tenant_id/user_id come EXCLUSIVELY from the server-side
                  HIVE_SKILLS_DEV_TENANT_ID/HIVE_SKILLS_DEV_USER_ID config values.
                  Any trusted_tenant_id/trusted_user_id a caller passes in is
                  ignored outright (not merged, not preferred, not logged as a
                  mismatch and then used) — development mode has exactly one
                  source of identity, period.

  authenticated -- tenant_id/user_id MUST come from the caller's own trusted
                  application request context, passed as this function's
                  trusted_tenant_id/trusted_user_id arguments. Today nothing in
                  hive-mcp actually populates those from a real authenticated
                  request (there is no request/session object reaching an MCP
                  tool call) — that wiring is explicitly out of scope for this
                  phase (see CLAUDE.md Phase 1.57a: "no request forwarding").
                  What this phase guarantees is the CONTRACT: authenticated mode
                  requires both IDs to be non-empty strings supplied through this
                  parameter, and raises IdentityResolutionError otherwise. It
                  never reads a raw client payload itself, and it never falls
                  back to development's config values.

A caller MUST NOT have a separate "client-supplied tenant/user id" parameter that
bypasses this function — resolve_identity() is the only place tenant_id/user_id
are ever decided, so a malicious or buggy client payload has no path to override
the active identity (see each mode's validation harness in Phase 1.57a's scratch
tests for a concrete demonstration).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

import structlog

log = structlog.get_logger()

DEVELOPMENT = "development"
AUTHENTICATED = "authenticated"
_VALID_MODES = (DEVELOPMENT, AUTHENTICATED)


class IdentityResolutionError(Exception):
    """Raised when the active identity mode's requirements aren't met -- an
    unrecognized HIVE_SKILLS_IDENTITY_MODE value, or authenticated mode missing
    one or both trusted IDs. Never raised for "no identity" in development mode
    with no dev IDs configured -- that is a valid state (platform-only access),
    not an error; see resolve_identity()'s own docstring."""


@dataclass(frozen=True)
class Identity:
    tenant_id: str | None
    user_id: str | None
    mode: str


def resolve_identity(
    *, trusted_tenant_id: str | None = None, trusted_user_id: str | None = None
) -> Identity:
    """Resolve the active (tenant_id, user_id) pair for one skills-registry call.

    trusted_tenant_id/trusted_user_id must ALREADY be values the calling
    application layer trusts (e.g. verified from a real auth/session context in
    a future phase) — never a raw, unverified field lifted straight out of a
    client request body. In "development" mode these two arguments are not
    read at all; passing them is harmless but has zero effect, by design (see
    module docstring).

    Returns an Identity with tenant_id=user_id=None when development mode has
    no dev identity configured — a legitimate state meaning "platform skills
    only, no third-party identity active," not a failure.

    Raises IdentityResolutionError for:
      - an unrecognized HIVE_SKILLS_IDENTITY_MODE value (no silent default)
      - authenticated mode with either trusted id missing/blank (no silent
        fall-through to development mode's config values)
    """
    mode = config.HIVE_SKILLS_IDENTITY_MODE
    if mode not in _VALID_MODES:
        raise IdentityResolutionError(
            f"HIVE_SKILLS_IDENTITY_MODE={mode!r} is not one of {_VALID_MODES!r} — "
            "refusing to guess; fix the server-side config."
        )

    if mode == DEVELOPMENT:
        tenant_id = config.HIVE_SKILLS_DEV_TENANT_ID
        user_id = config.HIVE_SKILLS_DEV_USER_ID
        if (tenant_id is None) != (user_id is None):
            # Half-configured dev identity is an operator mistake, not a valid
            # partial state -- fail loud rather than silently treating it as
            # "platform only" or as a usable identity with a missing half.
            raise IdentityResolutionError(
                "development mode requires BOTH HIVE_SKILLS_DEV_TENANT_ID and "
                "HIVE_SKILLS_DEV_USER_ID set, or BOTH unset — got exactly one."
            )
        identity = Identity(tenant_id=tenant_id, user_id=user_id, mode=mode)
    else:  # authenticated
        if not trusted_tenant_id or not trusted_user_id:
            raise IdentityResolutionError(
                "authenticated mode requires both trusted_tenant_id and "
                "trusted_user_id from the caller's own trusted request context "
                "— missing identity is rejected, never defaulted to development."
            )
        identity = Identity(tenant_id=trusted_tenant_id, user_id=trusted_user_id, mode=mode)

    log.info(
        "skills.identity.resolved",
        mode=identity.mode,
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
    )
    return identity
