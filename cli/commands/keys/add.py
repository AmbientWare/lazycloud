import typer
from cli.config import config
from cli.logging import logger
from cli.api import api
app = typer.Typer(help="Add a new API key")


@app.command()
def add(
    name: str = typer.Option(..., prompt="Name for the API key", help="Name for the API key"),
    value: str = typer.Option(..., prompt="API key value", hide_input=True, help="API key value"),
):
    """Add a new API key"""
    # Check if key name already exists
    if name in config.list_api_keys():
        if not typer.confirm(f"API key '{name}' already exists. Overwrite?"):
            return

    ## First we need to check if the key is valid
    # save the old key to restore it later
    old_key = config.active_api_key

    try:
        # create a new TMP key and make it active
        config.add_api_key(f"TMP_{name}", value)
        config.active_api_key = f"TMP_{name}"
        # check if the key is valid
        api.users.get_user_id()

    except Exception as e:
        logger.error(f"The API key is invalid. Please check the key and try again.")
        raise typer.Exit(1)

    finally:
        # restore the old key
        config.active_api_key = old_key
        config.remove_api_key(f"TMP_{name}")


    try:
        # Add the API key
        config.add_api_key(name, value)
        logger.success(f"Added API key '{name}'")

        # If this is the first key, set it as active
        if not config.active_api_key:
            config.active_api_key = name
            logger.info(f"Set '{name}' as active API key")

    except Exception as e:
        logger.error(f"Failed to add API key: {e}")
        raise typer.Exit(1)
