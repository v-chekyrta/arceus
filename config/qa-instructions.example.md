<!--
The instructions for test runs of the platform (qa: in config.yaml). Everything outside these comments goes, as
it is, to the agent at the start of every run, after the runner's own rules on how to work and how to report.
Comments like this one are left out, so this file as it comes is empty: `arceus-runner check` fails and a run
reports that it had no instructions, until you write them.

The shape is yours; the agent reads plain prose as well as lists. What a run needs from you:
- Where the platform is: the URL of a test or staging stand, never production unless you mean it.
- How to get in: the test accounts by role, with their passwords. This file is next to secrets.env and readable
  by the runner's user only; use accounts made for testing, never a real person's.
- What to test, in the order you want it tested, and what you expect to see. The agent tests what you list; a
  line like "then look around the rest of the site" lets it go further.
- What it must not do: real payments, e-mails to real people, deleting shared data. Say how to clean up what it
  creates, if it should.
- Anything a tester would ask on the first day: a known bug to ignore, a page that is slow on purpose, the test
  card number the payment sandbox takes.

Write it in the language you want the report in. At most 64 KB without the comments. A run reads this file when
it starts and keeps the text it was given with its report, so editing it never changes a run under way.

An example to start from: copy it out of this comment and replace every value.

# Platform

Staging: https://staging.example.com
API docs: https://staging.example.com/api/docs

# Accounts

- Customer: customer@example.com / <password>
- Manager: manager@example.com / <password>
- Admin: admin@example.com / <password>, only for the admin checks below

# What to test

1. Sign-in and sign-out for each account. A wrong password shows an error under the field.
2. As the customer: search the catalogue, add two items to the cart, change the quantity of one, apply the
   discount code TEST10. The total shows the discount.
3. Checkout as the customer with the payment sandbox's test card, up to the order confirmation.
   The order is then in the customer's order history.
4. As the manager: find that order, change its status to "Shipped". The customer sees the new status.
5. Password reset: request it for customer@example.com and stop at the "e-mail sent" page.
6. The pages above on a phone-sized screen (390 px wide): nothing cut off or overlapping.

# Do not

- Pay with a real card or outside the payment sandbox.
- Change the admin's settings or other users' accounts.
- Leave test data behind: cancel the orders you made when you finish.

# Known

- The dashboard takes up to 10 seconds to load the first time after a deploy. That is not a bug.
-->
