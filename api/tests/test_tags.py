import tags


def test_normalize_tag_strips_and_lowercases():
    assert tags.normalize_tag("  Sale  ") == "sale"


def test_is_valid_tag_accepts_boundary_lengths():
    assert tags.is_valid_tag("a") is True
    assert tags.is_valid_tag("a" * tags.MAX_TAG_LENGTH) is True
    assert tags.is_valid_tag("a" * (tags.MAX_TAG_LENGTH + 1)) is False


def test_parse_tags_normalizes_and_sorts():
    result, error = tags.parse_tags(["  Sale  ", "Q4"])
    assert error is None
    assert result == ["q4", "sale"]


def test_parse_tags_deduplicates_case_insensitively():
    result, error = tags.parse_tags(["b", "A", "b"])
    assert error is None
    assert result == ["a", "b"]


def test_parse_tags_none_allowed_by_default():
    result, error = tags.parse_tags(None)
    assert error is None
    assert result == []


def test_parse_tags_none_rejected_when_disallowed():
    result, error = tags.parse_tags(None, allow_none=False)
    assert result is None
    assert error == {"error": "invalid_tags"}


def test_parse_tags_rejects_non_list():
    result, error = tags.parse_tags("sale")
    assert result is None
    assert error == {"error": "invalid_tags"}


def test_parse_tags_rejects_non_string_member():
    result, error = tags.parse_tags(["sale", 1])
    assert result is None
    assert error == {"error": "invalid_tags"}


def test_parse_tags_rejects_overlong_tag_as_submitted():
    overlong = "a" * (tags.MAX_TAG_LENGTH + 1)
    result, error = tags.parse_tags([overlong])
    assert result is None
    assert error == {"error": "invalid_tag", "tag": overlong}


def test_parse_tags_rejects_leading_hyphen():
    result, error = tags.parse_tags(["-lead"])
    assert result is None
    assert error == {"error": "invalid_tag", "tag": "-lead"}


def test_parse_tags_rejects_internal_space():
    result, error = tags.parse_tags(["a b"])
    assert result is None
    assert error == {"error": "invalid_tag", "tag": "a b"}


def test_parse_tags_rejects_non_ascii():
    result, error = tags.parse_tags(["café"])
    assert result is None
    assert error == {"error": "invalid_tag", "tag": "café"}


def test_parse_tags_too_many_tags_carries_max():
    eleven = [f"tag{i}" for i in range(11)]
    result, error = tags.parse_tags(eleven)
    assert result is None
    assert error == {"error": "too_many_tags", "max_tags": tags.MAX_TAGS_PER_LINK}


def test_apply_tags_unions_dedupes_and_sorts():
    assert tags.apply_tags(["b", "a"], ["a", "c"]) == ["a", "b", "c"]


def test_remove_tags_no_op_when_absent():
    assert tags.remove_tags(["a"], ["zz"]) == ["a"]


def test_remove_tags_removes_present():
    assert tags.remove_tags(["a", "b", "c"], ["b"]) == ["a", "c"]
