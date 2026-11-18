"""Who the factory takes work from, read from the runner's own config so the assistant and the runner never disagree.

The runner takes a ticket only when its queue tag was put on by someone in `tracker.allowed_users` (by the ticket's
history), in its queued state, and never its own bot's tag. The assistant does not queue tickets: the manager moves
the ticket to To Do and tags it. The assistant reads the same facts to tell the manager what is still missing and,
after that, whether the factory has taken it.

`guide()` is the same config in words, for her prompt: how people start work, answer the agent, review its merge
requests and pick a failed task up again, so what she tells them is what this runner actually does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class FactoryPolicy:
    """What the runner accepts, read from its own config so the two never disagree."""
    allowed_users: tuple[str, ...] = ()
    queue_tag: str = "agent"
    queued_state: str = "To Do"
    bot_user: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)   # the runner's whole config, for guide(); {} without one

    @classmethod
    def load(cls, path: Path | None, fallback_users: list[str] | None = None,
             fallback_state: str = "To Do") -> "FactoryPolicy":
        if path is not None:
            try:
                raw = yaml.safe_load(path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                raw = None
            if raw is not None:
                t = raw.get("tracker") or {}
                return cls(allowed_users=tuple(str(u) for u in t.get("allowed_users") or ()),
                           queue_tag=str((t.get("tags") or {}).get("queue") or "agent"),
                           queued_state=str((t.get("states") or {}).get("queued") or "To Do"),
                           bot_user=str(t.get("bot_user") or ""), source=str(path), raw=raw)
        return cls(allowed_users=tuple(fallback_users or ()), queued_state=fallback_state, source="config")

    def accepts(self, login: str | None) -> bool:
        # The runner never takes its own bot's tag, whatever the list says.
        return bool(login) and login in self.allowed_users and login != self.bot_user

    def guide(self) -> str:
        """How this factory works, in her words for people, from the runner's config. "" without one."""
        if not self.raw:
            return ""
        t = self.raw.get("tracker") or {}
        tags, states = t.get("tags") or {}, t.get("states") or {}
        host = self.raw.get("code_host") or {}
        sched = self.raw.get("schedule") or {}
        tag = lambda k, d: str(tags.get(k) or d)            # noqa: E731
        state = lambda k, d: str(states.get(k) or d)        # noqa: E731
        queue, needs, failed, research = tag("queue", "agent"), tag("needs_input", "agent-needs-input"), \
            tag("failed", "agent-failed"), tag("research", "agent-research")
        progress, review = state("in_progress", "In Progress"), state("code_review", "Code Review")
        merged = states.get("merged", "Testing") if "merged" in states else "Testing"
        who = ", ".join(self.allowed_users) or "нікого (список порожній)"
        gl_bot = str(host.get("bot_user") or "")
        group = f"{str(host.get('url') or '').rstrip('/')}/{host.get('group')}" if host.get("group") else ""
        lines = [
            "Фабрика — AI-агенти розробки: беруть тікети проєкту " + str(t.get("project") or "") + " з YouTrack, пишуть "
            "код у репозиторіях " + (f"групи {group}" if group else "проєкту") + " і відкривають merge request (MR). "
            "Нижче — як вона працює насправді (з її налаштувань). Коли людина питає, як щось зробити з фабрикою, "
            "поясни точно цими кроками, з назвами станів і тегів: тут технічні слова доречні.",
            f"- Запустити тікет: стан «{self.queued_state}» і тег «{queue}». Тег мусить поставити хтось із тих, кому "
            f"фабрика довіряє (логіни YouTrack): {who}. Тег від когось іншого фабрика не бере і пише про це коментар. "
            f"Акаунт бота {self.bot_user or 'фабрики'} запустити тікет не може, і ти тікетів у роботу не ставиш.",
            f"- Дослідження без коду: тег «{research}» поруч із «{queue}». Відповідь агент напише коментарем у тікеті, "
            f"MR не буде; продовження — коментар «/continue» з новим питанням.",
        ]
        plats = t.get("platforms") or {}
        if plats:
            pairs = "; ".join(f"тег «{k}» — {', '.join(v)}" for k, v in plats.items())
            lines.append(f"- Платформи: {pairs}. Тікет з таким тегом не піде на рев'ю, поки в тому репозиторії немає "
                         "змін; тег ставлять, коли зміна справді потрібна на цій платформі.")
        lines += [
            f"- Коли взяла: тікет у «{progress}», виконавець — бот, і в його першому коментарі розділ «Progress»: що "
            "зараз робиться, кроки, посилання на MR. Він оновлюється сам.",
            f"- Агент питає: тег «{needs}» і питання коментарем зі згадкою того, хто поставив тег. Відповідь — будь-який "
            "коментар у тікеті від людини зі списку вище, після питання. Без відповіді нагадає, потім спроба "
            "завершиться невдачею.",
            f"- Готово: MR відкриваються лише там, де агент щось змінив; посилання — у тікеті, стан «{review}».",
            "- Рев'ю в GitLab: коментар у MR, який має опрацювати агент, треба адресувати боту фабрики"
            + (f" — @{gl_bot}" if gl_bot else "") + " — у кожному треді, де чекають зміни (у самому зауваженні або "
            "відповіддю в треді). Коментарі без згадки агент сам по собі не бере: люди там говорять між собою. "
            "Публікувати одразу (Add comment now) або завершити рев'ю кнопкою Submit review: чернеток рев'ю фабрика "
            "не бачить. Інший шлях — коментар у тікеті від людини зі списку: тоді агент отримує його разом з усіма "
            "нерозв'язаними тредами MR (можна написати «див. коментарі в MR»). Агент збирає зауваження, поки "
            f"{sched.get('review_debounce') or '5 хвилин'} ніхто нічого не пише, бере їх разом, відповідає в "
            "кожному треді й комітить у ту саму гілку.",
            f"- Не вдалося: тег «{failed}» і коментар з причиною. Продовжити — коментар, що починається з "
            "«/continue», від людини зі списку (протягом 14 днів), можна з поясненням, що робити далі; гілки й коміти "
            "зберігаються. Те саме, якщо всі MR закрили без злиття.",
            f"- Злиття: коли всі MR задачі злиті, тікет іде в «{merged}». Якщо людина вручну перевела тікет далі "
            f"«{review}», фабрика його відпускає.",
            f"- Як швидко: у робочі години фабрика дивиться в YouTrack кожні {sched.get('poll_working') or '2 хв'}, "
            f"поза ними — кожні {sched.get('poll_off_hours') or '10 хв'}; береться за одну задачу за раз, правки "
            "відкритих MR ідуть перед новими тікетами.",
        ]
        return "\n".join(lines)
