from hydra_rag_hooks.trigger import parse


def test_rag_colon_form():
    m = parse("rag: where do we handle auth?", ["rag:", "/rag"])
    assert m is not None and m.query == "where do we handle auth?" and m.tag is None


def test_slash_rag_form():
    m = parse("/rag where do we handle auth?", ["rag:", "/rag"])
    assert m is not None and m.query == "where do we handle auth?" and m.tag is None


def test_tagged_form():
    m = parse("rag@work: tokens", ["rag:", "/rag"])
    assert m is not None and m.query == "tokens" and m.tag == "work"


def test_all_tag():
    m = parse("rag@all: tokens", ["rag:", "/rag"])
    assert m is not None and m.tag == "all"


def test_lax_off_with_query_still_caller_choice():
    # When `lax=False`, "rag tokens" (no colon) is not a query trigger.
    # A user who turns off lax_trigger gets back the strict colon form.
    assert parse("rag tokens", ["rag:", "/rag"], lax=False) is None


def test_lax_enabled():
    m = parse("rag tokens", ["rag:", "/rag"], lax=True)
    assert m is not None and m.query == "tokens"


def test_non_trigger_returns_none():
    assert parse("how do I write a regex?", ["rag:", "/rag"]) is None


def test_leading_whitespace():
    m = parse("   rag: tokens", ["rag:", "/rag"])
    assert m is not None and m.query == "tokens"


def test_empty_colon_query_is_status():
    # `rag:` with nothing after no longer returns None; it's a bare form
    # and routes to the status command.
    m = parse("rag: ", ["rag:", "/rag"])
    assert m is not None and m.command == "status"


def test_case_insensitive():
    m = parse("RAG: tokens", ["rag:", "/rag"])
    assert m is not None and m.query == "tokens"


# Bare-form (status command) cases.

def test_bare_rag_is_status():
    m = parse("rag", ["rag:", "/rag"])
    assert m is not None and m.command == "status" and m.query == ""


def test_bare_slash_rag_is_status():
    m = parse("/rag", ["rag:", "/rag"])
    assert m is not None and m.command == "status"


def test_rag_status_word_is_status():
    m = parse("rag status", ["rag:", "/rag"])
    assert m is not None and m.command == "status"


def test_slash_rag_status_word_is_status():
    m = parse("/rag status", ["rag:", "/rag"])
    assert m is not None and m.command == "status"


def test_bare_rag_with_whitespace_is_status():
    m = parse("   rag   ", ["rag:", "/rag"])
    assert m is not None and m.command == "status"


def test_rag_followed_by_text_is_query_not_status(lax_on=True):
    # Sanity: "rag tokens" with lax=True is still a query, not status.
    m = parse("rag tokens", ["rag:", "/rag"], lax=True)
    assert m is not None and m.command is None and m.query == "tokens"
