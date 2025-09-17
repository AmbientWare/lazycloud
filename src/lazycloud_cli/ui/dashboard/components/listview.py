from textual.widgets import ListView


class LcListView(ListView):
    """A list view for deployments."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.styles.background = "transparent"
