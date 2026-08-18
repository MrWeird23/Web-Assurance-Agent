from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    ValidationError,
)
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node
from yaml.resolver import BaseResolver

from triage_agent.security import UnsafeTargetError, validate_probe_url

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=500),
]
ShortcodeName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
MAX_MANIFEST_BYTES = 1024 * 1024
_FORBIDDEN_GLOB_CHARACTERS = frozenset(r"\(){}+|^$")


class StrictSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Node | None, index: int) -> Node:
        if self.check_event(AliasEvent):  # type: ignore[no-untyped-call]
            raise ConstructorError(None, None, "YAML aliases are not allowed", None)
        return cast(Node, super().compose_node(parent, index))


def _construct_unique_mapping(
    loader: StrictSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictSafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ViewportManifest(StrictManifestModel):
    id: Identifier
    width: StrictInt = Field(ge=1, le=7680)
    height: StrictInt = Field(ge=1, le=7680)
    device_scale_factor: StrictFloat = Field(ge=0.25, le=4.0)


FillValue = Annotated[str, StringConstraints(strict=True, max_length=500)]


class InteractionManifest(StrictManifestModel):
    action: Literal["click", "fill"]
    selector: NonEmptyText
    value: FillValue | None = None
    enabled: StrictBool = False


PluginAssertionKind = Literal[
    "elementor",
    "contact-form-7",
    "woocommerce",
    "gallery-slider",
    "search",
    "multilingual",
]


class PluginAssertionManifest(StrictManifestModel):
    id: Identifier
    kind: PluginAssertionKind
    required_selectors: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=20)


class PageManifest(StrictManifestModel):
    id: Identifier
    url: NonEmptyText
    viewports: tuple[ViewportManifest, ...] = Field(min_length=1, max_length=10)
    ready_selector: NonEmptyText | None = None
    required_text: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    required_selectors: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    forbidden_text: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    critical_resource_patterns: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    ignored_resource_patterns: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    screenshot_masks: tuple[NonEmptyText, ...] = Field(default=(), max_length=100)
    application_shortcodes: tuple[ShortcodeName, ...] = Field(default=(), max_length=20)
    plugin_assertions: tuple[PluginAssertionManifest, ...] = Field(default=(), max_length=20)
    interactions: tuple[InteractionManifest, ...] = Field(default=(), max_length=20)


class SiteManifest(StrictManifestModel):
    id: Identifier
    allowed_hosts: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=100)
    pages: tuple[PageManifest, ...] = Field(min_length=1, max_length=100)


class SiteManifestFile(StrictManifestModel):
    version: StrictInt = Field(ge=1, le=1)
    sites: tuple[SiteManifest, ...] = Field(min_length=1, max_length=100)


@dataclass(frozen=True, slots=True)
class ManifestRegistry:
    manifest: SiteManifestFile
    _pages: Mapping[str, PageManifest]
    _allowed_hosts: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_pages", MappingProxyType(dict(self._pages)))
        object.__setattr__(self, "_allowed_hosts", MappingProxyType(dict(self._allowed_hosts)))

    def page(self, page_id: str) -> PageManifest:
        return self._pages[page_id]

    def allowed_hosts(self, page_id: str) -> frozenset[str]:
        return self._allowed_hosts[page_id]


def load_site_manifest(path: Path) -> ManifestRegistry:
    try:
        with path.open("rb") as manifest_file:
            raw = manifest_file.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ValueError("Unable to read site manifest") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Site manifest is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Unable to read site manifest") from exc
    return parse_site_manifest(text)


def parse_site_manifest(text: str) -> ManifestRegistry:
    if not isinstance(text, str) or len(text) > MAX_MANIFEST_BYTES:
        raise ValueError("Site manifest is too large")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid site manifest") from exc
    if encoded_size > MAX_MANIFEST_BYTES:
        raise ValueError("Site manifest is too large")

    loader = StrictSafeLoader(text)
    try:
        raw = loader.get_single_data()
        manifest = SiteManifestFile.model_validate(raw)
    except (TypeError, ValidationError, yaml.YAMLError) as exc:
        raise ValueError("Invalid site manifest") from exc
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]

    site_ids = [site.id for site in manifest.sites]
    if len(site_ids) != len(set(site_ids)):
        raise ValueError("Invalid site manifest: duplicate site ID")

    try:
        for site in manifest.sites:
            allowed_hosts = set(site.allowed_hosts)
            for page in site.pages:
                validate_probe_url(page.url, allowed_hosts=allowed_hosts)
    except UnsafeTargetError as exc:
        raise ValueError("Invalid site manifest: unsafe page URL") from exc

    page_list = [page for site in manifest.sites for page in site.pages]
    for page in page_list:
        for pattern in (
            *page.critical_resource_patterns,
            *page.ignored_resource_patterns,
        ):
            if any(character in _FORBIDDEN_GLOB_CHARACTERS for character in pattern):
                raise ValueError("Invalid site manifest: invalid resource glob")

    for page in page_list:
        viewport_ids = [viewport.id for viewport in page.viewports]
        if len(viewport_ids) != len(set(viewport_ids)):
            raise ValueError("Invalid site manifest: duplicate viewport ID")

    for page in page_list:
        plugin_assertion_ids = [assertion.id for assertion in page.plugin_assertions]
        if len(plugin_assertion_ids) != len(set(plugin_assertion_ids)):
            raise ValueError("Invalid site manifest: duplicate plugin assertion ID")

    for page in page_list:
        for interaction in page.interactions:
            if interaction.action == "fill" and interaction.value is None:
                raise ValueError("Invalid site manifest: fill interaction requires a value")
            if interaction.action == "click" and interaction.value is not None:
                raise ValueError(
                    "Invalid site manifest: click interaction must not declare a value"
                )

    page_ids = [page.id for page in page_list]
    if len(page_ids) != len(set(page_ids)):
        raise ValueError("Invalid site manifest: duplicate page ID")

    pages = {page.id: page for page in page_list}
    page_allowed_hosts = {
        page.id: frozenset(site.allowed_hosts) for site in manifest.sites for page in site.pages
    }
    return ManifestRegistry(manifest=manifest, _pages=pages, _allowed_hosts=page_allowed_hosts)
