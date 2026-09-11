"""Lab participant lifecycle: invite, revoke, list.

All access flows through group membership, so adding and removing a participant
never touches the policy. The policy is written once by `bootstrap` and is the
same for every lab.
"""

from __future__ import annotations

from dataclasses import dataclass

import oci
from oci.identity_domains import models as dm

from .auth import NotFound, Session

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
EXT_USER_SCHEMA = "urn:ietf:params:scim:schemas:oracle:idcs:extension:user:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class UserError(Exception):
    pass


@dataclass
class Participant:
    id: str
    ocid: str
    username: str
    email: str
    active: bool
    in_group: bool


def _escape(value: str) -> str:
    return value.replace('"', '\\"')


def find_user(session: Session, email: str):
    """Look up a domain user by username or primary email."""
    client = session.domains_client
    safe = _escape(email)
    for scim_filter in (f'userName eq "{safe}"', f'emails.value eq "{safe}"'):
        found = client.list_users(filter=scim_filter).data
        if found.resources:
            return found.resources[0]
    raise NotFound(f"no user in domain '{session.config.domain}' matching {email}")


def _group(session: Session):
    client = session.domains_client
    found = client.list_groups(filter=f'displayName eq "{_escape(session.config.group)}"').data
    if not found.resources:
        raise UserError(
            f"group '{session.config.group}' does not exist. Run `labctl bootstrap --apply` first."
        )
    # The list projection omits members; re-fetch the full group.
    return client.get_group(found.resources[0].id).data


def _member_ids(group) -> set[str]:
    return {m.value for m in (group.members or [])}


def _split_name(email: str, full_name: str | None) -> tuple[str, str]:
    if full_name:
        parts = full_name.split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return parts[0], parts[0]
    local = email.split("@", 1)[0]
    chunks = [c for c in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if c]
    if len(chunks) >= 2:
        return chunks[0].capitalize(), " ".join(c.capitalize() for c in chunks[1:])
    return (chunks[0].capitalize() if chunks else local), "Lab"


# -- operations ----------------------------------------------------------


def add_user(session: Session, email: str, full_name: str | None = None, apply: bool = False) -> list[str]:
    """Create the user if needed and put them in the lab group. Idempotent."""
    actions: list[str] = []
    client = session.domains_client
    group = _group(session)

    try:
        user = find_user(session, email)
        actions.append(f"user {email} already exists ({user.ocid})")
    except NotFound:
        if not apply:
            actions.append(f"would create user {email} and send an invite email")
            actions.append(f"would add {email} to group '{session.config.group}'")
            return actions
        given, family = _split_name(email, full_name)
        user = client.create_user(
            user=dm.User(
                schemas=[USER_SCHEMA, EXT_USER_SCHEMA],
                user_name=email,
                name=dm.UserName(given_name=given, family_name=family),
                emails=[dm.UserEmails(value=email, type="work", primary=True)],
                urn_ietf_params_scim_schemas_oracle_idcs_extension_user_user=dm.ExtensionUserUser(
                    # bypass_notification=False is what actually sends the
                    # activation email. That email IS the invite -- it carries
                    # the link the participant uses to set a password.
                    bypass_notification=False,
                    creation_mechanism="api",
                ),
            )
        ).data
        actions.append(f"created user {email} ({user.ocid}); invite email sent")

    if user.id in _member_ids(group):
        actions.append(f"{email} already in group '{session.config.group}'")
        return actions

    if not apply:
        actions.append(f"would add {email} to group '{session.config.group}'")
        return actions

    client.patch_group(
        group.id,
        patch_op=dm.PatchOp(
            schemas=[PATCH_SCHEMA],
            operations=[
                dm.Operations(
                    op="add",
                    path="members",
                    value=[{"value": user.id, "type": "User"}],
                )
            ],
        ),
    )
    actions.append(f"added {email} to group '{session.config.group}'")
    return actions


def resend_invite(session: Session, email: str, apply: bool = False) -> list[str]:
    """Re-trigger the activation email for someone who lost or never got it."""
    user = find_user(session, email)
    if not apply:
        return [f"would resend invite email to {email}"]
    session.domains_client.put_user_password_resetter(
        user.id,
        user_password_resetter=dm.UserPasswordResetter(
            schemas=["urn:ietf:params:scim:schemas:oracle:idcs:UserPasswordResetter"],
            bypass_notification=False,
        ),
    )
    return [f"resent invite email to {email}"]


def remove_user(session: Session, email: str, apply: bool = False) -> list[str]:
    """Delete the participant from the domain entirely.

    Group membership is removed first so access is cut even if the delete itself
    fails partway -- revoking access is the part that actually matters.
    """
    actions: list[str] = []
    client = session.domains_client
    user = find_user(session, email)
    group = _group(session)

    if user.id in _member_ids(group):
        if apply:
            client.patch_group(
                group.id,
                patch_op=dm.PatchOp(
                    schemas=[PATCH_SCHEMA],
                    operations=[dm.Operations(op="remove", path=f'members[value eq "{user.id}"]')],
                ),
            )
            actions.append(f"removed {email} from group '{session.config.group}'")
        else:
            actions.append(f"would remove {email} from group '{session.config.group}'")
    else:
        actions.append(f"{email} was not in group '{session.config.group}'")

    if not apply:
        actions.append(f"would delete user {email} from domain '{session.config.domain}'")
        return actions

    try:
        client.delete_user(user.id)
    except oci.exceptions.ServiceError as exc:
        # Some domain configurations refuse to delete an active user; deactivate
        # and retry rather than leaving the account half-removed.
        if exc.status not in (400, 409):
            raise
        client.put_user_status_changer(
            user.id,
            user_status_changer=dm.UserStatusChanger(
                schemas=["urn:ietf:params:scim:schemas:oracle:idcs:UserStatusChanger"],
                active=False,
            ),
        )
        client.delete_user(user.id)
        actions.append(f"deactivated {email} before delete")

    actions.append(f"deleted user {email}")
    return actions


def list_participants(session: Session) -> list[Participant]:
    group = _group(session)
    members = _member_ids(group)
    out: list[Participant] = []
    for member in group.members or []:
        try:
            user = session.domains_client.get_user(member.value).data
        except oci.exceptions.ServiceError:
            continue
        primary = next(
            (e.value for e in (user.emails or []) if e.primary),
            (user.emails[0].value if user.emails else ""),
        )
        out.append(
            Participant(
                id=user.id,
                ocid=user.ocid,
                username=user.user_name,
                email=primary,
                active=bool(user.active),
                in_group=user.id in members,
            )
        )
    return sorted(out, key=lambda p: p.username.lower())
