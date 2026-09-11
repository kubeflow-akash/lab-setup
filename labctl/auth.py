"""OCI client construction and OCID resolution.

Everything in labctl addresses resources by *name* (from lab.toml) and resolves
them against live OCI on each run. There is deliberately no local state file:
`nuke` destroys resources that no state file could have known about, so a state
file would be permanently, silently wrong.
"""

from __future__ import annotations

import functools

import oci

from .config import Config


class AuthError(Exception):
    pass


class NotFound(Exception):
    """A named resource does not exist yet."""


class Session:
    def __init__(self, config: Config):
        self.config = config
        try:
            self.oci_config = oci.config.from_file(profile_name=config.profile)
            oci.config.validate_config(self.oci_config)
        except oci.exceptions.ConfigFileNotFound as exc:
            raise AuthError(
                f"no OCI config found. Run `oci setup config` first. ({exc})"
            ) from exc
        except oci.exceptions.ProfileNotFound as exc:
            raise AuthError(
                f"profile '{config.profile}' not found in ~/.oci/config. ({exc})"
            ) from exc
        except oci.exceptions.InvalidConfig as exc:
            raise AuthError(f"OCI config for profile '{config.profile}' is invalid: {exc}") from exc

        self.tenancy_id: str = self.oci_config["tenancy"]

    # -- clients ---------------------------------------------------------

    def _client(self, cls, region: str | None = None):
        conf = dict(self.oci_config)
        if region:
            conf["region"] = region
        return cls(conf)

    @functools.cached_property
    def home_region(self) -> str:
        subs = self.identity_any_region.list_region_subscriptions(self.tenancy_id).data
        for sub in subs:
            if sub.is_home_region:
                return sub.region_name
        raise AuthError("tenancy reports no home region")

    @functools.cached_property
    def subscribed_regions(self) -> list[str]:
        subs = self.identity_any_region.list_region_subscriptions(self.tenancy_id).data
        return [s.region_name for s in subs]

    @functools.cached_property
    def identity_any_region(self):
        """Only for calls that work in any region (region subscriptions)."""
        return self._client(oci.identity.IdentityClient)

    @functools.cached_property
    def identity(self):
        """IAM writes (compartments, policies, groups) must target the home region."""
        return self._client(oci.identity.IdentityClient, region=self.home_region)

    @functools.cached_property
    def search(self):
        return self._client(oci.resource_search.ResourceSearchClient, region=self.config.region)

    @functools.cached_property
    def quotas(self):
        return self._client(oci.limits.QuotasClient, region=self.home_region)

    @functools.cached_property
    def domains_client(self):
        """SCIM client bound to the identity domain named in lab.toml."""
        conf = dict(self.oci_config)
        return oci.identity_domains.IdentityDomainsClient(
            conf, service_endpoint=self.domain_endpoint
        )

    # -- resolution ------------------------------------------------------

    @functools.cached_property
    def domain(self):
        name = self.config.domain
        domains = oci.pagination.list_call_get_all_results(
            self.identity.list_domains, self.tenancy_id
        ).data
        for dom in domains:
            if dom.display_name == name:
                return dom
        available = ", ".join(d.display_name for d in domains) or "(none)"
        raise AuthError(
            f"identity domain '{name}' not found in this tenancy. Available: {available}"
        )

    @functools.cached_property
    def domain_endpoint(self) -> str:
        return self.domain.url

    def find_compartment(self, name: str, parent_id: str | None = None):
        """Find an ACTIVE compartment by name directly under parent (default: root)."""
        parent_id = parent_id or self.tenancy_id
        results = oci.pagination.list_call_get_all_results(
            self.identity.list_compartments,
            parent_id,
            lifecycle_state="ACTIVE",
        ).data
        for comp in results:
            if comp.name == name:
                return comp
        raise NotFound(f"compartment '{name}' does not exist under {parent_id}")

    @functools.cached_property
    def lab_compartment(self):
        """The one compartment labctl is allowed to touch.

        Guard rail: this resolves only a named, non-root compartment. Every
        destructive operation routes through here, so labctl structurally cannot
        be aimed at the tenancy root.
        """
        comp = self.find_compartment(self.config.compartment)
        if comp.id == self.tenancy_id:
            raise AuthError("refusing to operate on the tenancy root compartment")
        return comp

    def find_group(self, name: str):
        results = oci.pagination.list_call_get_all_results(
            self.identity.list_groups, self.tenancy_id, name=name
        ).data
        for group in results:
            if group.name == name:
                return group
        raise NotFound(f"group '{name}' does not exist")

    def find_policy(self, name: str, compartment_id: str | None = None):
        compartment_id = compartment_id or self.tenancy_id
        results = oci.pagination.list_call_get_all_results(
            self.identity.list_policies, compartment_id
        ).data
        for policy in results:
            if policy.name == name:
                return policy
        raise NotFound(f"policy '{name}' does not exist in {compartment_id}")

    # -- service clients used by `nuke` ----------------------------------

    def _regional(self, cls):
        return self._client(cls, region=self.config.region)

    @functools.cached_property
    def compute(self):
        return self._regional(oci.core.ComputeClient)

    @functools.cached_property
    def compute_management(self):
        return self._regional(oci.core.ComputeManagementClient)

    @functools.cached_property
    def block_storage(self):
        return self._regional(oci.core.BlockstorageClient)

    @functools.cached_property
    def network(self):
        return self._regional(oci.core.VirtualNetworkClient)

    @functools.cached_property
    def oke(self):
        return self._regional(oci.container_engine.ContainerEngineClient)

    @functools.cached_property
    def load_balancer(self):
        return self._regional(oci.load_balancer.LoadBalancerClient)

    @functools.cached_property
    def network_load_balancer(self):
        return self._regional(oci.network_load_balancer.NetworkLoadBalancerClient)

    @functools.cached_property
    def file_storage(self):
        return self._regional(oci.file_storage.FileStorageClient)

    @functools.cached_property
    def object_storage(self):
        return self._regional(oci.object_storage.ObjectStorageClient)

    @functools.cached_property
    def object_namespace(self) -> str:
        return self.object_storage.get_namespace().data

    @functools.cached_property
    def artifacts(self):
        return self._regional(oci.artifacts.ArtifactsClient)

    @functools.cached_property
    def vaults(self):
        return self._regional(oci.key_management.KmsVaultClient)

    @functools.cached_property
    def bastion(self):
        return self._regional(oci.bastion.BastionClient)

    @functools.cached_property
    def database(self):
        return self._regional(oci.database.DatabaseClient)

    @functools.cached_property
    def mysql(self):
        return self._regional(oci.mysql.DbSystemClient)

    @functools.cached_property
    def functions_management(self):
        return self._regional(oci.functions.FunctionsManagementClient)

    @functools.cached_property
    def api_gateway(self):
        return self._regional(oci.apigateway.GatewayClient)

    @functools.cached_property
    def streaming_admin(self):
        return self._regional(oci.streaming.StreamAdminClient)

    @functools.cached_property
    def notifications(self):
        return self._regional(oci.ons.NotificationControlPlaneClient)

    def descendant_compartments(self) -> list:
        """Every ACTIVE compartment under the lab compartment, deepest first.

        Participants can create sub-compartments, so `nuke` has to sweep the
        whole subtree, and must delete children before parents.
        """
        root = self.lab_compartment
        found: list = []
        frontier = [root.id]
        while frontier:
            parent = frontier.pop()
            children = oci.pagination.list_call_get_all_results(
                self.identity.list_compartments, parent, lifecycle_state="ACTIVE"
            ).data
            for child in children:
                found.append(child)
                frontier.append(child.id)
        # Deepest first: a compartment can only be deleted once it has no children.
        found.sort(key=lambda c: c.compartment_id != root.id)
        return found

    @functools.cached_property
    def dns(self):
        return self._regional(oci.dns.DnsClient)
