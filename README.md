# labctl

Temporary OCI lab environments. Invite people into a compartment, let them build
whatever they want in it, then wipe it clean.

```
labctl bootstrap      # once, ever
labctl add-user a@b.c # per participant
  ... participants do their thing ...
labctl nuke           # end of lab
```

## How it works

One compartment holds everything. One group has full access to it. A policy
written once connects the two. After that, granting and revoking access is just
group membership, and ending a lab is just emptying the compartment — the
policy, quotas and tag defaults never need touching again.

## Setup

```bash
uv venv --python 3.12
uv pip install -e .
```

Requires an `~/.oci/config` profile with tenancy-admin rights. Edit `lab.toml`,
then:

```bash
labctl bootstrap          # dry run — shows exactly what it would create
labctl bootstrap --apply
```

This creates the compartment, the group, the access policy, compartment quotas,
and tag defaults. It is idempotent: re-run it any time to apply a changed
`lab.toml`.

## Daily use

```bash
labctl add-user alice@example.com          # dry run
labctl add-user alice@example.com --apply  # creates account, sends invite, grants access
labctl list-users
labctl remove-user alice@example.com --apply
labctl status
labctl doctor                              # check the setup is sound
```

`add-user` creates the account in the identity domain if it does not exist,
which is what triggers the activation email — that email is the invite. If
someone never receives it, `labctl resend-invite <email> --apply`.

`remove-user` cuts access by removing group membership first, then deletes the
account. Their *resources* are untouched; `nuke` handles those.

## Ending a lab

```bash
labctl nuke          # dry run — lists everything that would be destroyed
labctl nuke --yes    # then asks you to type the compartment name
```

`nuke` sweeps the lab compartment and every sub-compartment inside it, deleting
in dependency order across repeated passes until nothing is left. It is
irreversible.

**It does not delete the compartment itself**, by design. A compartment holding
a KMS vault cannot be deleted for at least 7 days, and keeping the compartment
means its OCID stays stable — so the policy and quotas survive and the next lab
starts clean with no setup.

## Things worth knowing

**Deletion order is load-bearing.** OCI refuses to delete a VCN with a live
subnet, or a subnet with a live instance. More subtly, an OKE node pool actively
re-creates any node you terminate — so clusters and their node pools are deleted
before compute, or the sweep never converges.

**Resource search lags.** Deletes are asynchronous and the search index trails
them by seconds to minutes, so `nuke` re-scans and retries, stopping when a pass
makes no progress.

**Nothing is skipped silently.** A resource type with no registered deleter is
reported, not ignored — an unknown resource quietly left running is exactly the
thing that keeps costing money. `labctl doctor` also verifies every registered
type against OCI's live list of searchable types, because a typo'd type name
would otherwise mean a deleter that simply never fires.

**Vaults are the one exception.** KMS vaults can only be *scheduled* for
deletion, minimum 7 days. `nuke` schedules them and says so.

**Quotas are deny-by-default.** Every compute shape is zeroed, then `lab.toml`
re-allows a named few. OCI defines hundreds of compute limits and adds more with
each hardware generation, so an allowlist stays correct where a denylist rots.
GPUs stay at zero unless you deliberately raise them.

**No state file.** Everything is resolved by name against live OCI on each run.
A state file would be permanently wrong here, since `nuke` destroys resources
created outside of it.

## Configuration

See `lab.toml` — every option is commented.
