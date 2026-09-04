"""Render every accepted configuration key from the Pydantic schema."""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from orchestrator.config.schema import Command, Config, UserPath


@dataclass
class Entry:
    path: str
    type: str
    required: bool
    default: str
    description: str
    depth: int


def _type_name(tp: Any) -> str:
    if tp is Command:
        return "command"
    if tp is UserPath:
        return "path"
    origin = get_origin(tp)
    if origin is typing.Annotated:
        return _type_name(get_args(tp)[0])
    if origin is Literal:
        return " | ".join(repr(a) for a in get_args(tp))
    if origin in (Union, types.UnionType):
        return " | ".join(_type_name(a) for a in get_args(tp))
    if origin is list:
        return f"list[{_type_name(get_args(tp)[0])}]" if get_args(tp) else "list"
    if origin is dict:
        k, v = get_args(tp) if get_args(tp) else (str, Any)
        return f"map[{_type_name(k)} -> {_type_name(v)}]"
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return "section"
    if tp is Path:
        return "path"
    if tp is type(None):
        return "null"
    if tp is Any:
        return "any"
    return getattr(tp, "__name__", str(tp))


def _model_of(tp: Any) -> type[BaseModel] | None:
    origin = get_origin(tp)
    if origin is typing.Annotated:
        return _model_of(get_args(tp)[0])
    if origin in (Union, types.UnionType):
        for a in get_args(tp):
            m = _model_of(a)
            if m:
                return m
        return None
    if origin is dict and get_args(tp):
        return _model_of(get_args(tp)[1])
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return tp
    return None


def _default(info: FieldInfo) -> str:
    if info.is_required():
        return ""
    d = info.default
    if d is PydanticUndefined or d is None:
        return "none" if d is None else ""
    if isinstance(d, BaseModel):
        return "(defaults below)"
    return repr(d) if not isinstance(d, str) else d


def entries(model: type[BaseModel] = Config, prefix: str = "", depth: int = 0) -> list[Entry]:
    out: list[Entry] = []
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        tp = info.annotation
        sub = _model_of(tp)
        is_map_of_sections = get_origin(tp) is dict and sub is not None
        type_name = _type_name(tp)
        if is_map_of_sections:
            type_name = "map[name -> section]"
        out.append(
            Entry(
                path=path,
                type=type_name,
                required=info.is_required(),
                default=_default(info),
                description=info.description or (sub.__doc__ or "").strip().splitlines()[0]
                if sub
                else info.description or "",
                depth=depth,
            )
        )
        if sub is not None:
            child_prefix = f"{path}.<name>." if is_map_of_sections else f"{path}."
            out.extend(entries(sub, child_prefix, depth + 1))
    return out


def as_markdown() -> str:
    lines = [
        "# Configuration reference",
        "",
        "Every key the orchestrator accepts. Unknown keys are errors. Paths expand `~`; a `command` is "
        "either one string split like a shell would or a list of arguments. Secrets are never values here: "
        "an `auth` block names an environment variable or a `~/.netrc` machine.",
        "",
        "| Key | Type | Required | Default | Description |",
        "|---|---|---|---|---|",
    ]
    for e in entries():
        lines.append(
            "| `{}` | {} | {} | {} | {} |".format(
                e.path,
                e.type.replace("|", "\\|"),
                "yes" if e.required else "",
                f"`{e.default}`" if e.default else "",
                e.description.replace("|", "\\|"),
            )
        )
    return "\n".join(lines) + "\n"


def as_text() -> list[Entry]:
    return entries()
