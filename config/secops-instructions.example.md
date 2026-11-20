<!--
The team's notes on its stack for advisory triage (secops: in config.yaml). Everything outside these comments
goes, as it is, to the agent at the end of every triage run, after the runner's own rules for the verdicts, and to
the closing review that checks the agent's report, which reads it as the team's own rules for judging its stack
(from a copy beside the advisories; if that copy is changed during the run, none of its verdicts are sent).
Comments like this one are left out, so this file as it comes is empty, and that is fine: a triage run then works
from the code alone.
`arceus-runner check` shows which of the two a run will get.

Write here what the repositories do not show and a verdict depends on:
- What is deployed where, and from which repository: "the api image runs on Kubernetes, built from its Dockerfile;
  the worker runs on a VM we keep by hand".
- Which repositories are legacy, archived in practice, or only examples, so a verdict on them is read that way.
- Versions decided outside the code: "production pins the base image by digest in the infra repository", "the
  database is a managed service, its version is in the cloud console, not here".
- What counts as carrying a component: "a test-only dependency is not shipped". A rule like this is what lets a
  repository whose code has the component go without a verdict: the report names it with the rule, and the
  closing review accepts no such rule that is not written here.

Never put a secret here. Write it in the language you want the report and the evidence in; with nothing written,
they are in bulava.report_language. At most 64 KB without the comments. A run reads this file when it starts and
keeps the text with its report, so editing it never changes a triage run under way.

An example to start from: copy it out of this comment and change it.

# Where things run

- `api` and `web` ship as container images built from their Dockerfiles and run on the cluster.
- `legacy-portal` is frozen and serves nobody outside the office network.

# Versions decided outside the code

- The base images are pinned by digest in `infra/images.yaml`.
- PostgreSQL is a managed service: its version is not in any repository, so a verdict on it needs review.
-->
