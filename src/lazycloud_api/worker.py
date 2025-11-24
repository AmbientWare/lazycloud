from lazycloud_api.prefect_app import serve_crons


def main():
    """Serve the Prefect worker for cron tasks"""
    serve_crons()


if __name__ == "__main__":
    main()
