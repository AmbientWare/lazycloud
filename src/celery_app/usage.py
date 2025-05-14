from src.celery_app import app


@app.task()
def record_customer_usage():
    print("Recording customer usage")
    pass
