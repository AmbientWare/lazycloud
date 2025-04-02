import click
from cli.utils import make_table_view
from cli.config import config


@click.group()
def tokens():
    """Token management commands"""
    pass


@tokens.command()
@click.argument("name")
@click.argument("value")
def add(name: str, value: str):
    """Add a token with a name and value"""
    try:
        # Check for duplicate token name
        if name.lower() in config.list_tokens():
            while True:
                new_name = click.prompt(
                    f"Token '{name}' already exists. Enter a new name (or 'n' to cancel)",
                    type=str,
                )
                if new_name.lower() == "n":
                    click.echo("Operation cancelled.")
                    return

                if new_name.lower() not in config.list_tokens():
                    name = new_name
                    break
                else:
                    click.echo(
                        f"Token '{new_name}' also exists. Please try another name."
                    )

        # Add the token
        config.add_token(name, value)
        click.echo(f"Successfully set token {name}")
        if config.active_token == name.lower():
            click.echo(f"Token {name} is now active")

    except Exception as e:
        click.echo(f"Error setting token: {e}", err=True)


@tokens.command()
@click.argument("name", required=False)
def get(name: str | None = None):
    """Get a token value by name. If no name is provided, returns the active token."""
    try:
        token_value = config.get_token(name)
        if token_value is None:
            click.echo("No token found", err=True)
            return

        is_active = name is None or name.lower() == config.active_token
        data = [
            {
                "Name": name or config.active_token,
                "Value": token_value,
                "Status": "Active" if is_active else "Inactive",
            }
        ]
        click.echo(make_table_view(data, ["Name", "Value", "Status"]))
    except Exception as e:
        click.echo(f"Error getting token: {e}", err=True)


@tokens.command(name="rm")
@click.argument("name")
def remove(name: str):
    """Remove a token by name"""
    try:
        config.remove_token(name)
        click.echo(f"Successfully removed token {name}")
    except ValueError as e:
        click.echo(str(e), err=True)
    except Exception as e:
        click.echo(f"Error removing token: {e}", err=True)


@tokens.command(name="ls")
def list_tokens():
    """List all available tokens"""
    try:
        tokens = config.list_tokens()
        if not tokens:
            click.echo("No tokens set", err=True)
            return

        # Convert tokens to table format
        data = []
        for name, value in tokens.items():
            is_active = name.lower() == config.active_token
            data.append(
                {
                    "Name": name,
                    "Value": value,
                    "Status": "Active" if is_active else "Inactive",
                }
            )

        click.echo(make_table_view(data, ["Name", "Value", "Status"]))
    except Exception as e:
        click.echo(f"Error listing tokens: {e}", err=True)


@tokens.command()
@click.argument("name")
def use(name: str):
    """Set the active token to use for other commands"""
    try:
        config.active_token = name.lower()
        click.echo(f"Successfully set {name} as active token")
    except ValueError as e:
        click.echo(str(e), err=True)
    except Exception as e:
        click.echo(f"Error setting active token: {e}", err=True)
