"""`arceus-runner assistant import`: an assistant set up before it was part of Arceus (~/assistant/config.yaml with its
tokens in ~/assistant/secrets/) becomes the `assistant:` section of the project's config.yaml and assistant.env.

- Nothing of the conversations moves: `home` is the folder the old data_dir, uploads and mirror are in, so the same
  database, files and code mirror are used as they are.
- config.yaml gets the section appended at its end; every other line of it stays byte for byte. A file that already
  has an `assistant:` section is left alone. The result is checked by the runner's own parse_config before it is
  written, atomically, with the file's mode kept.
- The tokens go into assistant.env (600). A name it already has is kept, never overwritten.
- The persona, when it was a file inside the old code folder (which goes away), is copied into `home`.
- Without --apply it only says what it would do.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from arceus_runner.config import ConfigError, parse_config, read_env_file

DEFAULT_SECRETS = Path("~/arceus/data/assistant.env").expanduser()
# The old keys this command knows. Anything else in the old file is refused by name rather than dropped unseen.
KNOWN = {"project", "persona_name", "persona_file", "persona_gender", "persona_intro", "knowledge_file", "data_dir",
         "uploads_dir", "allowed_users", "brain", "transcribe", "youtrack", "factory", "ticket_engine", "slack",
         "ticket_creators", "expert"}
SLACK_KEYS = ("web_url", "read_channels", "internal_channels", "listen_channels", "partner_channels",
              "creator_channels", "creator_domain", "history_days", "call_names")
YOUTRACK_KEYS = ("project", "board", "queue_tag", "state_field", "queued_state", "type_field", "types")


class NotImportable(Exception):
    """The old setup cannot be carried over as it is; the words say what to change first."""


@dataclass
class Plan:
    section: dict
    secrets: dict[str, str] = field(default_factory=dict)   # name -> value (never printed)
    copies: list[tuple[Path, Path]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _path(v: object) -> Path:
    return Path(os.path.expanduser(str(v)))


def _home_str(p: Path) -> str:
    """A path as config.yaml writes it: under the run user's home as ~/…, so the file reads the same on any machine."""
    home = Path.home()
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return str(p)


def _duration(seconds: object) -> str:
    n = int(seconds)
    return f"{n // 60}m" if n % 60 == 0 else f"{n}s"


def _token(path: Path, name: str) -> str:
    try:
        text = path.read_text().strip()
    except OSError as e:
        raise NotImportable(f"{path}: cannot be read ({e.strerror})") from None
    if text.startswith(f"{name}="):
        text = text.split("=", 1)[1].strip().strip("\"'")
    if not text or "\n" in text:
        raise NotImportable(f"{path}: expected one token on one line")
    return text


def plan(old_config: Path, tracker_url: str, config_path: Path, port: int = 8190, web: bool = True) -> Plan:
    raw = yaml.safe_load(old_config.read_text()) or {}
    if not isinstance(raw, dict):
        raise NotImportable(f"{old_config}: not a mapping")
    unknown = sorted(set(raw) - KNOWN)
    if unknown:
        raise NotImportable(f"{old_config}: keys this import does not know: {', '.join(unknown)}")
    data_dir = _path(raw["data_dir"])
    home = data_dir.parent
    if data_dir.name != "data":
        raise NotImportable(f"data_dir {data_dir}: the assistant keeps its database in <home>/data; move it there first")
    if raw.get("uploads_dir") and _path(raw["uploads_dir"]) != home / "uploads":
        raise NotImportable(f"uploads_dir {raw['uploads_dir']}: expected {home / 'uploads'}")
    brain = raw.get("brain") or {}
    if brain.get("workdir") and _path(brain["workdir"]) != home / "mirror":
        raise NotImportable(f"brain.workdir {brain['workdir']}: expected the mirror at {home / 'mirror'}")
    if brain.get("mcp_config") or brain.get("extra_tools"):
        raise NotImportable("brain.mcp_config / brain.extra_tools: not carried over (the assistant's tools are its own)")

    out = Plan(section={"enabled": True})
    s = out.section
    if raw.get("project"):
        s["project"] = str(raw["project"])
    s["name"] = str(raw.get("persona_name") or "Асистент")
    s["gender"] = "m" if str(raw.get("persona_gender") or "f").lower().startswith("m") else "f"
    if raw.get("persona_intro"):
        s["intro"] = " ".join(str(raw["persona_intro"]).split())
    app = home / "app"
    for key, target in (("persona_file", "persona_file"), ("knowledge_file", "knowledge_file")):
        if not raw.get(key):
            continue
        src = _path(raw[key])
        if src.is_relative_to(app):     # the old code folder: it goes away with the old service
            dst = home / ("persona.md" if key == "persona_file" else src.name)
            out.copies.append((src, dst))
            s[target] = _home_str(dst)
        else:
            s[target] = _home_str(src)
    s["home"] = _home_str(home)
    if config_path.parent / "assistant.env" != DEFAULT_SECRETS:
        s["secrets"] = _home_str(config_path.parent / "assistant.env")
    s["port"] = port
    s["web"] = web
    for key in ("allowed_users", "ticket_creators"):
        if raw.get(key):
            s[key] = [str(u).lower() for u in raw[key]]
    if raw.get("expert"):
        s["expert"] = str(raw["expert"]).lower()
        out.notes.append(f"expert_name is how she names {s['expert']} to people («покликала …», the accusative): "
                         "set it in the section (e.g. «Андрія Мельника»); until then she says «експерта проєкту»")
    if brain.get("model"):
        s["model"] = str(brain["model"])
    if brain.get("effort"):
        s["effort"] = str(brain["effort"])
    if brain.get("timeout_s"):
        s["reply_timeout"] = _duration(brain["timeout_s"])
    if brain.get("claude_bin") and brain["claude_bin"] != "claude":
        s["claude_bin"] = str(brain["claude_bin"])
    if brain.get("prompt_file") or brain.get("settings_file"):
        out.notes.append("brain.prompt_file and brain.settings_file are not carried over: the prompt and the "
                         "session's permissions now come with the assistant's code")
    if "transcribe" in raw:
        s["voice_model"] = str((raw.get("transcribe") or {}).get("model", "medium") or "")

    fac = raw.get("factory") or {}
    if fac.get("config") and _path(fac["config"]).resolve() != config_path.resolve():
        out.notes.append(f"factory.config was {fac['config']}: the assistant now reads the factory from "
                         f"{config_path} itself")
    if fac.get("allowed_users"):
        out.notes.append("factory.allowed_users is not carried over: the factory's own tracker.allowed_users is read")

    yt = raw.get("youtrack") or {}
    if yt.get("url") or yt.get("project"):
        if str(yt.get("url", "")).rstrip("/") != tracker_url.rstrip("/"):
            raise NotImportable(f"youtrack.url {yt.get('url')} is not this factory's tracker ({tracker_url}): the "
                               "assistant makes tickets where the factory takes them")
        y = {k: yt[k] for k in YOUTRACK_KEYS if k in yt}
        y["types"] = {str(k): str(v) for k, v in (yt.get("types") or {}).items()} or None
        s["youtrack"] = {k: (str(v) if k != "types" else v) for k, v in y.items() if v is not None}
        if yt.get("token_file"):
            out.secrets["YOUTRACK_TOKEN"] = _token(_path(yt["token_file"]), "YOUTRACK_TOKEN")

    te = raw.get("ticket_engine") or {}
    if te.get("bridge"):
        bridge = te["bridge"] if isinstance(te["bridge"], str) else " ".join(map(str, te["bridge"]))
        s["ticket_engine"] = {"bridge": bridge, "timeout": _duration(te.get("timeout_s", 1500))}

    sl = raw.get("slack")
    if sl:
        s["slack"] = {k: sl[k] for k in SLACK_KEYS if sl.get(k) not in (None, "", [])}
        out.secrets["SLACK_APP_TOKEN"] = _token(_path(sl["app_token_file"]), "SLACK_APP_TOKEN")
        out.secrets["SLACK_BOT_TOKEN"] = _token(_path(sl["bot_token_file"]), "SLACK_BOT_TOKEN")

    return out


def section_text(section: dict, source: Path) -> str:
    body = yaml.safe_dump({"assistant": section}, allow_unicode=True, sort_keys=False, width=120,
                          default_flow_style=None)
    return (f"\n# The project's assistant (docs/assistant.md), imported from {_home_str(source)} by "
            f"`arceus-runner assistant import`. Its tokens are in assistant.env.\n" + body)


def _write_atomic(path: Path, data: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def run(config_path: Path, old_config: Path, port: int, web: bool, apply: bool, say=print) -> int:
    text = config_path.read_text(encoding="utf-8")
    current = yaml.safe_load(text) or {}
    if "assistant" in current:
        say(f"{config_path} already has an assistant: section; nothing is imported over it.")
        return 1
    tracker_url = str((current.get("tracker") or {}).get("url", ""))
    try:
        p = plan(old_config, tracker_url, config_path, port, web)
    except (NotImportable, KeyError, OSError, yaml.YAMLError) as e:
        say(f"cannot import {old_config}: {e}")
        return 1
    new_text = text + ("" if text.endswith("\n") else "\n") + section_text(p.section, old_config)
    try:
        parse_config(new_text, config_path)
    except ConfigError as e:
        say("the imported section would not pass the runner's check:")
        for line in e.problems:
            say(f"  {line}")
        return 1
    env_path = config_path.parent / "assistant.env"
    existing = read_env_file(env_path) if env_path.exists() else {}
    kept = [n for n in p.secrets if existing.get(n)]
    added = {n: v for n, v in p.secrets.items() if not existing.get(n)}
    say(f"config.yaml: appends to {config_path}:")
    say(section_text(p.section, old_config).rstrip())
    say(f"{env_path}: adds {', '.join(added) or 'nothing'}" + (f"; keeps its own {', '.join(kept)}" if kept else ""))
    for src, dst in p.copies:
        say(f"copies {src} to {dst}")
    for note in p.notes:
        say(f"note: {note}")
    if not apply:
        say("Nothing written: run again with --apply.")
        return 0
    for src, dst in p.copies:
        if not dst.exists():
            shutil.copy2(src, dst)
    if added:
        lines = [f"{n}={v}" for n, v in added.items()]
        old = env_path.read_text(encoding="utf-8") if env_path.exists() else \
            "# The assistant's tokens (docs/assistant.md). Never in config.yaml; set from the console's Assistant page.\n"
        _write_atomic(env_path, old + ("" if old.endswith("\n") else "\n") + "\n".join(lines) + "\n", 0o600)
    _write_atomic(config_path, new_text, config_path.stat().st_mode & 0o777)
    say("Imported. Stop and disable the old service before the new one starts: two connections to one Slack app "
        "split its events between them.")
    return 0
