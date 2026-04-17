import contextlib
import subprocess
import sys
import threading
from pathlib import Path

import click


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def get_swan_base_dir():
    cwd = Path.cwd()
    if cwd.name == "swan":
        return cwd
    if cwd.parent.name == "swan":
        return cwd.parent
    msg = f"Could not find swan base directory from {cwd}"
    raise RuntimeError(msg)


def is_python_package(path: Path) -> bool:
    return (path / "pyproject.toml").exists()


def iter_python_packages(path: Path):
    for item in path.iterdir():
        if item.is_dir() and is_python_package(item):
            yield item


def get_wheel_path(path: Path) -> Path:
    dist = path / "dist"
    for item in dist.glob("*.whl"):
        return item

    msg = f"No wheel found in {dist} for {path}"
    raise RuntimeError(msg)


def is_verbose() -> bool:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return False
    return ctx.params.get("verbose", False)


@contextlib.contextmanager
def spinner(message: str, *, indent: int = 0):
    padding = " " * indent
    if is_verbose() or not sys.stderr.isatty():
        click.secho(f"  {padding}{message}", fg="cyan")
        yield
        return

    stop = threading.Event()
    frame_idx = 0

    def spin():
        nonlocal frame_idx
        while not stop.is_set():
            frame = SPINNER_FRAMES[frame_idx % len(SPINNER_FRAMES)]
            click.echo(f"\r  {padding}{click.style(frame, fg='cyan')} {message}", nl=False)
            frame_idx += 1
            stop.wait(0.08)

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
        click.echo(f"\r  {padding}{click.style('✓', fg='green')} {message}")


def run_command(cmd: list[str], cwd: Path, input: bytes | None = None):
    verbose = is_verbose()
    if verbose:
        click.secho(f"  $ {' '.join(cmd)}", fg="bright_black")
    output = None if verbose else subprocess.DEVNULL
    subprocess.run(cmd, cwd=cwd, check=True, input=input, stdout=output, stderr=output)


def run_uv(cwd: Path, *args):
    run_command(["uv", *args], cwd)


def uv_build(path: Path):
    run_uv(path, "build", "--clear")
