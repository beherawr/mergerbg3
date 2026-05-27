"""Parser/writer for the two ``meta.lsx`` flavors a Toolkit project contains.

A BG3 Toolkit project has *two* meta.lsx files with different schemas:

1. ``Mods/<ModFolder>/meta.lsx``: the *mod identity* file that ships in
   the packed .pak. Defines who this mod is, what it depends on, what it
   conflicts with, and any Script Extender scripts registered.
2. ``Projects/<ModFolder>/meta.lsx``: the *Toolkit project identity* file.
   Points at the mod by UUID, has its own UUID for the project itself.
   Used only by the Toolkit at edit time.

The merger has to combine the first kind (dependency union, SE script
union) and regenerate the second kind with a fresh project UUID.

This module sits on top of ``core.lsx``: it doesn't re-parse XML; it walks
the LsxDocument tree from that module and pulls out the meta-specific data
into a typed model. Writing goes the other direction: build an LsxDocument
and let ``core.lsx`` serialize it.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import lsx


# --- Mod identity (Mods/<Folder>/meta.lsx) -------------------------------


@dataclass
class Dependency:
    """One ``<node id="ModuleShortDesc">`` entry under Dependencies.

    The MD5 and Version64 fields are Larian's internal cache-tracking;
    we preserve whatever was on disk and union by UUID.
    """
    folder: str
    name: str
    uuid: str
    md5: str = ""
    version64: str = "0"
    publish_handle: str = "0"


@dataclass
class ScriptParameter:
    """One ``<node id="Parameter">`` under a Script. Toolkit-defined K/V."""
    map_key: str
    type: str    # int as string
    value: str


@dataclass
class ScriptRegistration:
    """One ``<node id="Script">`` under ModuleInfo's Scripts.

    Script Extender uses these to know which Lua scripts to load and
    expose their toggleable parameters in the BG3 mod manager UI.
    """
    uuid: str
    parameters: list[ScriptParameter] = field(default_factory=list)


@dataclass
class ModMeta:
    """Parsed contents of ``Mods/<ModFolder>/meta.lsx``.

    Designed to be regenerated from scratch when writing: we don't try
    to preserve byte-exact source structure for this file; the Toolkit
    rewrites it anyway whenever the user saves.
    """
    # ModuleInfo attributes
    uuid: str
    folder: str
    name: str
    author: str = ""
    description: str = ""
    version64: str = "36028797018963968"  # default = 1.0.0.0 packed
    publish_handle: str = "0"
    publish_version: str = "0"
    # Optional level-name attributes: usually empty strings
    character_creation_level_name: str = ""
    lobby_level_name: str = ""
    main_menu_background: str = ""
    menu_level_name: str = ""
    photo_booth: str = ""
    startup_level_name: str = ""
    # Larian engine attributes
    num_players: str = "4"
    file_size: str = "0"
    md5: str = ""
    # Lists
    dependencies: list[Dependency] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # UUIDs of conflicting mods
    scripts: list[ScriptRegistration] = field(default_factory=list)
    # The <version> element of the surrounding LSX wrapper.
    # Kept so re-saving doesn't downgrade a file from 4.8 to 4.0.
    lsx_version: lsx.Version = field(
        default_factory=lambda: lsx.Version("4", "8", "0", "500")
    )


def parse_mod_meta(doc: lsx.LsxDocument) -> ModMeta:
    """Pull ModMeta out of a parsed LSX document (Mods/<Folder>/meta.lsx).

    Raises if the document doesn't have the expected ``Config / root /
    ModuleInfo`` structure: that's a strong indicator we got handed a
    Projects/-style meta.lsx by mistake, and we want the caller to know.
    """
    config = doc.region("Config")
    if config is None:
        raise ValueError(
            "Mods/<Folder>/meta.lsx must have a <region id='Config'>"
        )
    root = config.root_node
    if root.id != "root":
        raise ValueError(
            f"expected Config region's root node to be id='root', got {root.id!r}"
        )

    # Find the ModuleInfo child.
    module_info: lsx.Node | None = None
    deps_node: lsx.Node | None = None
    conflicts_node: lsx.Node | None = None
    for child in root.children:
        if child.id == "ModuleInfo":
            module_info = child
        elif child.id == "Dependencies":
            deps_node = child
        elif child.id == "Conflicts":
            conflicts_node = child

    if module_info is None:
        raise ValueError("meta.lsx missing <node id='ModuleInfo'>")

    def attr(name: str, default: str = "") -> str:
        return module_info.attr_value(name) or default

    meta = ModMeta(
        uuid=attr("UUID"),
        folder=attr("Folder"),
        name=attr("Name"),
        author=attr("Author"),
        description=attr("Description"),
        version64=attr("Version64", "36028797018963968"),
        publish_handle=attr("PublishHandle", "0"),
        character_creation_level_name=attr("CharacterCreationLevelName"),
        lobby_level_name=attr("LobbyLevelName"),
        main_menu_background=attr("MainMenuBackground"),
        menu_level_name=attr("MenuLevelName"),
        photo_booth=attr("PhotoBooth"),
        startup_level_name=attr("StartupLevelName"),
        num_players=attr("NumPlayers", "4"),
        file_size=attr("FileSize", "0"),
        md5=attr("MD5"),
        lsx_version=doc.version,
    )

    # Pull PublishVersion (child of ModuleInfo).
    for grandchild in module_info.children:
        if grandchild.id == "PublishVersion":
            meta.publish_version = grandchild.attr_value("Version64", "0")
        elif grandchild.id == "Scripts":
            # Scripts container; iterate its Script children.
            for script_node in grandchild.children_by_id("Script"):
                script = ScriptRegistration(uuid=script_node.attr_value("UUID") or "")
                for params_node in script_node.children_by_id("Parameters"):
                    for param_node in params_node.children_by_id("Parameter"):
                        script.parameters.append(ScriptParameter(
                            map_key=param_node.attr_value("MapKey") or "",
                            type=param_node.attr_value("Type") or "1",
                            value=param_node.attr_value("Value") or "",
                        ))
                meta.scripts.append(script)

    # Pull Dependencies.
    if deps_node is not None:
        for short_desc in deps_node.children_by_id("ModuleShortDesc"):
            meta.dependencies.append(Dependency(
                folder=short_desc.attr_value("Folder") or "",
                name=short_desc.attr_value("Name") or "",
                uuid=short_desc.attr_value("UUID") or "",
                md5=short_desc.attr_value("MD5") or "",
                version64=short_desc.attr_value("Version64") or "0",
                publish_handle=short_desc.attr_value("PublishHandle") or "0",
            ))

    # Pull Conflicts.
    if conflicts_node is not None:
        for desc in conflicts_node.children_by_id("ModuleShortDesc"):
            u = desc.attr_value("UUID")
            if u:
                meta.conflicts.append(u)

    return meta


def parse_mod_meta_file(path: Path | str) -> ModMeta:
    return parse_mod_meta(lsx.parse_file(path))


def build_mod_meta_doc(meta: ModMeta) -> lsx.LsxDocument:
    """Build an LsxDocument representing this ModMeta, ready to serialize.

    We write the full schema even when fields are empty strings, because
    the Toolkit also writes empty attributes and we want our output to look
    identical to a Toolkit-generated file.
    """
    # Conflicts node.
    conflicts_node = lsx.Node(id="Conflicts")
    for u in meta.conflicts:
        conflicts_node.children.append(lsx.Node(
            id="ModuleShortDesc",
            attributes=[lsx.Attribute("UUID", "guid", value=u)],
        ))

    # Dependencies node.
    deps_node = lsx.Node(id="Dependencies")
    for dep in meta.dependencies:
        deps_node.children.append(lsx.Node(
            id="ModuleShortDesc",
            attributes=[
                lsx.Attribute("Folder", "LSString", value=dep.folder),
                lsx.Attribute("MD5", "LSString", value=dep.md5),
                lsx.Attribute("Name", "LSString", value=dep.name),
                lsx.Attribute("PublishHandle", "uint64", value=dep.publish_handle),
                lsx.Attribute("UUID", "guid", value=dep.uuid),
                lsx.Attribute("Version64", "int64", value=dep.version64),
            ],
        ))

    # ModuleInfo node.
    module_info = lsx.Node(
        id="ModuleInfo",
        attributes=[
            lsx.Attribute("Author", "LSWString", value=meta.author),
            lsx.Attribute("CharacterCreationLevelName", "FixedString",
                          value=meta.character_creation_level_name),
            lsx.Attribute("Description", "LSWString", value=meta.description),
            lsx.Attribute("FileSize", "uint64", value=meta.file_size),
            lsx.Attribute("Folder", "LSString", value=meta.folder),
            lsx.Attribute("LobbyLevelName", "FixedString", value=meta.lobby_level_name),
            lsx.Attribute("MD5", "LSString", value=meta.md5),
            lsx.Attribute("MenuLevelName", "FixedString", value=meta.menu_level_name),
            lsx.Attribute("Name", "LSString", value=meta.name),
            lsx.Attribute("NumPlayers", "uint8", value=meta.num_players),
            lsx.Attribute("PhotoBooth", "FixedString", value=meta.photo_booth),
            lsx.Attribute("PublishHandle", "uint64", value=meta.publish_handle),
            lsx.Attribute("StartupLevelName", "FixedString",
                          value=meta.startup_level_name),
            lsx.Attribute("UUID", "FixedString", value=meta.uuid),
            lsx.Attribute("Version64", "int64", value=meta.version64),
        ],
    )

    # PublishVersion + Scripts children of ModuleInfo.
    module_info.children.append(lsx.Node(
        id="PublishVersion",
        attributes=[lsx.Attribute("Version64", "int64", value=meta.publish_version)],
    ))

    scripts_node = lsx.Node(id="Scripts")
    for script in meta.scripts:
        script_node = lsx.Node(
            id="Script",
            attributes=[lsx.Attribute("UUID", "FixedString", value=script.uuid)],
        )
        if script.parameters:
            params_node = lsx.Node(id="Parameters")
            for param in script.parameters:
                params_node.children.append(lsx.Node(
                    id="Parameter",
                    attributes=[
                        lsx.Attribute("MapKey", "FixedString", value=param.map_key),
                        lsx.Attribute("Type", "int32", value=param.type),
                        lsx.Attribute("Value", "LSString", value=param.value),
                    ],
                ))
            script_node.children.append(params_node)
        scripts_node.children.append(script_node)
    module_info.children.append(scripts_node)

    # Top-level structure.
    root = lsx.Node(
        id="root",
        children=[conflicts_node, deps_node, module_info],
    )
    return lsx.LsxDocument(
        version=meta.lsx_version,
        regions=[lsx.Region(id="Config", root_node=root)],
    )


def write_mod_meta_file(meta: ModMeta, path: Path | str) -> None:
    lsx.write_file(build_mod_meta_doc(meta), path)


# --- Project identity (Projects/<Folder>/meta.lsx) -----------------------


@dataclass
class ProjectMeta:
    """Parsed contents of ``Projects/<ModFolder>/meta.lsx``.

    The Toolkit uses this to find and identify projects in its Project
    Selection window. The ``module`` field points back at the mod's UUID,
    forming the link between the two meta.lsx files in a project.
    """
    uuid: str            # the project's own UUID (DISTINCT from the mod UUID)
    module: str          # the mod UUID it represents
    name: str
    game_project: str = ""
    updated_dependencies: str = "true"  # bool, but Larian stores as a string
    lsx_version: lsx.Version = field(
        default_factory=lambda: lsx.Version("4", "8", "0", "500")
    )


def parse_project_meta(doc: lsx.LsxDocument) -> ProjectMeta:
    """Pull ProjectMeta out of a parsed LSX document (Projects/<Folder>/meta.lsx)."""
    metadata = doc.region("MetaData")
    if metadata is None:
        raise ValueError(
            "Projects/<Folder>/meta.lsx must have a <region id='MetaData'>"
        )
    root = metadata.root_node
    if root.id != "root":
        raise ValueError(
            f"expected MetaData region root node id='root', got {root.id!r}"
        )

    return ProjectMeta(
        uuid=root.attr_value("UUID") or "",
        module=root.attr_value("Module") or "",
        name=root.attr_value("Name") or "",
        game_project=root.attr_value("GameProject") or "",
        updated_dependencies=root.attr_value("UpdatedDependencies") or "true",
        lsx_version=doc.version,
    )


def parse_project_meta_file(path: Path | str) -> ProjectMeta:
    return parse_project_meta(lsx.parse_file(path))


def build_project_meta_doc(meta: ProjectMeta) -> lsx.LsxDocument:
    root = lsx.Node(
        id="root",
        attributes=[
            lsx.Attribute("GameProject", "LSString", value=meta.game_project),
            lsx.Attribute("Module", "LSString", value=meta.module),
            lsx.Attribute("Name", "LSString", value=meta.name),
            lsx.Attribute("UUID", "LSString", value=meta.uuid),
            lsx.Attribute("UpdatedDependencies", "bool",
                          value=meta.updated_dependencies),
        ],
        children=[lsx.Node(id="Categories")],
    )
    return lsx.LsxDocument(
        version=meta.lsx_version,
        regions=[lsx.Region(id="MetaData", root_node=root)],
    )


def write_project_meta_file(meta: ProjectMeta, path: Path | str) -> None:
    lsx.write_file(build_project_meta_doc(meta), path)


# --- Identity generation for the merged mod ------------------------------


def generate_uuid() -> str:
    """Mint a fresh RFC 4122 v4 UUID, in the dashed lower-case form Larian
    uses. We don't reuse any input mod's UUID (would create a dup); the
    merged mod is a new identity that depends on the inputs being separate.
    """
    return str(_uuid.uuid4())


def union_dependencies(*deps_lists: list[Dependency]) -> list[Dependency]:
    """Merge dependency lists by UUID, keeping the first occurrence.

    When the same dependency appears in multiple inputs (the common case:
    every modded BG3 project depends on GustavX), we keep the first and
    drop the duplicates. Order from the first input is preserved.
    """
    seen: set[str] = set()
    out: list[Dependency] = []
    for deps in deps_lists:
        for dep in deps:
            if dep.uuid not in seen:
                out.append(dep)
                seen.add(dep.uuid)
    return out


def union_scripts(*scripts_lists: list[ScriptRegistration]) -> list[ScriptRegistration]:
    """Merge Script Extender script registrations by UUID.

    If the same script UUID appears in multiple inputs, the first one wins
    (subsequent registrations of the same script are dropped). Parameters
    on a script are not merged: that would require knowing semantic types,
    and Script Extender treats script registration as atomic.
    """
    seen: set[str] = set()
    out: list[ScriptRegistration] = []
    for scripts in scripts_lists:
        for script in scripts:
            if script.uuid not in seen:
                out.append(script)
                seen.add(script.uuid)
    return out


def union_conflicts(*conflicts_lists: list[str]) -> list[str]:
    """Merge conflict-mod-UUID lists, removing duplicates while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for conflicts in conflicts_lists:
        for u in conflicts:
            if u not in seen:
                out.append(u)
                seen.add(u)
    return out


def merge_mod_meta(
    *metas: ModMeta,
    new_uuid: str,
    new_folder: str,
    new_name: str,
    new_author: str = "",
    new_description: str = "",
) -> ModMeta:
    """Produce a merged ModMeta from N input metas plus a new identity.

    The new identity (uuid, folder, name, author, description) comes from
    the user via the merge wizard. The structural data (dependencies,
    scripts, conflicts) is unioned across all inputs.

    We pick the highest lsx_version build number across inputs so we don't
    write to a stale format.
    """
    if not metas:
        raise ValueError("need at least one input ModMeta to merge")

    # Use the highest input lsx_version build to avoid writing to an older
    # format than any of the inputs.
    highest = metas[0].lsx_version
    for m in metas[1:]:
        if int(m.lsx_version.build) > int(highest.build):
            highest = m.lsx_version

    return ModMeta(
        uuid=new_uuid,
        folder=new_folder,
        name=new_name,
        author=new_author,
        description=new_description,
        # Reset version-tracking fields for a fresh release.
        version64="36028797018963968",
        publish_handle="0",
        publish_version="0",
        # NumPlayers: keep the max across inputs (more conservative for compat).
        num_players=str(max(int(m.num_players) for m in metas)),
        # Empty placeholders: these would only matter for full-campaign mods
        # which we don't currently target.
        character_creation_level_name="",
        lobby_level_name="",
        main_menu_background="",
        menu_level_name="",
        photo_booth="",
        startup_level_name="",
        file_size="0",
        md5="",
        # The actual unions.
        dependencies=union_dependencies(*(m.dependencies for m in metas)),
        conflicts=union_conflicts(*(m.conflicts for m in metas)),
        scripts=union_scripts(*(m.scripts for m in metas)),
        lsx_version=highest,
    )
