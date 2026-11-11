# ADR 1: native `cidr`/`inet` types and an EXCLUDE constraint

**Status:** accepted

**Context.** An IPAM must never hold two overlapping subnets in the same VRF, and the check
has to hold when two clients create subnets at the same time. Storing prefixes as text or
integers moves overlap logic into the application, where it races.

**Decision.** Store prefixes as PostgreSQL `cidr` and addresses as `inet`. Enforce
non-overlap with `EXCLUDE USING gist (vrf_id WITH =, prefix inet_ops WITH &&)` (`btree_gist`
provides `=` for the integer column). The API still pre-checks with the same `&&` operator to
return a readable 409 and to power `GET /subnets/overlaps`. `CHECK` constraints keep the
gateway inside the prefix and addresses as /32 or /128 hosts.

**Consequences.** The guarantee holds for any writer, including raw SQL (tested). The schema is
PostgreSQL-only, so tests use a real Postgres instead of SQLite.
