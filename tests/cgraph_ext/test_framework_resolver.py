"""Tests for the shared framework resolver (Phase 5.8)."""

from __future__ import annotations

from codegraphcontext_ext.framework.resolver import (
    FrameworkMatch,
    build_handler_decorator_clause,
    classify_decorator,
    classify_decorators,
    get_frameworks,
    get_http_frameworks,
    normalize_decorator,
)


# ── normalize_decorator ───────────────────────────────────────────────


def test_normalize_strips_at():
    assert normalize_decorator("@app.route") == "app.route"


def test_normalize_strips_parens():
    assert normalize_decorator("@app.route('/login')") == "app.route"


def test_normalize_strips_both():
    assert normalize_decorator("@pytest.fixture(scope='session')") == "pytest.fixture"


def test_normalize_plain():
    assert normalize_decorator("dataclass") == "dataclass"


def test_normalize_none():
    assert normalize_decorator(None) == ""


def test_normalize_empty():
    assert normalize_decorator("") == ""


# ── classify_decorator ────────────────────────────────────────────────


def test_classify_flask_route():
    fw = classify_decorator("app.route")
    assert fw is not None
    assert fw.name == "flask"
    assert fw.category == "http"


def test_classify_fastapi_get():
    fw = classify_decorator("router.get")
    assert fw is not None
    assert fw.name == "fastapi"


def test_classify_fastapi_post():
    fw = classify_decorator("router.post")
    assert fw is not None
    assert fw.name == "fastapi"


def test_classify_fastapi_websocket():
    fw = classify_decorator("app.websocket")
    assert fw is not None
    assert fw.name == "fastapi"


def test_classify_django_api_view():
    fw = classify_decorator("rest_framework.api_view")
    assert fw is not None
    assert fw.name == "django"


def test_classify_click_command():
    fw = classify_decorator("click.command")
    assert fw is not None
    assert fw.category == "cli"


def test_classify_celery_task():
    fw = classify_decorator("app.task")
    assert fw is not None
    assert fw.name == "celery"
    assert fw.category == "worker"


def test_classify_celery_shared_task():
    fw = classify_decorator("shared_task")
    assert fw is not None
    assert fw.name == "celery"


def test_classify_pytest_fixture():
    fw = classify_decorator("pytest.fixture")
    assert fw is not None
    assert fw.name == "pytest"
    assert fw.category == "test"


def test_classify_graphql_mutation():
    fw = classify_decorator("strawberry.mutation")
    assert fw is not None
    assert fw.name == "graphql"
    assert fw.category == "graphql"


def test_classify_unknown_returns_none():
    assert classify_decorator("dataclass") is None


def test_classify_empty_returns_none():
    assert classify_decorator("") is None


# ── classify_decorators (multi-decorator) ─────────────────────────────


def test_classify_decorators_flask_route():
    result = classify_decorators(["@app.route('/login')"])
    assert result is not None
    assert result.framework == "flask"
    assert result.category == "http"
    assert result.base_score == 5.0
    assert "app.route" in result.matched_decorators


def test_classify_decorators_picks_highest_score():
    result = classify_decorators([
        "@pytest.fixture",
        "@app.route('/api')",
    ])
    assert result is not None
    assert result.framework == "flask"
    assert result.base_score == 5.0
    assert len(result.matched_decorators) == 2


def test_classify_decorators_none():
    assert classify_decorators(None) is None


def test_classify_decorators_empty():
    assert classify_decorators([]) is None


def test_classify_decorators_no_match():
    assert classify_decorators(["@dataclass", "@staticmethod"]) is None


def test_classify_decorators_deduplicates():
    result = classify_decorators([
        "@app.route('/a')",
        "@app.route('/b')",
    ])
    assert result is not None
    assert len(result.matched_decorators) == 1


def test_classify_decorators_celery():
    result = classify_decorators(["@app.task(bind=True)"])
    assert result is not None
    assert result.framework == "celery"
    assert result.category == "worker"


# ── get_frameworks ────────────────────────────────────────────────────


def test_get_frameworks_nonempty():
    fws = get_frameworks()
    assert len(fws) >= 8
    names = {fw.name for fw in fws}
    assert "flask" in names
    assert "fastapi" in names
    assert "django" in names
    assert "celery" in names
    assert "pytest" in names


def test_get_http_frameworks():
    http_fws = get_http_frameworks()
    assert all(fw.category == "http" for fw in http_fws)
    names = {fw.name for fw in http_fws}
    assert "flask" in names
    assert "fastapi" in names
    assert "django" in names


# ── build_handler_decorator_clause ────────────────────────────────────


def test_clause_all_frameworks():
    clause = build_handler_decorator_clause("handler")
    assert "ANY(d IN handler.decorators WHERE" in clause
    assert "d CONTAINS 'route'" in clause
    assert "d CONTAINS 'get'" in clause
    assert "d CONTAINS 'task'" in clause
    assert "d CONTAINS 'fixture'" in clause


def test_clause_http_only():
    clause = build_handler_decorator_clause("f", categories=("http",))
    assert "ANY(d IN f.decorators WHERE" in clause
    assert "d CONTAINS 'route'" in clause
    assert "d CONTAINS 'api_view'" in clause
    # Worker/test patterns should NOT be present
    assert "task" not in clause
    assert "fixture" not in clause


def test_clause_worker_only():
    clause = build_handler_decorator_clause("n", categories=("worker",))
    assert "d CONTAINS 'task'" in clause
    assert "d CONTAINS 'shared_task'" in clause
    assert "route" not in clause


def test_clause_custom_node_var():
    clause = build_handler_decorator_clause("fn", categories=("test",))
    assert "ANY(d IN fn.decorators WHERE" in clause
    assert "d CONTAINS 'fixture'" in clause
