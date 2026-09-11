"""Lab participant lifecycle: invite, revoke, list.

All access flows through group membership, so adding and removing a participant
never touches the policy. The policy is written once by `bootstrap` and is the
same for every lab.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import oci
from oci.identity_domains import models as dm

from .auth import NotFound, Session

# Accounts labctl creates are stamped with these OCI freeform tags. Everything
# destructive keys off them: labctl will not delete an account it did not create.
# The domain already held 16 accounts before this tool existed -- real people --
# and an untagged account is assumed to be one of them.
MARKER_TAG = "labctl"
CREATED_AT_TAG = "labctl-created-at"

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
EXT_USER_SCHEMA = "urn:ietf:params:scim:schemas:oracle:idcs:extension:user:User"
OCI_TAGS_SCHEMA = "urn:ietf:params:scim:schemas:oracle:idcs:extension:OCITags"
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
    created_by_labctl: bool = False
    created_at: str = ""


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
        if created_by_labctl(user):
            actions.append(f"user {email} already exists (created by labctl)")
        else:
            # Adopting a pre-existing account into the group is fine, but it must
            # NOT get the marker: labctl did not create it and must never delete it.
            actions.append(f"user {email} already exists [pre-existing — labctl will not delete it]")
    except NotFound:
        if not apply:
            actions.append(f"would create user {email} and send an invite email")
            actions.append(f"would add {email} to group '{session.config.group}'")
            return actions
        given, family = _split_name(email, full_name)
        user = client.create_user(
            user=dm.User(
                schemas=[USER_SCHEMA, EXT_USER_SCHEMA, OCI_TAGS_SCHEMA],
                user_name=email,
                name=dm.UserName(given_name=given, family_name=family),
                emails=[dm.UserEmails(value=email, type="work", primary=True)],
                urn_ietf_params_scim_schemas_oracle_idcs_extension_oci_tags=_marker_tags(session),
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
    """Revoke access by removing the participant from the lab group.

    Deliberately does NOT delete the account. Most accounts in this domain
    pre-date labctl and belong to real people; deleting one is irreversible and
    is not something a routine "remove this participant" should ever risk.
    Deleting accounts labctl created is a separate, explicit command:
    `labctl purge-users`.
    """
    actions: list[str] = []
    client = session.domains_client
    user = find_user(session, email)
    group = _group(session)

    if user.id not in _member_ids(group):
        actions.append(f"{email} is not in group '{session.config.group}' — nothing to do")
        return actions

    if not apply:
        actions.append(f"would remove {email} from group '{session.config.group}'")
    else:
        client.patch_group(
            group.id,
            patch_op=dm.PatchOp(
                schemas=[PATCH_SCHEMA],
                operations=[dm.Operations(op="remove", path=f'members[value eq "{user.id}"]')],
            ),
        )
        actions.append(f"removed {email} from group '{session.config.group}' — access revoked")

    if created_by_labctl(user):
        actions.append(f"account kept. `labctl purge-users {email}` deletes it")
    else:
        actions.append("account kept (pre-existing user, labctl will never delete it)")
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
                created_by_labctl=created_by_labctl(user),
                created_at=_freeform(user).get(CREATED_AT_TAG, ""),
            )
        )
    return sorted(out, key=lambda p: p.username.lower())


# -- provenance ----------------------------------------------------------


def _marker_tags(session: Session) -> dm.ExtensionOCITags:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return dm.ExtensionOCITags(
        freeform_tags=[
            dm.FreeformTags(key=MARKER_TAG, value=session.config.compartment),
            dm.FreeformTags(key=CREATED_AT_TAG, value=now),
        ]
    )


def _freeform(user) -> dict[str, str]:
    ext = getattr(user, "urn_ietf_params_scim_schemas_oracle_idcs_extension_oci_tags", None)
    if not ext or not getattr(ext, "freeform_tags", None):
        return {}
    return {tag.key: tag.value for tag in ext.freeform_tags}


def created_by_labctl(user) -> bool:
    """True only for accounts this tool created. Untagged accounts are assumed
    to be pre-existing real users and are never deleted."""
    return MARKER_TAG in _freeform(user)


def _all_domain_users(session: Session) -> list:
    """Every user in the domain. SCIM paginates with startIndex/count."""
    client = session.domains_client
    out: list = []
    start, page_size = 1, 200
    while True:
        page = client.list_users(
            start_index=start,
            count=page_size,
            attributes="userName,emails,active,ocid,urn:ietf:params:scim:schemas:oracle:idcs:extension:OCITags:freeformTags",
        ).data
        resources = page.resources or []
        out.extend(resources)
        total = page.total_results or 0
        start += len(resources)
        if not resources or start > total:
            break
    return out


def list_created(session: Session) -> list[Participant]:
    """Accounts labctl created, whether or not they are still in the group."""
    group = _group(session)
    members = _member_ids(group)
    out: list[Participant] = []
    for user in _all_domain_users(session):
        tags = _freeform(user)
        if MARKER_TAG not in tags:
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
                created_by_labctl=True,
                created_at=tags.get(CREATED_AT_TAG, ""),
            )
        )
    return sorted(out, key=lambda p: p.username.lower())


def purge_users(
    session: Session, emails: tuple[str, ...] | None = None, apply: bool = False
) -> list[str]:
    """Delete accounts labctl created. Refuses to touch anything else.

    Separate from `remove-user` and from `nuke` on purpose: revoking access and
    destroying an account are different decisions, and only one of them is
    irreversible.
    """
    actions: list[str] = []
    candidates = list_created(session)

    if emails:
        by_email = {p.email.lower(): p for p in candidates}
        by_name = {p.username.lower(): p for p in candidates}
        selected = []
        for email in emails:
            key = email.lower()
            found = by_email.get(key) or by_name.get(key)
            if found is None:
                # Distinguish "does not exist" from "exists but we did not create it".
                try:
                    find_user(session, email)
                    raise UserError(
                        f"{email} exists but was not created by labctl — refusing to delete it. "
                        f"Use `labctl remove-user {email}` to revoke access instead."
                    )
                except NotFound:
                    raise UserError(f"no labctl-created user matching {email}") from None
            selected.append(found)
        candidates = selected

    if not candidates:
        return ["no labctl-created users to delete"]

    for participant in candidates:
        if not apply:
            actions.append(f"would delete {participant.email} (created {participant.created_at})")
            continue
        actions.extend(_delete_account(session, participant.id, participant.email))
    return actions


def _delete_account(session: Session, user_id: str, email: str) -> list[str]:
    """Drop group membership first, then the account. Revoking access is the
    part that matters, so it happens even if the delete then fails."""
    actions: list[str] = []
    client = session.domains_client
    group = _group(session)

    if user_id in _member_ids(group):
        client.patch_group(
            group.id,
            patch_op=dm.PatchOp(
                schemas=[PATCH_SCHEMA],
                operations=[dm.Operations(op="remove", path=f'members[value eq "{user_id}"]')],
            ),
        )
        actions.append(f"removed {email} from group '{session.config.group}'")

    try:
        client.delete_user(user_id)
    except oci.exceptions.ServiceError as exc:
        if exc.status not in (400, 409):
            raise
        client.put_user_status_changer(
            user_id,
            user_status_changer=dm.UserStatusChanger(
                schemas=["urn:ietf:params:scim:schemas:oracle:idcs:UserStatusChanger"],
                active=False,
            ),
        )
        client.delete_user(user_id)
        actions.append(f"deactivated {email} before delete")

    actions.append(f"deleted account {email}")
    return actions
