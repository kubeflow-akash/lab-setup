"""Empty the lab compartment.

Deliberately does NOT delete the compartment itself. A compartment containing a
KMS vault cannot be deleted for at least 7 days, and keeping the compartment
means its OCID never changes, so the policy, quotas and tag defaults written by
`bootstrap` stay valid across every lab session.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

import oci

from . import deleters
from .auth import Session

# Rows in these states are already gone; re-issuing a delete just produces noise.
GONE_STATES = {"TERMINATED", "DELETED"}
# Rows in these states have an in-flight delete; wait rather than re-issue.
IN_FLIGHT_STATES = {"TERMINATING", "DELETING"}


@dataclass
class Item:
    identifier: str
    resource_type: str
    display_name: str
    compartment_id: str
    lifecycle_state: str

    @property
    def tier(self) -> int:
        return deleters.TIER_INDEX.get(self.resource_type, len(deleters.TIERS))


@dataclass
class Inventory:
    known: list[Item] = field(default_factory=list)
    unknown: list[Item] = field(default_factory=list)
    in_flight: list[Item] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.known) + len(self.unknown) + len(self.in_flight)

    def by_type(self) -> dict[str, list[Item]]:
        grouped: dict[str, list[Item]] = defaultdict(list)
        for item in self.known + self.unknown + self.in_flight:
            grouped[item.resource_type].append(item)
        return dict(sorted(grouped.items()))


@dataclass
class Outcome:
    item: Item
    status: str  # "deleted" | "skipped" | "failed"
    detail: str = ""


def scan(session: Session) -> tuple[Inventory, list]:
    """Inventory the lab compartment and every sub-compartment under it."""
    compartments = [session.lab_compartment, *session.descendant_compartments()]
    inventory = Inventory()

    for comp in compartments:
        query = f"query all resources where compartmentId = '{comp.id}'"
        details = oci.resource_search.models.StructuredSearchDetails(
            query=query, matching_context_type="NONE"
        )
        try:
            results = oci.pagination.list_call_get_all_results(
                session.search.search_resources, details
            ).data
        except oci.exceptions.ServiceError as exc:
            raise RuntimeError(f"resource search failed for {comp.name}: {exc.message}") from exc

        for row in results:
            state = (row.lifecycle_state or "").upper()
            if state in GONE_STATES:
                continue
            if row.resource_type in deleters.IGNORED_TYPES:
                continue
            item = Item(
                identifier=row.identifier,
                resource_type=row.resource_type,
                display_name=row.display_name or row.identifier,
                compartment_id=row.compartment_id,
                lifecycle_state=state,
            )
            if state in IN_FLIGHT_STATES:
                inventory.in_flight.append(item)
            elif row.resource_type in deleters.BY_TYPE:
                inventory.known.append(item)
            else:
                inventory.unknown.append(item)

    return inventory, compartments


def sweep_once(session: Session, inventory: Inventory, compartments: list) -> list[Outcome]:
    """One tier-ordered pass. Failures are recorded, not raised -- a resource
    that is not ready yet usually succeeds on the next round."""
    ctx = deleters.build_context(session, [c.id for c in compartments])
    outcomes: list[Outcome] = []

    for tier_index, (_, types) in enumerate(deleters.TIERS):
        batch = [i for i in inventory.known if i.tier == tier_index]
        if not batch:
            continue
        for item in batch:
            fn = deleters.BY_TYPE[item.resource_type]
            try:
                fn(ctx, item)
                outcomes.append(Outcome(item, "deleted"))
            except deleters.SkipResource as exc:
                outcomes.append(Outcome(item, "skipped", str(exc)))
            except oci.exceptions.ServiceError as exc:
                if exc.status == 404:
                    outcomes.append(Outcome(item, "deleted", "already gone"))
                else:
                    outcomes.append(Outcome(item, "failed", f"{exc.status} {exc.code}: {exc.message}"))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(Outcome(item, "failed", str(exc)))
    return outcomes


def delete_subcompartments(session: Session, apply: bool) -> list[Outcome]:
    """Remove sub-compartments participants created. The lab compartment itself
    is always kept."""
    outcomes: list[Outcome] = []
    for comp in session.descendant_compartments():
        item = Item(comp.id, "Compartment", comp.name, comp.compartment_id, "ACTIVE")
        if not apply:
            outcomes.append(Outcome(item, "skipped", "dry run"))
            continue
        try:
            session.identity.delete_compartment(comp.id)
            outcomes.append(Outcome(item, "deleted", "deletion is async, may take minutes"))
        except oci.exceptions.ServiceError as exc:
            outcomes.append(Outcome(item, "failed", f"{exc.status} {exc.code}: {exc.message}"))
    return outcomes


def run(
    session: Session,
    apply: bool,
    max_rounds: int = 8,
    settle_seconds: int = 20,
    progress=None,
) -> tuple[Inventory, list[Outcome]]:
    """Sweep until the compartment is empty or a round makes no progress.

    Repeats because OCI deletes are asynchronous and the resource-search index
    lags behind them by seconds to minutes -- a single pass reliably leaves
    resources behind.
    """
    inventory, compartments = scan(session)
    if not apply:
        return inventory, []

    all_outcomes: list[Outcome] = []
    previous_total = None

    for round_number in range(1, max_rounds + 1):
        if inventory.total == 0:
            break
        if progress:
            progress(round_number, inventory)

        outcomes = sweep_once(session, inventory, compartments)
        all_outcomes.extend(outcomes)

        time.sleep(settle_seconds)
        inventory, compartments = scan(session)

        remaining = len(inventory.known)
        if previous_total is not None and remaining >= previous_total and remaining > 0:
            # Nothing moved this round; further rounds will not help.
            break
        previous_total = remaining

    all_outcomes.extend(delete_subcompartments(session, apply))
    inventory, _ = scan(session)
    return inventory, all_outcomes


def verify_registry(session: Session) -> tuple[list[str], list[str]]:
    """Check every registered resource type against the live searchable-type list.

    A typo in a type key is silent and dangerous: the deleter simply never fires
    and the resource is left running. `labctl doctor` runs this.
    """
    real = {t.name for t in oci.pagination.list_call_get_all_results(
        session.search.list_resource_types
    ).data}
    bogus = sorted(set(deleters.BY_TYPE) - real)
    ignored_bogus = sorted(deleters.IGNORED_TYPES - real)
    return bogus, ignored_bogus
