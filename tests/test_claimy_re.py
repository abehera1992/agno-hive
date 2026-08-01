from swarm.team import _CLAIMY_RE


def test_matches_existing_backticked_symbol():
    assert _CLAIMY_RE.search("The function is `getUser`.")


def test_matches_existing_file_line_citation():
    assert _CLAIMY_RE.search("See items_api.py:209 for the signature.")


def test_matches_bare_count_claim_with_trailing_noun():
    assert _CLAIMY_RE.search("There are 3 active, 3 inactive, 6 total items.")


def test_matches_count_of_phrasing():
    assert _CLAIMY_RE.search("The count of active users is 42.")


def test_does_not_match_plain_conversational_reply():
    assert not _CLAIMY_RE.search("Sure, that makes sense — go ahead.")


def test_does_not_match_could_not_verify():
    assert not _CLAIMY_RE.search("I could not verify this without reading the file.")
