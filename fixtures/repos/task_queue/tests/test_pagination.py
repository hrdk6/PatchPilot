import pytest
from task_queue.api import Task, TaskStore
from task_queue.pagination import paginate

ITEMS = ["a", "b", "c", "d", "e"]


def test_first_page_starts_at_the_beginning():
    page = paginate(ITEMS, page=1, per_page=2)
    assert page.items == ["a", "b"]


def test_second_page():
    page = paginate(ITEMS, page=2, per_page=2)
    assert page.items == ["c", "d"]


def test_page_past_the_end_is_empty():
    page = paginate(ITEMS, page=4, per_page=2)
    assert page.items == []
    assert page.has_next is False


def test_total_pages():
    assert paginate(ITEMS, page=1, per_page=2).total_pages == 3


def test_rejects_zero_per_page():
    with pytest.raises(ValueError):
        paginate(ITEMS, page=1, per_page=0)


def test_store_lists_first_page():
    store = TaskStore([Task("1", "write tests"), Task("2", "ship"), Task("3", "measure")])
    assert [task.identifier for task in store.list_tasks(page=1, per_page=2).items] == ["1", "2"]
