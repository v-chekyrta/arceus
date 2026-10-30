<!--
The project's notes for the agent. Everything outside these comments goes, as it is, at the end of the
AGENTS.md the agent and its reviewers read at the start of every task, under "## Project notes".
Comments like this one are left out, so this file as it comes gives the agent nothing.

What belongs here: what only this project knows and every task needs.
- What the product is and who uses it, in two or three sentences.
- The words the team and the tickets use, and what they mean in the code.
- How the repositories fit together: which one serves what, what talks to what.
- Rules the team keeps that the code does not show: "migrations are reversible", "no new dependencies
  without asking on the ticket", "test files are *_spec.py".
- Decisions already made, with the reason, so the agent does not undo them.

What does not belong here:
- Rules of one repository (how to build it, its code style): put them in that repository's AGENTS.md,
  through a merge request.
- How to work on this server (pushing, make verify, English in MRs): those are config/agent-notes.md.
- Secrets, tokens, passwords.
- What one ticket needs: that goes on the ticket.

The server's notes win where these contradict them. At most 8 KB without the comments; a longer file
stops new tasks until it is shortened (arceus-runner check shows the size). A task keeps the notes it
started with to its end, its code review included; a change reaches the next task.
-->
