"""A very small in-memory task listing API."""

from __future__ import annotations

from dataclasses import dataclass

from task_queue.pagination import DEFAULT_PAGE_SIZE, Page, paginate


@dataclass(frozen=True)
class Task:
    identifier: str
    title: str
    done: bool = False


class TaskStore:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self._tasks: list[Task] = list(tasks or [])

    def add(self, task: Task) -> None:
        self._tasks.append(task)

    def pending(self) -> list[Task]:
        return [task for task in self._tasks if not task.done]

    def list_tasks(self, page: int = 1, per_page: int = DEFAULT_PAGE_SIZE) -> Page:
        """Return a page of pending tasks, newest last."""
        return paginate(self.pending(), page=page, per_page=per_page)
