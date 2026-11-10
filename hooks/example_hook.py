"""Optional plugin hook example."""
stage = None
context = {}


def run(stage_name: str, ctx: dict):
    global stage, context
    stage = stage_name
    context = ctx
    # Hook logic here.
    # Examples: PII redaction, notifications, extra storage, validation.
