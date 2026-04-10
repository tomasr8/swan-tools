from __future__ import annotations

import enum
import re
from pathlib import Path
from typing import Literal

import click

from swan_tools.utils import (
    get_swan_base_dir,
    get_wheel_path,
    is_verbose,
    iter_python_packages,
    run_command,
    spinner,
    uv_build,
)


DOCKERFILE_TEMPLATE = """
FROM {base_image}

{wheels_to_add}

RUN pip3 uninstall -y {extensions}
RUN pip3 install --no-cache-dir {wheels}
"""


class RebuildFrom(enum.IntEnum):
    NONE = 0
    SWAN_CERN = 1
    SWAN = 2
    BASE = 3

    @staticmethod
    def from_string(value: str) -> RebuildFrom:
        value = value.strip().replace("-", "_").upper()
        for member in RebuildFrom:
            if member.name == value:
                return member
        msg = f"Invalid RebuildFrom value: {value}"
        raise ValueError(msg)


def collect_wheels(extensions_dir: Path, packages: list[str] | None = None) -> list[Path]:
    return [
        get_wheel_path(path)
        for path in iter_python_packages(extensions_dir)
        if not packages or path.name.lower() in packages
    ]


def generate_dockerfile(base_dir: Path, base_image: str, wheels: list[Path]) -> str:
    wheels_to_add = [f"COPY {wheel.relative_to(base_dir)} /wheels/" for wheel in wheels]
    extensions = " ".join(w.stem.split("-")[0] for w in wheels)
    dockerfile = DOCKERFILE_TEMPLATE.format(
        base_image=base_image,
        wheels_to_add="\n".join(wheels_to_add),
        extensions=extensions,
        wheels=" ".join(f"/wheels/{wheel.name}" for wheel in wheels),
    )

    if is_verbose():
        click.secho("Generated Dockerfile content:", fg="bright_black")
        click.echo(dockerfile)

    return dockerfile


def build_docker_image(image: str, *, context_dir: str = ".", cwd: Path | None = None, dockerfile: str | None = None):
    docker_args = [
        "docker",
        "build",
        "-t",
        image,
    ]
    if dockerfile:
        docker_args.extend(["-f", "-"])
    docker_args.append(context_dir)

    process_args = {}
    if cwd:
        process_args["cwd"] = cwd
    if dockerfile:
        process_args["input"] = dockerfile.encode("utf-8")

    run_command(
        docker_args,
        **process_args,
    )


def build_extensions_image(
    base_dir: Path,
    extensions_dir: Path,
    base_image: str,
    image_name: str,
    packages: list[str] | None = None,
):
    wheels = collect_wheels(extensions_dir, packages)
    dockerfile = generate_dockerfile(base_dir, base_image, wheels)
    build_docker_image(image_name, cwd=base_dir, dockerfile=dockerfile)


def push_docker_image(image_name: str):
    run_command(["docker", "push", image_name], cwd=Path.cwd())


def build_and_push_hub_image(image: str, *, push: bool):
    base_dir = get_swan_base_dir()

    extensions_dir = base_dir / "jupyterhub-extensions"
    click.secho("  Building hub extensions", fg="cyan")
    for path in iter_python_packages(extensions_dir):
        with spinner(path.name, indent=2):
            uv_build(path)

    base_image = "jupyterhub-image:latest"
    with spinner("Building base hub image"):
        build_docker_image(base_image, cwd=base_dir / "jupyterhub-image")
    with spinner("Building final hub image with extensions"):
        build_extensions_image(base_dir, extensions_dir, base_image, image)

    if push:
        with spinner(f"Pushing {image}"):
            push_docker_image(image)

    click.secho("  Done!", fg="green", bold=True)


def _intermediate_image(name: str) -> str:
    return f"jupyter-images/{name}:latest"


def build_swan_layer(
    base_dir: Path,
    base_image: str,
    image_name: str,
    image_type: Literal["swan", "swan-cern"] = "swan",
):
    image_dir = base_dir / "jupyter-images"
    dockerfile = (image_dir / image_type / "Dockerfile").read_text(encoding="utf-8")
    dockerfile = re.sub(r"ARG VERSION_PARENT=(.+)", "ARG VERSION_PARENT=latest", dockerfile)
    dockerfile = re.sub(r"FROM (.+)", f"FROM {base_image}", dockerfile)

    build_docker_image(
        image_name,
        context_dir=image_type,
        cwd=image_dir,
        dockerfile=dockerfile,
    )


def rebuild_user_image(rebuild_from: RebuildFrom, image: str, packages: list[str], *, push: bool):
    base_dir = get_swan_base_dir()

    if rebuild_from >= RebuildFrom.BASE:
        with spinner("Building base user image"):
            build_docker_image(_intermediate_image("base"), context_dir="base", cwd=base_dir / "jupyter-images")
    if rebuild_from >= RebuildFrom.SWAN:
        with spinner("Building swan layer"):
            build_swan_layer(base_dir, _intermediate_image("base"), _intermediate_image("swan"))
    if rebuild_from >= RebuildFrom.SWAN_CERN:
        with spinner("Building swan-cern layer"):
            build_swan_layer(
                base_dir,
                _intermediate_image("swan"),
                _intermediate_image("swan-cern"),
                image_type="swan-cern",
            )

    click.secho("  Building user extensions", fg="cyan")
    extensions_dir = base_dir / "jupyter-extensions"
    for path in iter_python_packages(extensions_dir):
        if not packages or path.name.lower() in packages:
            with spinner(path.name, indent=2):
                uv_build(path)

    with spinner("Building final user image with extensions"):
        build_extensions_image(base_dir, extensions_dir, _intermediate_image("swan-cern"), image, packages or None)

    if push:
        with spinner(f"Pushing {image}"):
            push_docker_image(image)

    click.secho("  Done!", fg="green", bold=True)
