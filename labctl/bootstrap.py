"""One-time tenancy setup: compartment, group, policy, quotas, tag defaults.

Every step is idempotent get-or-create, so re-running `bootstrap` is safe and is
the supported way to apply a changed lab.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import oci

from .auth import NotFound, Session

TAG_KEYS = {
    # Tag defaults support these OCI-substituted variables, so every resource a
    # participant creates is automatically stamped with who made it and when.
    # That is what makes `nuke` auditable and per-participant cost visible.
    "created-by": "${iam.principal.name}",
    "created-on": "${oci.datetime}",
}


@dataclass
class Step:
    name: str
    action: str  # "create" | "update" | "ok"
    detail: str = ""
    statements: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.action in ("create", "update")


def policy_statements(session: Session, comp_name: str) -> list[str]:
    cfg = session.config
    group = f"'{cfg.domain}'/'{cfg.group}'"

    conditions: list[str] = []
    if cfg.block_policy_management:
        conditions += [
            "request.permission != 'POLICY_CREATE'",
            "request.permission != 'POLICY_UPDATE'",
            "request.permission != 'POLICY_DELETE'",
        ]
    if cfg.restrict_to_region:
        conditions.append(f"request.region = '{_region_key(session)}'")

    if not conditions:
        where = ""
    elif len(conditions) == 1:
        where = f" where {conditions[0]}"
    else:
        where = " where all {" + ", ".join(conditions) + "}"

    return [
        # The core grant: full control of everything inside the lab compartment.
        # `manage` (not `use`) is what permits *creating* clusters, VCNs and nodes.
        f"Allow group {group} to manage all-resources in compartment {comp_name}{where}",
        # Without this the console's compartment picker is empty and the lab is
        # unusable through the UI.
        f"Allow group {group} to inspect compartments in tenancy",
        # Cloud Shell gives participants kubectl with no local API key setup.
        f"Allow group {group} to use cloud-shell in tenancy",
        # Required for any object-storage or OCIR work; the namespace is tenancy-level.
        f"Allow group {group} to read objectstorage-namespaces in tenancy",
    ]


def _region_key(session: Session) -> str:
    subs = session.identity_any_region.list_region_subscriptions(session.tenancy_id).data
    for sub in subs:
        if sub.region_name == session.config.region:
            return sub.region_key.lower()
    raise RuntimeError(
        f"region '{session.config.region}' is not subscribed in this tenancy "
        f"(subscribed: {', '.join(s.region_name for s in subs)})"
    )


# -- individual steps ----------------------------------------------------


def ensure_compartment(session: Session, apply: bool) -> Step:
    cfg = session.config
    try:
        comp = session.find_compartment(cfg.compartment)
        return Step(f"compartment '{cfg.compartment}'", "ok", comp.id)
    except NotFound:
        pass

    if not apply:
        return Step(f"compartment '{cfg.compartment}'", "create", "under tenancy root")

    comp = session.identity.create_compartment(
        oci.identity.models.CreateCompartmentDetails(
            compartment_id=session.tenancy_id,
            name=cfg.compartment,
            description="Temporary lab environment. Contents are wiped by `labctl nuke`.",
        )
    ).data
    oci.wait_until(
        session.identity,
        session.identity.get_compartment(comp.id),
        "lifecycle_state",
        "ACTIVE",
        max_wait_seconds=300,
    )
    return Step(f"compartment '{cfg.compartment}'", "create", comp.id)


def ensure_group(session: Session, apply: bool) -> Step:
    cfg = session.config
    client = session.domains_client
    existing = client.list_groups(filter=f'displayName eq "{cfg.group}"').data
    if existing.resources:
        return Step(f"group '{cfg.group}'", "ok", existing.resources[0].ocid)

    if not apply:
        return Step(f"group '{cfg.group}'", "create", f"in domain '{cfg.domain}'")

    group = client.create_group(
        oci.identity_domains.models.Group(
            schemas=["urn:ietf:params:scim:schemas:core:2.0:Group"],
            display_name=cfg.group,
            urn_ietf_params_scim_schemas_oracle_idcs_extension_group_group=(
                oci.identity_domains.models.ExtensionGroupGroup(
                    description="Full access to the lab compartment. Managed by labctl.",
                    creation_mechanism="api",
                )
            ),
        )
    ).data
    return Step(f"group '{cfg.group}'", "create", group.ocid)


def ensure_policy(session: Session, apply: bool) -> Step:
    cfg = session.config
    wanted = policy_statements(session, cfg.compartment)

    try:
        policy = session.find_policy(cfg.policy_name)
    except NotFound:
        if not apply:
            return Step(f"policy '{cfg.policy_name}'", "create", statements=wanted)
        created = session.identity.create_policy(
            oci.identity.models.CreatePolicyDetails(
                compartment_id=session.tenancy_id,
                name=cfg.policy_name,
                description="Lab compartment access for the lab group. Managed by labctl.",
                statements=wanted,
            )
        ).data
        return Step(f"policy '{cfg.policy_name}'", "create", created.id, wanted)

    if list(policy.statements) == wanted:
        return Step(f"policy '{cfg.policy_name}'", "ok", policy.id)

    if not apply:
        return Step(f"policy '{cfg.policy_name}'", "update", "statements differ", wanted)

    session.identity.update_policy(
        policy.id,
        oci.identity.models.UpdatePolicyDetails(statements=wanted),
    )
    return Step(f"policy '{cfg.policy_name}'", "update", policy.id, wanted)


def quota_statements(session: Session) -> list[str]:
    """Deny-by-default compute, plus explicit ceilings on storage and clusters."""
    cfg = session.config
    comp = cfg.compartment
    q = cfg.quotas

    stmts = [
        # Zero the whole family first; everything below re-opens a narrow door.
        f"zero compute-core quotas in compartment {comp}",
    ]
    for family in q.allowed_compute:
        stmts.append(f"set compute-core quota {family} to {q.standard_ocpus} in compartment {comp}")
    for family in q.allowed_gpu:
        stmts.append(f"set compute-core quota {family} to {q.gpu_count} in compartment {comp}")

    stmts.append(f"set block-storage quota total-storage-gb to {q.block_storage_gb} in compartment {comp}")
    stmts.append(f"set container-engine quota cluster-count to {q.clusters} in compartment {comp}")
    return stmts


def ensure_quotas(session: Session, apply: bool) -> Step:
    cfg = session.config
    name = cfg.quota_name

    if not cfg.quotas.enabled:
        return Step(f"quota '{name}'", "ok", "disabled in lab.toml")

    wanted = quota_statements(session)
    existing = None
    for quota in oci.pagination.list_call_get_all_results(
        session.quotas.list_quotas, session.tenancy_id
    ).data:
        if quota.name == name:
            existing = quota
            break

    if existing is None:
        if not apply:
            return Step(f"quota '{name}'", "create", statements=wanted)
        created = session.quotas.create_quota(
            oci.limits.models.CreateQuotaDetails(
                compartment_id=session.tenancy_id,
                name=name,
                description="Resource ceilings for the lab compartment. Managed by labctl.",
                statements=wanted,
            )
        ).data
        return Step(f"quota '{name}'", "create", created.id, wanted)

    current = session.quotas.get_quota(existing.id).data
    if list(current.statements) == wanted:
        return Step(f"quota '{name}'", "ok", existing.id)
    if not apply:
        return Step(f"quota '{name}'", "update", "statements differ", wanted)
    session.quotas.update_quota(
        existing.id, oci.limits.models.UpdateQuotaDetails(statements=wanted)
    )
    return Step(f"quota '{name}'", "update", existing.id, wanted)


def ensure_tag_defaults(session: Session, apply: bool) -> list[Step]:
    """Auto-stamp every resource created in the compartment with creator + timestamp."""
    cfg = session.config
    steps: list[Step] = []
    if not cfg.tags_enabled:
        return [Step("tag defaults", "ok", "disabled in lab.toml")]

    ns_name = cfg.tag_namespace
    namespace = None
    for candidate in oci.pagination.list_call_get_all_results(
        session.identity.list_tag_namespaces, session.tenancy_id
    ).data:
        if candidate.name == ns_name:
            namespace = candidate
            break

    if namespace is None:
        if not apply:
            steps.append(Step(f"tag namespace '{ns_name}'", "create", "in tenancy root"))
            for key in TAG_KEYS:
                steps.append(Step(f"tag '{ns_name}.{key}'", "create"))
            steps.append(Step("tag defaults on compartment", "create", str(list(TAG_KEYS))))
            return steps
        namespace = session.identity.create_tag_namespace(
            oci.identity.models.CreateTagNamespaceDetails(
                compartment_id=session.tenancy_id,
                name=ns_name,
                description="Lab resource attribution. Managed by labctl.",
            )
        ).data
        steps.append(Step(f"tag namespace '{ns_name}'", "create", namespace.id))
    else:
        steps.append(Step(f"tag namespace '{ns_name}'", "ok", namespace.id))

    existing_tags = {
        t.name: t
        for t in oci.pagination.list_call_get_all_results(
            session.identity.list_tags, namespace.id
        ).data
    }

    # Tag defaults attach to the compartment, so it must exist first.
    try:
        comp_id = session.lab_compartment.id
    except NotFound:
        steps.append(
            Step("tag defaults on compartment", "create", "pending: compartment not created yet")
        )
        return steps

    current_defaults = {
        d.tag_definition_name: d
        for d in oci.pagination.list_call_get_all_results(
            session.identity.list_tag_defaults, compartment_id=comp_id
        ).data
    }

    for key, value in TAG_KEYS.items():
        if key not in existing_tags:
            if not apply:
                steps.append(Step(f"tag '{ns_name}.{key}'", "create"))
            else:
                session.identity.create_tag(
                    namespace.id,
                    oci.identity.models.CreateTagDetails(
                        name=key, description=f"Lab attribution: {key}."
                    ),
                )
                steps.append(Step(f"tag '{ns_name}.{key}'", "create"))
        else:
            steps.append(Step(f"tag '{ns_name}.{key}'", "ok"))

        if key in current_defaults:
            steps.append(Step(f"tag default '{ns_name}.{key}'", "ok", value))
            continue
        if not apply:
            steps.append(Step(f"tag default '{ns_name}.{key}'", "create", value))
            continue
        session.identity.create_tag_default(
            oci.identity.models.CreateTagDefaultDetails(
                compartment_id=comp_id,
                tag_definition_id=existing_tags[key].id
                if key in existing_tags
                else session.identity.get_tag(namespace.id, key).data.id,
                value=value,
                is_required=False,
            )
        )
        steps.append(Step(f"tag default '{ns_name}.{key}'", "create", value))

    return steps


def run(session: Session, apply: bool) -> list[Step]:
    steps = [
        ensure_compartment(session, apply),
        ensure_group(session, apply),
        ensure_policy(session, apply),
    ]
    steps.extend(ensure_tag_defaults(session, apply))
    steps.append(ensure_quotas(session, apply))
    return steps
