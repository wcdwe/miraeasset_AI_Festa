"""Compatibility entry; no independent SQL or keyword fallback."""
def try_fast_structured(question_id, question):
    from .runtime import structured_payload
    return structured_payload(question_id, question, expected_tool="FACT")
