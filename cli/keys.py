import click
from cli.utils import make_table_view
from cli.config import config


@click.group()
def keys():
    """API key management commands"""
    pass


@keys.command()
@click.argument("name")
@click.argument("value")
def add(name: str, value: str):
    """Add an api key with a name and value"""
    try:
        # Check for duplicate api key name
        if name.lower() in config.list_api_keys():
            while True:
                new_name = click.prompt(
                    f"Api key '{name}' already exists. Enter a new name (or 'n' to cancel)",
                    type=str,
                )
                if new_name.lower() == "n":
                    click.echo("Operation cancelled.")
                    return

                if new_name.lower() not in config.list_api_keys():
                    name = new_name
                    break
                else:
                    click.echo(
                        f"Api key '{new_name}' also exists. Please try another name."
                    )

        # Add the api key
        config.add_api_key(name, value)
        click.echo(f"Successfully set api key {name}")
        if config.active_api_key == name.lower():
            click.echo(f"Api key {name} is now active")

    except Exception as e:
        click.echo(f"Error setting api key: {e}", err=True)


@keys.command()
@click.argument("name", required=False)
def get(name: str | None = None):
    """Get an api key value by name. If no name is provided, returns the active api key."""
    try:
        api_key_value = config.get_api_key(name)
        if api_key_value is None:
            click.echo("No api key found", err=True)
            return

        is_active = name is None or name.lower() == config.active_api_key
        data = [
            {
                "Name": name or config.active_api_key,
                "Value": api_key_value,
                "Status": "Active" if is_active else "Inactive",
            }
        ]
        click.echo(make_table_view(data, ["Name", "Value", "Status"]))
    except Exception as e:
        click.echo(f"Error getting api key: {e}", err=True)


@keys.command(name="rm")
@click.argument("name")
def remove(name: str):
    """Remove an api key by name"""
    try:
        config.remove_api_key(name)
        click.echo(f"Successfully removed api key {name}")

    except ValueError as e:
        click.echo(str(e), err=True)

    except Exception as e:
        click.echo(f"Error removing api key: {e}", err=True)


@keys.command(name="ls")
def list_api_keys():
    """List all available api keys"""
    try:
        api_keys = config.list_api_keys()
        if not api_keys:
            click.echo("No api keys set", err=True)
            return

        # Convert api keys to table format
        data = []
        for name, value in api_keys.items():
            is_active = name.lower() == config.active_api_key
            data.append(
                {
                    "Name": name,
                    "Value": value,
                    "Status": "Active" if is_active else "Inactive",
                }
            )

        click.echo(make_table_view(data, ["Name", "Value", "Status"]))
    except Exception as e:
        click.echo(f"Error listing api keys: {e}", err=True)


@keys.command()
@click.argument("name")
def use(name: str):
    """Set the active api key to use for other commands"""
    try:
        config.active_api_key = name.lower()
        click.echo(f"Successfully set {name} as active api key")

    except ValueError as e:
        click.echo(str(e), err=True)

    except Exception as e:
        click.echo(f"Error setting active api key: {e}", err=True)
