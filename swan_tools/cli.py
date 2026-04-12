import click

from swan_tools.config import HUB_IMAGE, USER_IMAGE
from swan_tools.docker import RebuildFrom, build_and_push_hub_image, rebuild_user_image
from swan_tools.status import run_dashboard


class RebuildFromType(click.ParamType):
    name = "rebuild_from"

    def get_metavar(self, param, ctx):  # noqa: ARG002
        choices = [m.name.lower().replace("_", "-") for m in RebuildFrom]
        return "[" + "|".join(choices) + "]"

    def convert(self, value, param, ctx):
        if isinstance(value, RebuildFrom):
            return value
        try:
            return RebuildFrom.from_string(value)
        except ValueError:
            self.fail(f"Invalid value: {value}. {self.get_metavar(param, ctx)}", param, ctx)


@click.group()
def cli():
    pass


@cli.group()
def build():
    pass


def _print_header(title: str, **fields: object):
    click.echo()
    click.secho(f"  {title}", fg="magenta", bold=True)
    for label, value in fields.items():
        click.secho(f"  {label}: {value}", fg="magenta")
    click.echo()


@build.command()
@click.option(
    "--image",
    default=HUB_IMAGE,
    help="Image name to build",
)
@click.option("--push/--no-push", is_flag=True, help="Push the built image to the registry", default=True)
@click.option("-v", "--verbose", is_flag=True, help="Show command output")
def hub(image: str, *, push: bool, verbose: bool):
    _print_header("Building hub image", Image=image, Push=push)
    build_and_push_hub_image(image, push=push)


@build.command()
@click.option(
    "--rebuild-from",
    help="Base image to rebuild from",
    type=RebuildFromType(),
    default=RebuildFrom.NONE,
)
@click.option(
    "--image",
    default=USER_IMAGE,
    help="Image name to build",
)
@click.option(
    "--packages",
    help="Comma-separated list of packages to build and include in the image",
    default="",
)
@click.option("--push/--no-push", is_flag=True, help="Push the built image to the registry", default=True)
@click.option("-v", "--verbose", is_flag=True, help="Show command output")
def user(rebuild_from: RebuildFrom, image: str, packages: str, *, push: bool, verbose: bool):
    packages_list = [pkg.strip().lower() for pkg in packages.split(",")] if packages else []
    _print_header(
        "Building user image",
        **{"Rebuild from": rebuild_from.name},
        Image=image,
        Packages=", ".join(packages_list) or "(all)",
        Push=push,
    )
    rebuild_user_image(rebuild_from, image, packages_list, push=push)


@cli.command()
def status():
    """Launch the SWAN cluster health dashboard (TUI)."""
    run_dashboard()
