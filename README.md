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

## Install

```bash
uv tool install --editable .
```

This puts `labctl` on your PATH (via `~/.local/bin`) so you can run it as a
plain command. `--editable` points it at this checkout, so edits to the source
take effect immediately with no reinstall.

To update it later after pulling changes, nothing is needed. To remove it:
`uv tool uninstall labctl`.

<details>
<summary>Prefer not to install it globally?</summary>

```bash
uv venv --python 3.12 && uv pip install -e .
uv run labctl <command>          # or: source .venv/bin/activate
```

Note that a plain `uv pip install -e .` does *not* put `labctl` on your PATH —
it installs into `.venv/`, so you need `uv run` or an activated venv.
</details>

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
labctl list-users                          # who has access right now
labctl remove-user alice@example.com --apply   # revokes access, keeps the account
labctl status
labctl doctor                              # check the setup is sound
```

`add-user` creates the account in the identity domain if it does not exist,
which is what triggers the activation email — that email is the invite. If
someone never receives it, `labctl resend-invite <email> --apply`.

### Access and accounts are separate

Revoking access and deleting an account are different decisions, and only one of
them is irreversible. labctl keeps them apart:

| | command | reversible |
|---|---|---|
| revoke access | `remove-user` | yes — just re-add them |
| delete the account | `purge-users` | **no** |
| delete their resources | `nuke` | **no** |

`remove-user` only removes someone from the lab group. That cuts all access
immediately, because the group is what carries the permission. The account
stays.

### labctl only deletes accounts it created

Every account labctl creates is stamped with an OCI freeform tag
(`labctl = <compartment>`, plus a creation timestamp). These tags are visible on
the user in the OCI console.

That tag is the *only* thing `purge-users` will act on. An untagged account is
assumed to belong to a real person who was in the domain before this tool
existed, and labctl refuses to delete it — naming one explicitly is an error,
not a confirmation prompt.

Adding a pre-existing user to a lab is fine and normal; they get access but no
tag, so they are never a deletion candidate.

```bash
labctl list-created          # accounts labctl created — the only deletable ones
labctl purge-users           # dry run: shows every account that would be deleted
labctl purge-users --apply   # then asks you to type the group name
labctl purge-users alice@example.com --apply   # or just one
```

`nuke` never touches accounts at all — only resources.

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

**Accounts outlive labs.** Participants are removed from the group at the end of
a lab, not deleted. A domain typically contains real people who pre-date the
tool, so deletion is opt-in, separate, and restricted to tagged accounts.

**Vaults are the one exception.** KMS vaults can only be *scheduled* for
deletion, minimum 7 days. `nuke` schedules them and says so.

**Quotas are deny-by-default.** Every compute shape is zeroed, then `lab.toml`
re-allows a named few. OCI defines hundreds of compute limits and adds more with
each hardware generation, so an allowlist stays correct where a denylist rots.
GPUs stay at zero unless you deliberately raise them.

**No state file.** Everything is resolved by name against live OCI on each run.
A state file would be permanently wrong here, since `nuke` destroys resources
created outside of it.

## Security notes

**The compartment boundary is the whole security model.** `manage all-resources`
inside it is broad by design — that is the point of a lab. What matters is that
nothing inside can reach out.

Four things could let it reach out, and all four are addressed:

- *Writing its own policies.* Blocked by a `request.permission` condition, so a
  participant cannot widen their own grant.
- *Instance principals.* A participant can launch instances. If a dynamic group
  matched those instances and a policy gave that dynamic group tenancy-wide
  rights, the compartment boundary would be gone. `labctl doctor` checks for
  exactly this combination on every run.
- *Raising their own limits.* The quota and the policy both live in the tenancy
  root, outside the compartment, so participants cannot edit or delete either.
  `doctor` asserts this.
- *Escaping into sub-compartments.* Blocked — a participant building clusters
  has no need to create one, and it keeps `nuke` sweeping a flat space.

**Conditions fail open.** Each block above is a `request.permission != '...'`
condition. A misspelled permission name does not break access — it silently
stops blocking. Worth confirming once, by signing in as a lab user and trying to
create a policy in the compartment. It should be refused.

**Invites are the softest edge.** `add-user` emails an activation link, so a
typo'd address invites a stranger into the lab group. Set
`allowed_email_domains` in `lab.toml` to the domains you actually invite from.

**Vault creation is blocked by default.** Not for confidentiality — a vault
cannot be deleted for at least 7 days, so one created by accident keeps the
compartment dirty long after the lab ends, and Virtual Private Vaults are priced
far above anything else a lab would create.

**Quotas cap resources, not spend.** They stop a participant allocating 64 OCPUs
or a GPU node. They do not stop egress, or many small load balancers. For a hard
ceiling on cost, add an OCI budget with alerts on the compartment.

**Access has no expiry.** Nothing revokes a participant automatically. Ending a
lab is a manual `remove-user` / `nuke`.

**Attribution.** Every resource is auto-tagged with its creator and creation
time, and every account labctl creates is tagged too — so OCI Audit plus those
tags will tell you who did what.

## Configuration

See `lab.toml` — every option is commented.

`labctl` finds `lab.toml` by walking up from the current directory, so run it
from anywhere inside this repo. From elsewhere, pass `--config /path/to/lab.toml`.
