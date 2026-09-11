"""labctl — temporary OCI lab environments."""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from . import bootstrap as bootstrap_mod
from . import deleters, nuke as nuke_mod, users as users_mod
from .auth import AuthError, NotFound, Session
from .config import ConfigError, load
from .users import UserError

console = Console()
err = Console(stderr=True)

ACTION_STYLE = {"create": "green", "update": "yellow", "ok": "dim"}
ACTION_LABEL = {"create": "+ create", "update": "~ update", "ok": "= exists"}


def get_session(ctx) -> Session:
    try:
        return Session(load(ctx.obj["config"]))
    except (ConfigError, AuthError) as exc:
        err.print(f"[red]error:[/red] {exc}")
        sys.exit(1)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to lab.toml.")
@click.pass_context
def main(ctx, config_path):
    """Stand up a temporary OCI lab compartment, invite users, then wipe it."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config_path


# -- bootstrap -----------------------------------------------------------


@main.command()
@click.option("--apply", is_flag=True, help="Actually make the changes. Without this, dry run.")
@click.pass_context
def bootstrap(ctx, apply):
    """One-time setup: compartment, group, policy, quotas, tag defaults.

    Idempotent — re-run it to apply a changed lab.toml.
    """
    session = get_session(ctx)
    cfg = session.config

    console.print(
        f"[bold]tenancy[/bold] {cfg.source}  ->  "
        f"compartment [cyan]{cfg.compartment}[/cyan], group [cyan]{cfg.group}[/cyan], "
        f"domain [cyan]{cfg.domain}[/cyan], region [cyan]{cfg.region}[/cyan]"
    )
    if not apply:
        console.print("[yellow]dry run[/yellow] — nothing will be changed. Re-run with --apply.\n")
    else:
        console.print()

    try:
        steps = bootstrap_mod.run(session, apply=apply)
    except Exception as exc:  # noqa: BLE001 - surface any OCI failure verbatim
        err.print(f"[red]bootstrap failed:[/red] {exc}")
        sys.exit(1)

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("")
    table.add_column("resource")
    table.add_column("detail", overflow="fold")
    for step in steps:
        table.add_row(
            f"[{ACTION_STYLE[step.action]}]{ACTION_LABEL[step.action]}[/]",
            step.name,
            step.detail,
        )
    console.print(table)

    statements = [s for step in steps for s in step.statements]
    if statements:
        console.print("\n[bold]policy / quota statements:[/bold]")
        for stmt in statements:
            console.print(f"  [dim]{stmt}[/dim]")

    changed = [s for s in steps if s.changed]
    console.print()
    if not changed:
        console.print("[green]everything already in place.[/green]")
    elif apply:
        console.print(f"[green]applied {len(changed)} change(s).[/green]")
    else:
        console.print(f"[yellow]{len(changed)} change(s) pending.[/yellow] Re-run with --apply.")


# -- status --------------------------------------------------------------


@main.command()
@click.pass_context
def status(ctx):
    """Show what the lab looks like right now. Read-only."""
    session = get_session(ctx)
    cfg = session.config

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("tenancy", session.tenancy_id)
    table.add_row("home region", session.home_region)
    table.add_row("subscribed", ", ".join(session.subscribed_regions))
    table.add_row("lab region", cfg.region)

    try:
        comp = session.lab_compartment
        table.add_row("compartment", f"[green]{cfg.compartment}[/green]  {comp.id}")
    except NotFound:
        table.add_row("compartment", f"[red]{cfg.compartment} — not created[/red] (run `labctl bootstrap`)")

    try:
        domain = session.domain
        table.add_row("domain", f"{domain.display_name}  [dim]{domain.url}[/dim]")
    except AuthError as exc:
        table.add_row("domain", f"[red]{exc}[/red]")

    console.print(table)




# -- participants --------------------------------------------------------


def _run_user_op(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (UserError, NotFound) as exc:
        err.print(f"[red]error:[/red] {exc}")
        sys.exit(1)


@main.command("add-user")
@click.argument("emails", nargs=-1, required=True)
@click.option("--name", default=None, help="Full name. Defaults to a guess from the email.")
@click.option("--apply", is_flag=True, help="Actually invite. Without this, dry run.")
@click.pass_context
def add_user(ctx, emails, name, apply):
    """Invite one or more people and grant them the lab compartment.

    Creates the account if needed (which sends the activation email) and adds
    them to the lab group. Safe to re-run — existing users are left alone.
    """
    session = get_session(ctx)
    if not apply:
        console.print("[yellow]dry run[/yellow] — nothing will be changed. Re-run with --apply.\n")
    if len(emails) > 1 and name:
        err.print("[red]error:[/red] --name cannot be used with multiple emails.")
        sys.exit(1)

    for email in emails:
        actions = _run_user_op(users_mod.add_user, session, email, name, apply)
        for action in actions:
            console.print(f"  [green]•[/green] {action}")


@main.command("remove-user")
@click.argument("emails", nargs=-1, required=True)
@click.option("--apply", is_flag=True, help="Actually remove. Without this, dry run.")
@click.pass_context
def remove_user(ctx, emails, apply):
    """Revoke a participant's access to the lab compartment.

    Removes them from the lab group. The account itself is kept — deleting
    accounts is a separate, explicit step (`labctl purge-users`), and accounts
    labctl did not create are never deleted at all.

    Their resources are untouched; use `labctl nuke` for those.
    """
    session = get_session(ctx)
    if not apply:
        console.print("[yellow]dry run[/yellow] — nothing will be changed. Re-run with --apply.\n")
    for email in emails:
        actions = _run_user_op(users_mod.remove_user, session, email, apply)
        for action in actions:
            console.print(f"  [green]•[/green] {action}")


@main.command("list-created")
@click.pass_context
def list_created(ctx):
    """List accounts labctl created — the only ones it will ever delete."""
    session = get_session(ctx)
    created = _run_user_op(users_mod.list_created, session)
    if not created:
        console.print("labctl has not created any accounts in this domain.")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("email")
    table.add_column("created")
    table.add_column("access")
    table.add_column("status")
    for p in created:
        table.add_row(
            p.email or p.username,
            p.created_at or "[dim]unknown[/dim]",
            "[green]in group[/green]" if p.in_group else "[dim]revoked[/dim]",
            "[green]active[/green]" if p.active else "[yellow]inactive[/yellow]",
        )
    console.print(table)
    console.print(f"\n{len(created)} account(s) created by labctl. "
                  f"Delete them with [bold]labctl purge-users[/bold].")


@main.command("purge-users")
@click.argument("emails", nargs=-1)
@click.option("--apply", is_flag=True, help="Actually delete. Without this, dry run.")
@click.pass_context
def purge_users(ctx, emails, apply):
    """Delete accounts labctl created. Never touches anyone else.

    With no arguments, targets every labctl-created account. Pre-existing
    accounts in the domain are refused outright — this tool does not delete
    people it did not create.
    """
    session = get_session(ctx)
    actions = _run_user_op(users_mod.purge_users, session, emails or None, False)

    if actions == ["no labctl-created users to delete"]:
        console.print("no labctl-created accounts to delete.")
        return

    console.print("[bold]would delete these accounts:[/bold]")
    for action in actions:
        console.print(f"  [red]•[/red] {action}")

    if not apply:
        console.print("\n[yellow]dry run[/yellow] — nothing deleted. Re-run with --apply.")
        return

    typed = click.prompt(
        f"\nType the group name to confirm irreversible account deletion",
        default="",
        show_default=False,
    )
    if typed.strip() != session.config.group:
        err.print("[red]aborted[/red] — name did not match.")
        sys.exit(1)

    for action in _run_user_op(users_mod.purge_users, session, emails or None, True):
        console.print(f"  [green]•[/green] {action}")


@main.command("resend-invite")
@click.argument("email")
@click.option("--apply", is_flag=True, help="Actually send.")
@click.pass_context
def resend_invite(ctx, email, apply):
    """Re-send the activation email to someone who never received it."""
    session = get_session(ctx)
    for action in _run_user_op(users_mod.resend_invite, session, email, apply):
        console.print(f"  [green]•[/green] {action}")


@main.command("list-users")
@click.pass_context
def list_users(ctx):
    """Show who currently has access to the lab compartment."""
    session = get_session(ctx)
    participants = _run_user_op(users_mod.list_participants, session)
    if not participants:
        console.print(f"no members in group '{session.config.group}'.")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("username")
    table.add_column("email")
    table.add_column("status")
    table.add_column("origin")
    for p in participants:
        table.add_row(
            p.username,
            p.email,
            "[green]active[/green]" if p.active else "[yellow]inactive[/yellow]",
            "[dim]labctl[/dim]" if p.created_by_labctl
            else "[cyan]pre-existing[/cyan]",
        )
    console.print(table)
    console.print(f"\n{len(participants)} member(s) of '{session.config.group}'.")


# -- nuke ----------------------------------------------------------------


def _print_inventory(inv) -> None:
    if inv.total == 0:
        console.print("[green]compartment is already empty.[/green]")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("count", justify="right")
    table.add_column("resource type")
    table.add_column("note")
    for rtype, items in inv.by_type().items():
        if rtype in deleters.BY_TYPE:
            note = ""
        elif any(i in inv.in_flight for i in items):
            note = "[dim]delete already in flight[/dim]"
        else:
            note = "[yellow]no deleter — will be reported, not removed[/yellow]"
        table.add_row(str(len(items)), rtype, note)
    console.print(table)


@main.command()
@click.option("--yes", is_flag=True, help="Actually delete. Without this, dry run.")
@click.option("--rounds", default=8, show_default=True, help="Max sweep passes.")
@click.pass_context
def nuke(ctx, yes, rounds):
    """Delete every resource in the lab compartment.

    The compartment itself is kept, so the policy, quotas and tag defaults from
    `bootstrap` survive and the next lab starts clean without re-running setup.
    """
    session = get_session(ctx)
    cfg = session.config

    try:
        comp = session.lab_compartment
    except NotFound as exc:
        err.print(f"[red]error:[/red] {exc}")
        sys.exit(1)

    console.print(
        f"[bold]target[/bold] compartment [cyan]{cfg.compartment}[/cyan] "
        f"[dim]{comp.id}[/dim] in [cyan]{cfg.region}[/cyan]\n"
    )

    try:
        inventory, _ = nuke_mod.scan(session)
    except RuntimeError as exc:
        err.print(f"[red]error:[/red] {exc}")
        sys.exit(1)

    _print_inventory(inventory)
    if inventory.total == 0:
        return

    if not yes:
        console.print(
            f"\n[yellow]dry run[/yellow] — nothing deleted. "
            f"Re-run with [bold]--yes[/bold] to destroy these {inventory.total} resource(s)."
        )
        return

    # Typed confirmation: --yes alone is too easy to leave in shell history and
    # fire at the wrong compartment.
    typed = click.prompt(
        f"\nType the compartment name to confirm irreversible deletion",
        default="",
        show_default=False,
    )
    if typed.strip() != cfg.compartment:
        err.print("[red]aborted[/red] — name did not match.")
        sys.exit(1)

    def progress(round_number, inv):
        console.print(f"\n[bold]round {round_number}[/bold] — {inv.total} resource(s) remaining")

    remaining, outcomes = nuke_mod.run(session, apply=True, max_rounds=rounds, progress=progress)

    deleted = [o for o in outcomes if o.status == "deleted"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    failed = [o for o in outcomes if o.status == "failed"]

    console.print(f"\n[green]deleted {len(deleted)}[/green], "
                  f"[dim]skipped {len(skipped)}[/dim], "
                  f"[red]failed {len(failed)}[/red]")

    if failed:
        console.print("\n[bold red]could not delete:[/bold red]")
        for o in failed:
            console.print(f"  {o.item.resource_type} [dim]{o.item.display_name}[/dim] — {o.detail}")

    if remaining.total:
        console.print(f"\n[yellow]{remaining.total} resource(s) still present:[/yellow]")
        _print_inventory(remaining)
        if any(i.resource_type == "Vault" for i in remaining.known + remaining.unknown):
            console.print(
                "\n[dim]Vaults are scheduled for deletion, not deleted: 7 days is the "
                "minimum OCI allows. They stop being usable immediately.[/dim]"
            )
    else:
        console.print("\n[green]compartment is empty.[/green]")


# -- doctor --------------------------------------------------------------


@main.command()
@click.pass_context
def doctor(ctx):
    """Check the setup for problems before you rely on it."""
    session = get_session(ctx)
    cfg = session.config
    problems = 0

    def check(label, ok, detail=""):
        nonlocal problems
        if ok:
            console.print(f"  [green]✓[/green] {label} [dim]{detail}[/dim]")
        else:
            problems += 1
            console.print(f"  [red]✗[/red] {label} [red]{detail}[/red]")

    console.print("[bold]setup[/bold]")
    try:
        comp = session.lab_compartment
        check("lab compartment exists", True, comp.id)
    except NotFound:
        check("lab compartment exists", False, "run `labctl bootstrap --apply`")

    try:
        session.find_group(cfg.group)
        check("lab group exists", True, cfg.group)
    except NotFound:
        check("lab group exists", False, "run `labctl bootstrap --apply`")

    try:
        policy = session.find_policy(cfg.policy_name)
        wanted = bootstrap_mod.policy_statements(session, cfg.compartment)
        check("policy matches lab.toml", list(policy.statements) == wanted,
              "" if list(policy.statements) == wanted else "run `labctl bootstrap --apply`")
    except NotFound:
        check("policy exists", False, "run `labctl bootstrap --apply`")

    console.print("\n[bold]nuke coverage[/bold]")
    bogus, bogus_ignored = nuke_mod.verify_registry(session)
    check("every deleter targets a real resource type", not bogus, ", ".join(bogus))
    check("ignore list targets real resource types", not bogus_ignored, ", ".join(bogus_ignored))

    check("single region subscribed (nuke sweeps one region)",
          len(session.subscribed_regions) == 1,
          f"subscribed to {', '.join(session.subscribed_regions)} — nuke only sweeps {cfg.region}")

    console.print()
    if problems:
        console.print(f"[red]{problems} problem(s) found.[/red]")
        sys.exit(1)
    console.print("[green]all checks passed.[/green]")


if __name__ == "__main__":
    main()
