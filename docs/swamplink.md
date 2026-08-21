# swamplink tools landing page

The roost marketing page at <https://swamplink.com/tools/roost/> and the
family catalog at <https://swamplink.com/tools/versions.json> are **not**
files in this GitHub repository.

They live in **swamplink-root**, a separate git repo on lynx
(`/srv/git/swamplink-root.git`). Pushing that repo is deploying: a
post-receive hook unpacks into `/var/www/swamplink`. xycalc already splices
one file into the same tree (`tools/xycalc/calculator/index.html`) from its
own `deploy-calculator.yml`.

`versions.json` is a multi-tool catalog. Live shape (2026-08-21):

```json
{
  "roost": { "version": "0.10.1", "date": "2026-08-21" },
  "leghorn": { "version": "0.4.14", "date": "2026-08-15" },
  "legbar": { "version": "0.3.6", "date": "2026-08-20" }
}
```

This repo owns **only the roost stanza**. The source of truth that must match
`roost.py` `__version__` is [`packaging/swamplink-roost.json`](../packaging/swamplink-roost.json).
`packaging/check-version-consistency.sh` asserts that, and
`packaging/publish-swamplink-roost.sh` rewrites just the `roost` key in a
catalog file, leaving every other tool alone.

`release.yml`'s `swamplink-tools` job clones swamplink-root over SSH and runs
that script when the `SWAMPLINK_SSH_KEY`, `SWAMPLINK_HOST`, and
`SWAMPLINK_KNOWN_HOSTS` secrets exist (same names xycalc uses). Until those
secrets are registered on this repo, the live catalog will not move on a tag;
bump `tools/versions.json` in swamplink-root by hand, or copy the stanza from
`packaging/swamplink-roost.json`.

Do not add the full catalog to this repo. A roost-only checkout cannot be
the authority for leghorn or legbar versions.
