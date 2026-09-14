"""Compatibility entry; all conditions use the common class-granular query."""
def try_fast_filter(question_id, question):
    from .runtime import structured_payload
    return structured_payload(question_id, question, expected_tool="FILTER")
