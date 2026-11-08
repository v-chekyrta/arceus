"""The web console: one address, with a sign-in, for everything a person does with the factory.

It replaces the SSH tunnels to Grafana and the two report pages. People register, an admin approves them and gives
them roles; what each role may do is checked on the server for every request (auth.py). The console runs in a
container of its own (console/ at the repository's root), with nginx in front of it and Grafana behind it; what
needs root on the machine (a restart, setup) it asks the host for (hostops.py), from a fixed list.

Its own state is console.db (store.py), beside runner.db: people, sessions, invitations, what they did. The runner's
state stays the runner's: the console reads runner.db and writes only what a person decides there.
"""
