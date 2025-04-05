import click


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def remach():
    """ReMachina management commands"""
    pass


if __name__ == "__main__":
    remach()
