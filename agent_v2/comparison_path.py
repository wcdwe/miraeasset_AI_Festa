"""Compatibility entry; no implicit period or representative-class selection."""
def try_fast_compare(question_id, question):
    from .runtime import structured_payload
    return structured_payload(question_id, question, expected_tool="COMPARE")
