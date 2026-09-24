# task_queue

A minimal in-memory task store with a 1-indexed pagination contract.
`task_queue/pagination.py` slices result pages; `task_queue/api.py` exposes them.

```bash
python -m pytest -q
```
