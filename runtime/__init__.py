"""An approval-gated agent runtime.

A small, honest implementation of the pattern: a model drafts, a human approves, and the
approval is kept as training data. See the repository README for what each module is for
and which production system the design came from.
"""
__all__ = ["store", "approval", "policy", "review", "surface", "llm", "transport"]
