__all__ = [
    "same_task_threads",
]

import queue
import threading


def same_task_threads(
    task_func: callable, n_workers: int, result_queue: queue.Queue = None
) -> queue.Queue:
    """Creates workers and provides queue to assign work

    Args:
        task_func (callable): Task.
        n_workers (int): Number of requested workers.
        result_queue (queue.Queue, optional): Queue in which to drop
            results. Defaults to None.

    Returns:
        queue.Queue: Task queue.
    """
    task_queue = queue.Queue()

    def worker():
        while True:
            try:
                next_task = task_queue.get()
            except TypeError:
                continue
            if next_task is not None:
                result = task_func(*next_task)
                if result_queue is not None:
                    result_queue.put(result)
                task_queue.task_done()

    for i in range(n_workers):
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
    return task_queue
