import typer
from rich.console import Console

from lazycloud_cli.config import config
from lazycloud_cli.ui.views import AuthView
from lazycloud_cli.api import api

app = typer.Typer(help="Add a new API key")
console = Console()
view = AuthView(console)


@app.command()
def add(
    name: str = typer.Argument(..., help="Name for the API key"),
):
    """Add a new API key"""
    # Check if key name already exists
    if name in config.list_api_keys():
        if not view.confirm_overwrite(name):
            return

    value = view.prompt_api_key()

    # First we need to check if the key is valid
    view.show_validating_key()

    # save the old key to restore it later
    old_key = config.active_api_key
    validation_passed = False

    try:
        # create a new TMP key and make it active
        config.add_api_key(f"tmp_{name}", value)
        config.active_api_key = f"tmp_{name}"
        # check if the key is valid
        user_id = api.users.get_user_id()
        if user_id:  # Only valid if we get a non-empty user ID
            validation_passed = True
        else:
            view.show_key_invalid()

    except Exception:
        view.show_key_invalid()

    finally:
        # restore the old key
        config.active_api_key = old_key
        # Remove the temporary key
        try:
            config.remove_api_key(f"tmp_{name}")
        except Exception:
            pass  # Ignore if it doesn't exist

    # Only proceed if validation passed
    if not validation_passed:
        raise typer.Exit(1)

    try:
        # Add the API key
        config.add_api_key(name, value)

        # If this is the first key, set it as active
        set_active = not config.active_api_key
        if set_active:
            config.active_api_key = name

        view.show_key_added(name, set_active)

    except Exception as e:
        view.show_error(f"Failed to add API key: {e}")
        raise typer.Exit(1)
