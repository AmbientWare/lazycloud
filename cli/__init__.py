import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def machine():
    """CLI tool for managing machines"""
    pass


# Import modules to register their commands
import cli.machines
import cli.keys
import cli.volumes

__all__ = ["machine"]
