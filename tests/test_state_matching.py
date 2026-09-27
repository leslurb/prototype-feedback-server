from feedback_state import classify_repeat, iou, normalize_path, resolve_repo

PROJ = {"repo": "leslurb/abdry-rebuild", "local_path": r"C:\Users\sebas\abdry-rebuild",
        "url_prefixes": ["https://abdry-staging.pages.dev"], "target_url": None}


def test_normalize_path():
    assert normalize_path("https://x.dev/products/?a=1#top") == "/products"
    assert normalize_path("https://x.dev") == "/"
    assert normalize_path("/contact/") == "/contact"
    assert normalize_path(None) == "/"


def test_project_mapping_wins_over_reviewer_hint():
    hints = {"https://abdry-staging.pages.dev": {"repo": "someone/else"}}
    got = resolve_repo(PROJ, "https://abdry-staging.pages.dev/products", hints)
    assert got == {"repo": "leslurb/abdry-rebuild", "local_path": PROJ["local_path"], "source": "project"}


def test_url_outside_project_prefixes_is_not_trusted():
    assert resolve_repo(PROJ, "https://evil.example/products", None) is None


def test_direct_issue_path_resolves_to_project():
    assert resolve_repo(PROJ, "/products", None)["source"] == "project"


def test_reviewer_hint_fallback_uses_longest_prefix():
    hints = {"https://a.dev": {"repo": "r/short"}, "https://a.dev/app": {"repo": "r/long", "local_path": "C:/x"}}
    got = resolve_repo({"repo": None}, "https://a.dev/app/page", hints)
    assert got == {"repo": "r/long", "local_path": "C:/x", "source": "reviewer_hint"}


def test_unmapped_returns_none():
    assert resolve_repo(None, "https://a.dev/x", {}) is None


def test_project_without_prefixes_trusts_any_url_and_hints_are_not_consulted():
    proj = {"repo": "r/p", "local_path": None, "url_prefixes": [], "target_url": None}
    hints = {"https://other.dev": {"repo": "r/hint"}}
    assert resolve_repo(proj, "https://other.dev/x", hints)["repo"] == "r/p"


def test_iou():
    assert iou({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": 0, "w": 10, "h": 10}) == 1.0
    assert iou({"x": 0, "y": 0, "w": 10, "h": 10}, {"x": 20, "y": 20, "w": 5, "h": 5}) == 0.0
    assert iou({}, {"x": 0, "y": 0, "w": 1, "h": 1}) == 0.0


def test_classify_repeat_regression_vs_didnt_take():
    new = {"page": "/", "selector": "footer .logo img", "bbox": {}}
    shipped_prior = {"id": "p1", "page": "/", "selector": "footer .logo img", "bbox": {}, "shipped": True}
    unshipped_prior = {"id": "p2", "page": "/", "selector": "footer .logo img", "bbox": {}, "shipped": False}
    assert classify_repeat(new, [shipped_prior]) == ("p1", "regression")
    assert classify_repeat(new, [unshipped_prior]) == ("p2", "didnt_take")


def test_classify_repeat_region_pins_use_overlap_and_ignore_other_pages():
    new = {"page": "/", "selector": None, "bbox": {"x": 0, "y": 0, "w": 10, "h": 10}}
    near = {"id": "p3", "page": "/", "selector": None, "bbox": {"x": 1, "y": 1, "w": 10, "h": 10}, "shipped": True}
    other_page = {"id": "p4", "page": "/x", "selector": None, "bbox": new["bbox"], "shipped": True}
    assert classify_repeat(new, [other_page]) is None
    assert classify_repeat(new, [near]) == ("p3", "regression")


def test_project_prefix_match_respects_a_boundary():
    proj = {"repo": "r/a", "url_prefixes": ["https://a.dev"], "target_url": None}
    assert resolve_repo(proj, "https://a.dev.evil.tld/x", None) is None
    assert resolve_repo(proj, "https://a.dev/x", None)["repo"] == "r/a"
    assert resolve_repo(proj, "https://a.dev", None)["repo"] == "r/a"
    assert resolve_repo(proj, "https://a.dev?q=1", None)["repo"] == "r/a"
    assert resolve_repo(proj, "https://a.dev#top", None)["repo"] == "r/a"
    slash = {"repo": "r/a", "url_prefixes": ["https://a.dev/app/"], "target_url": None}
    assert resolve_repo(slash, "https://a.dev/app/page", None)["repo"] == "r/a"
    assert resolve_repo(slash, "https://a.dev/apple", None) is None


def test_reviewer_hint_prefix_match_respects_a_boundary():
    hints = {"https://a.dev": {"repo": "r/hint"}}
    assert resolve_repo(None, "https://a.dev.evil.tld/x", hints) is None
    assert resolve_repo(None, "https://a.dev/x", hints)["repo"] == "r/hint"
    assert resolve_repo(None, "https://a.dev", hints)["repo"] == "r/hint"
