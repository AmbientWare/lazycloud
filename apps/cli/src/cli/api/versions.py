from responses.versions import CLIVersionResponse

from cli.api.base import BaseAPI


class VersionsAPI(BaseAPI):
    def __init__(self):
        super().__init__("cli-version", use_version=False)

    def get_cli_version(self) -> CLIVersionResponse:
        """Get the version of the CLI"""
        response = self._get()
        return CLIVersionResponse(**response)


versions_api = VersionsAPI()
