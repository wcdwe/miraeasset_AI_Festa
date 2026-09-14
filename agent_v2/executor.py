"""Public execution facade; all tasks use a single repository/executor."""
from .task_executor import execute_tasks
from .product_repository import DB_PATH, FIELDS as ALLOWED_FIELDS, matches as _matches
from .document_path import retrieve_document_hits


def execute_plan(question, plan):
    return execute_tasks(question, plan)


def _execute_filter(plan):
    from .product_repository import query
    try:
        result, evidence = query(plan, codes=plan.entities.get("anchor_product_codes"))
        return result, evidence, []
    except ValueError as exc:
        return {}, [], [str(exc)]
