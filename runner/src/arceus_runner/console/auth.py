"""Who may do what. Every active account may look; each action needs one of the roles below. An admin may do
everything. The server checks this on every request: a button the page does not show is not the check."""

from __future__ import annotations

from arceus_runner.console.store import ROLES, User

ROLE_WORDS = {
    "qa": ("QA", "Starts and stops test runs of the platform and edits their instructions."),
    "architect": ("Architect", "Decides on architecture findings (accepts the risk, marks a false positive, takes a "
                               "decision back), starts and stops reviews and edits their instructions."),
    "operator": ("Operator", "Sees the settings, restarts the runner and the engine, runs the setup check."),
    "admin": ("Admin", "Everything: edits the settings and the project's notes, runs setup, approves people and "
                       "gives them roles."),
}
EVERYONE = "Everyone approved sees the status, the reports, the findings and the monitoring."

# action -> the roles that allow it (admin allows everything)
ACTIONS: dict[str, frozenset[str]] = {
    "qa.run": frozenset({"qa"}),
    "qa.instructions": frozenset({"qa"}),
    "arch.decide": frozenset({"architect"}),
    "arch.run": frozenset({"architect"}),
    "arch.instructions": frozenset({"architect"}),
    "settings.view": frozenset({"operator"}),
    "settings.edit": frozenset(),
    "notes.edit": frozenset(),
    "ops.view": frozenset({"operator"}),
    "ops.restart": frozenset({"operator"}),
    "ops.check": frozenset({"operator"}),
    "ops.setup": frozenset(),
    "users.manage": frozenset(),
}
assert set(ROLE_WORDS) == set(ROLES)


def can(user: User | None, action: str) -> bool:
    if user is None or not user.active:
        return False
    if action == "view":
        return True
    allowed = ACTIONS[action]  # an unknown action is a bug, never a quiet yes
    return user.has("admin") or bool(allowed & user.roles)


def grafana_role(user: User) -> str:
    """Grafana's own role for this person: Admin for admins, Viewer for everyone else."""
    return "Admin" if user.has("admin") else "Viewer"
