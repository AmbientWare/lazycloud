from threading import Event

from agent.image_preparation import WorkerImagePreparation


def test_slow_preparation_leaves_control_available_and_bounds_concurrency() -> None:
    started = Event()
    finish = Event()

    def prepare(image: str, stop: Event) -> None:
        del stop
        if image == "first":
            started.set()
            if not finish.wait(timeout=5):
                raise TimeoutError("test did not release the image operation")

    images = WorkerImagePreparation(prepare)
    try:
        operation = images.start("first")
        assert started.wait(timeout=5)
        assert not images.ensure("first")
        assert not images.ensure("next")
        assert images.prepared() == []
        finish.set()
        operation.result(timeout=5)
        assert images.ensure("first")
        assert not images.ensure("next")
        images.start("next").result(timeout=5)
        assert images.ensure("next")
    finally:
        finish.set()
        images.close()


def test_shutdown_cancels_owned_preparation() -> None:
    started = Event()

    def prepare(image: str, stop: Event) -> None:
        del image
        started.set()
        if not stop.wait(timeout=5):
            raise TimeoutError("image preparation was not cancelled")

    images = WorkerImagePreparation(prepare)
    operation = images.start("image")
    assert started.wait(timeout=5)
    images.close()
    operation.result(timeout=0)
