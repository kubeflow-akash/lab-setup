# labctl

Temporary OCI lab environments. Invite people into a compartment, let them build
whatever they want in it, then wipe it clean.

```bash
labctl bootstrap       # once, ever
labctl add-user a@b.c  # per participant
  ... participants do their thing ...
labctl nuke            # end of lab
```

## Install

```bash
uv tool install --editable .
```

Requires an `~/.oci/config` profile with tenancy-admin rights. Edit `lab.toml`, then:

```bash
labctl bootstrap          # dry run — shows exactly what it would create
labctl bootstrap --apply
```

## More usage details

For full command usage, account lifecycle details, lab cleanup behavior, and
security notes, see [usage.md](usage.md).
