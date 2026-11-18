"""The ticket's own tags, and the platforms they name.

A ticket often says where the work goes only in its tags: `android` and `ios` on a ticket whose text says
"As a user" and nothing more, which can come back as a web change. The tags are part
of what the ticket asks, so the agent and every reviewer read them with the text. When the project maps a
tag to its repositories (`tracker.platforms`), the work is not published until each tagged platform has
changes in one of them.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from arceus_runner.config import TrackerTags


def runner_tags(tags: TrackerTags) -> set[str]:
    """The tags the runner itself reads or sets: they say nothing of the work. Every field of TrackerTags, so a tag
    the runner gains later is left out too."""
    return {str(v).lower() for v in tags.model_dump().values() if v}


def ticket_tags(tags: Iterable[str], service: TrackerTags) -> list[str]:
    """The ticket's tags without the runner's own, in a stable order."""
    own = runner_tags(service)
    return sorted({t.strip() for t in tags if t and t.strip() and t.strip().lower() not in own}, key=str.lower)


def tagged_platforms(tags: Iterable[str], platforms: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """The platforms the tags name, tag (as the config spells it) -> its repositories."""
    have = {t.lower() for t in tags}
    return {name: repos for name, repos in platforms.items() if name.lower() in have}


def section(tags: list[str], platforms: Mapping[str, list[str]], present: Iterable[str],
            reviewer: bool = False) -> str:
    """The part of the brief that gives the ticket's tags, and what the platform ones ask. Empty without tags.
    `reviewer`: for whoever checks the work rather than does it."""
    if not tags:
        return ""
    lines = ["## Tags on the ticket", "", ", ".join(f"`{t}`" for t in tags), ""]
    named = tagged_platforms(tags, platforms)
    have = set(present)
    if named and reviewer:
        lines.append("These tags name the platforms the work is for, in these repositories:")
        for name, repos in named.items():
            lines.append(f"- `{name}`: " + ", ".join(f"`{r}`" for r in repos))
        lines += ["",
                  "A tagged platform is a requirement of the ticket: it is met only by a change in that platform's "
                  "repository. Work in another repository (a backend, the web app) does not meet it, and one "
                  "platform done leaves the other unmet. Report a tagged platform without its change as a blocker."]
    elif reviewer:
        lines.append("Tags often say where the work goes (a platform, an app, a part of the product). When a tag "
                     "names one and the change is somewhere else, the ticket's requirements are not met.")
    elif named:
        lines.append("These tags name the platforms the work is for. Do the work there, in these repositories:")
        for name, repos in named.items():
            where = ", ".join(f"`{r}`" + ("" if r in have else " (not in your workspace)") for r in repos)
            lines.append(f"- `{name}`: {where}")
        lines += ["",
                  "Each tagged platform needs its own change: work in another repository (a backend, the web app) "
                  "does not count for it, and one platform done leaves the other undone. Code that already does "
                  "something similar elsewhere is a reference, not the place for this ticket's work. If a tagged "
                  "platform truly needs no change, or its repository is not in your workspace, do not decide that "
                  "yourself: ask (needs_input) and say why. The runner does not publish the work while a tagged "
                  "platform has no change."]
    else:
        lines.append("Tags often say where the work goes (a platform, an app, a part of the product) or what kind of "
                     "work it is. Read them with the text: when a tag names a platform or a part of the product, the "
                     "work is there, not in whatever code happens to look closest. When the tags and the text "
                     "disagree, ask (needs_input) instead of choosing.")
    return "\n".join(lines)


def missing(tags: Iterable[str], platforms: Mapping[str, list[str]], changed: Iterable[str]) -> dict[str, list[str]]:
    """The tagged platforms none of whose repositories changed: tag -> its repositories."""
    done = set(changed)
    return {name: repos for name, repos in tagged_platforms(tags, platforms).items()
            if not any(r in done for r in repos)}


def describe_missing(gaps: Mapping[str, list[str]]) -> str:
    return "; ".join(f"`{name}` ({', '.join(f'`{r}`' for r in repos)})" for name, repos in gaps.items())
