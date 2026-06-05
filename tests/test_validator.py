from scar.validator import validate, _check_safety_rules


def test_rejects_malloc():
    patch = "+    ptr = malloc(size);\n"
    result = _check_safety_rules(patch)
    assert not result.passed
    assert "heap" in result.detail.lower()


def test_rejects_strcpy():
    patch = "+    strcpy(dst, src);\n"
    result = _check_safety_rules(patch)
    assert not result.passed
    assert "MISRA" in result.detail


def test_rejects_unbounded_while():
    patch = "+    while (1) { process(); }\n"
    result = _check_safety_rules(patch)
    assert not result.passed


def test_accepts_safe_patch():
    patch = "+    strncpy(dst, src, sizeof(dst) - 1);\n"
    result = _check_safety_rules(patch)
    assert result.passed


def test_ignores_removed_lines():
    # A removed line with malloc should NOT trigger the rule
    patch = "-    ptr = malloc(size);\n+    ptr = &static_buf;\n"
    result = _check_safety_rules(patch)
    assert result.passed
