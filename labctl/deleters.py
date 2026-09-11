"""Per-resource-type deletion, ordered into dependency tiers.

OCI refuses out-of-order deletes (a VCN with a live subnet, a subnet with a live
instance), so ordering is not an optimisation here -- it is the difference
between the sweep converging and looping forever.

Anything not registered below is *reported*, never silently skipped: an unknown
resource type left behind in a lab compartment is exactly the thing that quietly
keeps costing money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import oci

from .auth import Session


@dataclass
class Context:
    """Per-round facts the deleters need, computed once."""

    session: Session
    # Default route tables / security lists / DHCP options are deleted by OCI
    # together with their VCN and cannot be deleted on their own.
    vcn_default_ids: set[str] = field(default_factory=set)


def build_context(session: Session, compartment_ids: list[str]) -> Context:
    ctx = Context(session=session)
    for comp_id in compartment_ids:
        try:
            vcns = oci.pagination.list_call_get_all_results(
                session.network.list_vcns, comp_id
            ).data
        except oci.exceptions.ServiceError:
            continue
        for vcn in vcns:
            for attr in ("default_route_table_id", "default_security_list_id", "default_dhcp_options_id"):
                value = getattr(vcn, attr, None)
                if value:
                    ctx.vcn_default_ids.add(value)
    return ctx


class SkipResource(Exception):
    """Not deletable on its own; it goes away with its parent."""


# -- compute / OKE -------------------------------------------------------


def _cluster(ctx, item):
    """Delete node pools first, then the cluster.

    Two reasons this cannot be left to resource search: node pools are not an
    indexed resource type at all, and a live node pool actively re-creates any
    node instance that gets terminated -- so sweeping instances while a pool
    still exists loops forever.
    """
    session = ctx.session
    pools = session.oke.list_node_pools(
        compartment_id=item.compartment_id, cluster_id=item.identifier
    ).data
    for pool in pools:
        try:
            session.oke.delete_node_pool(pool.id)
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
    session.oke.delete_cluster(item.identifier)


def _instance(ctx, item):
    # preserve_boot_volume=False so the boot volume goes with the instance
    # instead of becoming an orphan that still bills for storage.
    ctx.session.compute.terminate_instance(item.identifier, preserve_boot_volume=False)


def _instance_pool(ctx, item):
    ctx.session.compute_management.terminate_instance_pool(item.identifier)


def _instance_configuration(ctx, item):
    ctx.session.compute_management.delete_instance_configuration(item.identifier)


def _image(ctx, item):
    ctx.session.compute.delete_image(item.identifier)


# -- load balancers ------------------------------------------------------


def _load_balancer(ctx, item):
    ctx.session.load_balancer.delete_load_balancer(item.identifier)


def _network_load_balancer(ctx, item):
    ctx.session.network_load_balancer.delete_network_load_balancer(item.identifier)


# -- storage -------------------------------------------------------------


def _volume(ctx, item):
    ctx.session.block_storage.delete_volume(item.identifier)


def _boot_volume(ctx, item):
    ctx.session.block_storage.delete_boot_volume(item.identifier)


def _volume_backup(ctx, item):
    ctx.session.block_storage.delete_volume_backup(item.identifier)


def _boot_volume_backup(ctx, item):
    ctx.session.block_storage.delete_boot_volume_backup(item.identifier)


def _volume_group(ctx, item):
    ctx.session.block_storage.delete_volume_group(item.identifier)


def _file_system(ctx, item):
    ctx.session.file_storage.delete_file_system(item.identifier)


def _mount_target(ctx, item):
    ctx.session.file_storage.delete_mount_target(item.identifier)


def _export(ctx, item):
    ctx.session.file_storage.delete_export(item.identifier)


def _bucket(ctx, item):
    """Buckets must be fully empty -- objects, every version, in-flight
    multipart uploads, and pre-authenticated requests."""
    session = ctx.session
    ns = session.object_namespace
    name = item.display_name
    os_client = session.object_storage

    # Pre-authenticated requests keep a bucket alive and are easy to forget.
    for par in oci.pagination.list_call_get_all_results(
        os_client.list_preauthenticated_requests, ns, name
    ).data:
        os_client.delete_preauthenticated_request(ns, name, par.id)

    # Abort in-flight multipart uploads; their parts occupy storage.
    for upload in oci.pagination.list_call_get_all_results(
        os_client.list_multipart_uploads, ns, name
    ).data:
        os_client.abort_multipart_upload(ns, name, upload.object, upload.upload_id)

    # Versioned buckets need every version removed, not just current objects.
    try:
        versions = oci.pagination.list_call_get_all_results(
            os_client.list_object_versions, ns, name
        ).data
        for version in versions.items:
            os_client.delete_object(ns, name, version.name, version_id=version.version_id)
    except oci.exceptions.ServiceError:
        # Unversioned bucket: fall back to a plain object listing.
        start = None
        while True:
            page = os_client.list_objects(ns, name, start=start, limit=1000).data
            for obj in page.objects:
                os_client.delete_object(ns, name, obj.name)
            start = page.next_start_with
            if not start:
                break

    os_client.delete_bucket(ns, name)


def _container_repository(ctx, item):
    ctx.session.artifacts.delete_container_repository(item.identifier)


# -- data services -------------------------------------------------------


def _autonomous_database(ctx, item):
    ctx.session.database.delete_autonomous_database(item.identifier)


def _db_system(ctx, item):
    ctx.session.database.terminate_db_system(item.identifier)


def _mysql_db_system(ctx, item):
    ctx.session.mysql.delete_db_system(item.identifier)


# -- serverless / misc ---------------------------------------------------


def _function(ctx, item):
    ctx.session.functions_management.delete_function(item.identifier)


def _functions_application(ctx, item):
    ctx.session.functions_management.delete_application(item.identifier)


def _api_gateway(ctx, item):
    ctx.session.api_gateway.delete_gateway(item.identifier)


def _stream(ctx, item):
    ctx.session.streaming_admin.delete_stream(item.identifier)


def _topic(ctx, item):
    ctx.session.notifications.delete_topic(item.identifier)


def _bastion(ctx, item):
    ctx.session.bastion.delete_bastion(item.identifier)


# -- networking ----------------------------------------------------------


def _subnet(ctx, item):
    ctx.session.network.delete_subnet(item.identifier)


def _nsg(ctx, item):
    ctx.session.network.delete_network_security_group(item.identifier)


def _route_table(ctx, item):
    if item.identifier in ctx.vcn_default_ids:
        raise SkipResource("default route table, removed with its VCN")
    ctx.session.network.delete_route_table(item.identifier)


def _security_list(ctx, item):
    if item.identifier in ctx.vcn_default_ids:
        raise SkipResource("default security list, removed with its VCN")
    ctx.session.network.delete_security_list(item.identifier)


def _dhcp_options(ctx, item):
    if item.identifier in ctx.vcn_default_ids:
        raise SkipResource("default DHCP options, removed with its VCN")
    ctx.session.network.delete_dhcp_options(item.identifier)


def _nat_gateway(ctx, item):
    ctx.session.network.delete_nat_gateway(item.identifier)


def _internet_gateway(ctx, item):
    ctx.session.network.delete_internet_gateway(item.identifier)


def _service_gateway(ctx, item):
    ctx.session.network.delete_service_gateway(item.identifier)


def _local_peering_gateway(ctx, item):
    ctx.session.network.delete_local_peering_gateway(item.identifier)


def _drg_attachment(ctx, item):
    ctx.session.network.delete_drg_attachment(item.identifier)


def _drg(ctx, item):
    ctx.session.network.delete_drg(item.identifier)


def _public_ip(ctx, item):
    ctx.session.network.delete_public_ip(item.identifier)


def _vcn(ctx, item):
    ctx.session.network.delete_vcn(item.identifier)


def _dns_zone(ctx, item):
    """Private zones attached to a VCN are protected and vanish with the VCN;
    only user-created zones are deletable here."""
    try:
        zone = ctx.session.dns.get_zone(item.identifier).data
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            return
        raise
    if getattr(zone, "is_protected", False):
        raise SkipResource("protected zone, removed with its VCN")
    ctx.session.dns.delete_zone(item.identifier)


# -- vault / IAM ---------------------------------------------------------


def _vault(ctx, item):
    """KMS vaults cannot be deleted immediately -- 7 days is the shortest
    schedule OCI permits. This is the one resource `nuke` cannot fully remove."""
    import datetime

    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    ctx.session.vaults.schedule_vault_deletion(
        item.identifier,
        oci.key_management.models.ScheduleVaultDeletionDetails(time_of_deletion=when),
    )


def _policy(ctx, item):
    ctx.session.identity.delete_policy(item.identifier)


# -- tier table ----------------------------------------------------------

# Ordered outermost-dependency first. Within a tier, order does not matter.
TIERS: list[tuple[str, dict]] = [
    ("kubernetes", {"ClustersCluster": _cluster}),
    (
        "compute",
        {
            "InstancePool": _instance_pool,
            "Instance": _instance,
            "InstanceConfiguration": _instance_configuration,
        },
    ),
    (
        "load balancers",
        {"LoadBalancer": _load_balancer, "NetworkLoadBalancer": _network_load_balancer},
    ),
    (
        "data services",
        {
            "AutonomousDatabase": _autonomous_database,
            "DbSystem": _db_system,
            "MysqlDbSystem": _mysql_db_system,
        },
    ),
    (
        "serverless & misc",
        {
            "FunctionsFunction": _function,
            "FunctionsApplication": _functions_application,
            "ApiGateway": _api_gateway,
            "Stream": _stream,
            "OnsTopic": _topic,
            "Bastion": _bastion,
        },
    ),
    (
        "storage",
        {
            "BootVolume": _boot_volume,
            "Volume": _volume,
            "VolumeGroup": _volume_group,
            "VolumeBackup": _volume_backup,
            "BootVolumeBackup": _boot_volume_backup,
            "Export": _export,
            "MountTarget": _mount_target,
            "FileSystem": _file_system,
            "Bucket": _bucket,
            "ContainerRepo": _container_repository,
            "Image": _image,
        },
    ),
    (
        "network attachments",
        {
            "NetworkSecurityGroup": _nsg,
            "Subnet": _subnet,
            "PublicIp": _public_ip,
            "DrgAttachment": _drg_attachment,
        },
    ),
    (
        "network core",
        {
            "RouteTable": _route_table,
            "SecurityList": _security_list,
            "DHCPOptions": _dhcp_options,
            "NatGateway": _nat_gateway,
            "InternetGateway": _internet_gateway,
            "ServiceGateway": _service_gateway,
            "LocalPeeringGateway": _local_peering_gateway,
            "Drg": _drg,
        },
    ),
    ("vcn", {"Vcn": _vcn}),
    ("dns", {"CustomerDnsZone": _dns_zone}),
    ("vaults", {"Vault": _vault}),
    ("policies", {"Policy": _policy}),
]

BY_TYPE: dict[str, object] = {rt: fn for _, types in TIERS for rt, fn in types.items()}
TIER_INDEX: dict[str, int] = {
    rt: idx for idx, (_, types) in enumerate(TIERS) for rt in types
}

# Resource-search rows that are not independently deletable and must not be
# reported as "unknown" -- they disappear with their parent resource.
IGNORED_TYPES = {
    "PrivateIp",       # goes with its VNIC
    "Vnic",            # goes with its instance
    "Key",             # goes with its vault
    "VaultSecret",     # goes with its vault
    "ContainerImage",  # goes with its container repo
    "DnsResolver",     # created and destroyed with its VCN
    "DnsView",         # created and destroyed with its VCN
    "TagDefault",      # written by `bootstrap`, not by participants
}
