"""Independent work shares the client's bounded model-call semaphore."""
from concurrent.futures import ThreadPoolExecutor, as_completed

from storyforge.llm import CallPaused
from storyforge.runner import StageBlocked


def run(items, action, workers):
    waiting, paused, results = [], [], []
    with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="sfl-worker") as pool:
        futures = [pool.submit(action,item) for item in items]
        for future in as_completed(futures):
            try:results.append(future.result())
            except StageBlocked as exc:waiting.append(str(exc))
            except CallPaused as exc:paused.append(str(exc))
    if paused:raise CallPaused("; ".join(paused))
    if waiting:raise StageBlocked("; ".join(waiting))
    return results
