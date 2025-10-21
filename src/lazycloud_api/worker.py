from lazycloud_api.prefect_app import serve_deployments


def main():
    """Serve the Prefect worker"""
    serve_deployments()


if __name__ == "__main__":
    main()
