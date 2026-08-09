"""checkbox(allow_empty=True) must construct a real questionary prompt without raising.

Regression: the installed questionary version (2.0.1) requires `validate` to be callable
and raises `ValueError("validate must be callable")` immediately inside questionary.checkbox()
- before .ask() is ever reached - if it's passed None, which is what allow_empty=True used
to do. This only surfaced once guided_flow() started reaching the multi-choice branch with
5 configured targets instead of 2-3, but the bug applies to any allow_empty=True call
regardless of choice count.

.ask() itself needs a real terminal (it drives prompt_toolkit's async event loop), so these
tests stub out just that method - the thing under test is whether constructing the prompt
(where questionary's callable check lives) raises, not the interactive read.
"""

from questionary.question import Question

from store_pricing.ui import checkbox


def test_allow_empty_checkbox_is_constructed_without_raising(monkeypatch):
    monkeypatch.setattr(Question, "ask", lambda self, **kwargs: [])
    assert checkbox("Pick some:", ["A", "B", "C"], allow_empty=True) == []


def test_required_checkbox_is_constructed_without_raising(monkeypatch):
    monkeypatch.setattr(Question, "ask", lambda self, **kwargs: ["A"])
    assert checkbox("Pick some:", ["A", "B", "C"], allow_empty=False) == ["A"]
