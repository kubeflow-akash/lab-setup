"""Loads lab.toml and exposes it as a typed object."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Quotas:
    enabled: bool
    allowed_compute: tuple[str, ...]
    standard_ocpus: int
    allowed_gpu: tuple[str, ...]
    gpu_count: int
    block_storage_gb: int
    clusters: int


@dataclass(frozen=True)
class Config:
    compartment: str
    group: str
    domain: str
    region: str
    profile: str
    block_policy_management: bool
    restrict_to_region: bool
    quotas: Quotas
    tags_enabled: bool
    tag_namespace: str
    source: Path

    @property
    def policy_name(self) -> str:
        return f"{self.compartment}-access-policy"

    @property
    def quota_name(self) -> str:
        return f"{self.compartment}-quotas"


DEFAULT_FILENAME = "lab.toml"


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    # Walk up from cwd so labctl works from anywhere inside the repo.
    for directory in [Path.cwd(), *Path.cwd().parents]:
        candidate = directory / DEFAULT_FILENAME
        if candidate.is_file():
            return candidate

    raise ConfigError(
        f"no {DEFAULT_FILENAME} found in the current directory or any parent. "
        f"Run labctl from the repo, or pass --config."
    )


def load(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    lab = raw.get("lab", {})
    policy = raw.get("policy", {})
    quotas = raw.get("quotas", {})
    tags = raw.get("tags", {})

    for key in ("compartment", "group", "domain", "region"):
        if not lab.get(key):
            raise ConfigError(f"{path}: [lab] is missing required key '{key}'")

    compartment = lab["compartment"].strip()
    if compartment in ("", "/", "root", "tenancy"):
        raise ConfigError(
            f"{path}: [lab] compartment must be a real compartment name, not the tenancy root. "
            f"labctl refuses to manage the root compartment."
        )

    return Config(
        compartment=compartment,
        group=lab["group"].strip(),
        domain=lab["domain"].strip(),
        region=lab["region"].strip(),
        profile=lab.get("profile", "DEFAULT").strip(),
        block_policy_management=bool(policy.get("block_policy_management", True)),
        restrict_to_region=bool(policy.get("restrict_to_region", True)),
        quotas=Quotas(
            enabled=bool(quotas.get("enabled", True)),
            allowed_compute=tuple(quotas.get("allowed_compute", ())),
            standard_ocpus=int(quotas.get("standard_ocpus", 32)),
            allowed_gpu=tuple(quotas.get("allowed_gpu", ())),
            gpu_count=int(quotas.get("gpu_count", 0)),
            block_storage_gb=int(quotas.get("block_storage_gb", 2000)),
            clusters=int(quotas.get("clusters", 5)),
        ),
        tags_enabled=bool(tags.get("enabled", True)),
        tag_namespace=tags.get("namespace", "lab").strip(),
        source=path,
    )
