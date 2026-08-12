# Naming: roost-top is the published name

**Status: settled 2026-08-11.** `roost-top` is the real published name on npm and
PyPI, not a temporary workaround. Stop treating the bare name `roost` as pending.

## Why

**PyPI — bare `roost` is unobtainable, permanently.** The name is on PyPI's
prohibited list, not merely registered. [pypi/support#10145][1] was an identical
PEP 541 request from an unrelated user with an active project and a TestPyPI
publish already in place; a moderator closed it on 2026-07-30 with "the project
name `roost` is unfortunately considered a risk, so we are not going to release
it." Prohibited names are not granted to anyone, so there is no request we could
file that would land. Do not re-file.

The cause is almost certainly the deleted `roost` 0.1.0 (March 2021, described
"Remind Supply Chain Risks", pointing at a `github.com/roost` that never
existed) — a dependency-confusion proof-of-concept from the wave that followed
Alex Birsan's February 2021 disclosure. PyPI's refusal text lists malware *or*
dependency confusion as grounds; this is the second.

**There is no malware called Roost.** Searching "roost malware" surfaces only the
English idiom — Unit 42 titled a HenBox writeup "The Chickens Come Home to
Roost", and analysts say malware "roosts" to mean persistence. The nearest real
families are name-collisions on *Rust* (Rustock, RustDoor, Rustonotto). We do not
put a disclaimer anywhere: denying an association nobody has made is what creates
it, and it would be the only page on the internet with "roost" and "malware" in
the same sentence about this project. If someone asks directly, answer in that
thread.

**npm — dispute dropped.** A name dispute was emailed to the `roost` owner
(websecurify, npmjs@websecurify.com; source repo archived since 2018) with
support@npmjs.com CC'd on 2026-08-10, starting npm's ~4-week abandonment clock
(earliest adjudication ~2026-09-07). Given PyPI is closed permanently, winning
npm would only split the naming across registries, so the dispute is abandoned.
If npm transfers the name unprompted, that is a decision to revisit, not an
obligation to act on.

**winget is unaffected.** Identifiers there are `Publisher.Package`, so the
package half is already the bare `roost` with no contest — `WillyGarage.Roost`
and `DiscoverWorthy.Roost` coexist in the same repo. Published as
`gmhoward9289-ops.roost`; [PR #411432][2] (v0.6.1) merged 2026-08-11.

## What each surface is called

| Surface | Name |
|---|---|
| npm package | `roost-top` |
| PyPI distribution | `roost-top` |
| winget | `gmhoward9289-ops.roost` |
| Homebrew formula, `.deb` | `roost` |
| Repo, module, man page | `roost` |
| CLI command | `roost` |

The **CLI command stays `roost`** (decided 2026-08-11). Package name ≠ command
name is the normal shape — `typescript` installs `tsc`, `@vue/cli` installs
`vue` — and the `-top` suffix is a registry-collision artifact, not part of the
product's identity. Users read the install line once and type the command
forever, so optimizing the typed-once string at the cost of the typed-daily one
is backwards. A rename would also break existing installs and force the Homebrew
formula and `.deb`, which already ship `roost`, to follow or diverge.

## Name collisions users actually hit

The collisions are on the bare `roost` that people naturally *try*, never on
`roost-top` — that name is uniquely ours everywhere. Install docs must therefore
always give the exact package name or `--id`, never a bare `install roost`.

| Registry | Bare `roost` resolves to | Severity |
|---|---|---|
| npm | websecurify's 2013 "System provisioning toolkit", which **ships `bin: {roost: ./bin/roost}`** | **Silent** — installs a different program and puts its own `roost` on PATH |
| winget | Ambiguous: `DiscoverWorthy.Roost` also declares `Moniker: roost` | Loud — winget refuses and demands `--id` |
| crates.io | An existing placeholder crate, described "Coming soon" | Squatted, inert |
| PyPI | Nothing — the name is prohibited | Harmless, `pip install roost` just errors |
| Homebrew core | No such formula | Clear |

The npm one is the dangerous case, because it succeeds. `npm i -g roost`
installs an abandoned 2013 provisioning tool that claims the `roost` command,
and nothing tells the user they got the wrong thing.

**`DiscoverWorthy.Roost` is worth knowing about specifically.** It is not a
squat — it is a live, adjacent product in our own niche, described as "Launch a
Claude Code terminal per project and snap them into place across your monitors."
It published to winget on 2026-07-17, ahead of us, and claims the same moniker.
Two Claude Code tools named Roost is a product-identity fact to be aware of, not
a packaging bug to fix. It installs via a Nullsoft installer and declares no
`Commands`, so it is unlikely to contend for `roost` on PATH; the collision is
at install-resolution time, not at runtime.

## Deferred work

- winget is two releases behind: published 0.6.1, latest is v0.8.0. Needs a
  version-update PR.
- The winget publisher half is `gmhoward9289-ops`, a GitHub account name rather
  than a publisher identity. Changing it post-publish means republishing under a
  new `PackageIdentifier` and getting the old path removed, and existing installs
  stop seeing updates — cheaper now than after more versions accumulate.

[1]: https://github.com/pypi/support/issues/10145
[2]: https://github.com/microsoft/winget-pkgs/pull/411432
