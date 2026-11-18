# The assistant's ticket desk

This folder is where tickets for the {project} factory are written. Every task here is the same job: read a conversation
between a manager and {name} (the assistant), check what it says against the code, and write the ticket an AI agent
of the factory will work from. That agent never sees the conversation and cannot ask anyone.

- `briefs/<task id>.md` is the conversation the ticket comes from, and the assistant's draft. Read it; do not change it.
- `code/` holds copies of the project's repositories, refreshed before every task. Read them; never change them.
- Write exactly one file: `artifacts/<task id>.md`. Nothing else is written, committed or pushed.
- There is no YouTrack, no internet and no production access here. Say so when something could only be checked there.
