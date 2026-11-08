<!--
The team's instructions for architecture reviews (architect: in config.yaml). Everything outside these comments goes,
as it is, to the agent at the end of every review, after the runner's own criteria and rules. Comments like this one
are left out, so this file as it comes is empty, and that is fine: a review then works by the runner's criteria
alone (how features fit together, code written to look busy, tests that prove little, modules worth deepening,
drift from the team's written rules). `arceus-runner check` shows which of the two a run will get.

Write here what only your team knows and the code does not show:
- What to look at harder, and what to leave alone: "billing and permissions matter most", "the admin screens are
  being rewritten, skip them".
- What the team counts as a problem and what it does not: "integration tests with a real database are the norm
  here, a mock of the database is a finding", "duplication between the two mobile clients is on purpose".
- Decisions already made, so the agent does not argue with them. Rules every task needs belong in the project's
  notes (project-notes.md) instead: the review reads those too.
- Code the team builds on but does not own goes in config.yaml (`architect.not_ours`), not here: the runner then
  tells the agent never to propose changing it, and to raise a change of the team's inside it that could have lived
  outside, in an extension point, a setting or an override. Here you can say how the team extends it.

Write it in the language you want the report in; with nothing written, the report is in bulava.report_language.
At most 64 KB without the comments. A run reads this file when it starts and keeps the text with its report, so
editing it never changes a review under way.

An example to start from: copy it out of this comment and change it.

# Where to look

- Billing and permissions first: a mistake there costs money or leaks data.
- The reporting service is being replaced this quarter: report only what breaks other parts.

# What we count as a problem

- Business rules belong in the services; a view or a serializer that decides prices is a finding.
- Our tests run against a real database. A test that mocks the database is a finding, a test that mocks an outside
  payment provider is not.

# How we extend the platform we build on

- Customizations go into our plugins and theme, never into the platform's own code. A change in the platform's code
  needs a comment that links the ticket explaining why there was no other way.
-->
