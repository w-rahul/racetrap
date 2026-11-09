# Security

## Reporting a problem

Please report security problems in private, through [Report a vulnerability](https://github.com/w-rahul/racetrap/security/advisories/new) on GitHub. Do not open a public issue for them. I aim to reply within a week.

Fixes go into the latest release.

## Before you run it

racetrap is a test tool, and it is built to run against test systems only.

- Point it at a test database you can wipe. It runs your setup SQL and reads every table in the schema.
- A scenario file is code. `command` starts a process, and `setup`, `init` and `invariants` run SQL. Only run scenario files you trust.
- The proxy listens on 127.0.0.1 by default and speaks plain TCP, without TLS. Do not expose it on a network you do not trust.
- The HTML report contains SQL statements and rows from the test database. Treat it like test data.
