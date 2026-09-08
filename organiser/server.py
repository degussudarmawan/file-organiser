"""The HTTP layer: a thin JSON shell around the manager.

This module holds no organising logic of its own. It parses requests, hands
them to a single shared `Manager`, and serialises whatever comes back. The UI
can only reach the classifier or the filesystem *through* the manager, so
every safety rule stays in one place instead of being re-implemented here.

Errors always come back as JSON — a browser fetch() should never have to
parse an HTML traceback to find out what went wrong.
"""

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .config import ConfigError, config_path, load_settings
from .manager import FirstRunConfirmationRequired, Manager
from .paths import UnsafePath, state_dir
from .plan import PlanStore
from .rules import known_extensions, rule_categories
from .undo_log import read_log, undo_log_path

# static/ sits next to the organiser package, at the project root.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

DEFAULT_LOG_LIMIT = 200
DEFAULT_PLAN_LIMIT = 20


def _json_body() -> dict:
    """The request body as a dict, or raise ValueError.

    `silent=True` keeps a malformed or absent body from becoming a 500 — the
    caller turns the ValueError into a 400 with a readable message.
    """
    body = request.get_json(silent=True)
    if body is None:
        raise ValueError("expected a JSON request body")
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def _int_arg(name: str, default: int) -> int:
    """A query-string integer, tolerating junk by falling back to the default."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def create_app(store: PlanStore | None = None) -> Flask:
    """Build the app with one shared PlanStore and one shared Manager.

    Passing a `store` is how tests point the whole app at a temp directory.
    """
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )

    plan_store = store or PlanStore()
    manager = Manager(delegate_name="classifier", store=plan_store)

    # Stashed so run.py (and tests) can introspect without rebuilding them.
    app.extensions["organiser_store"] = plan_store
    app.extensions["organiser_manager"] = manager

    # --- UI -----------------------------------------------------------------

    @app.get("/")
    def index():
        """Serve the single-page UI. It may not exist yet during development,
        so say so in JSON rather than raising."""
        if not (STATIC_DIR / "index.html").exists():
            return jsonify({"error": "static/index.html not found"}), 404
        return send_from_directory(STATIC_DIR, "index.html")

    # /static/<path> is handled by Flask's built-in static endpoint.

    # --- introspection ------------------------------------------------------

    @app.get("/health")
    def health():
        available, detail = manager.delegate_status()

        # Read fresh rather than cached at startup, so editing config.json is
        # reflected without restarting the server.
        try:
            settings = load_settings()
            provider = {
                "name": settings.provider,
                "base_url": settings.base_url,
                "scheme_model": settings.scheme.model,
                "classifier_model": settings.classifier.model,
                "config_path": settings.config_path,
                "sources": settings.sources,
                "error": None,
            }
        except ConfigError as exc:
            # Keep the shape stable even when the config is unusable: the
            # caller's whole job in this state is to tell someone which file
            # to go and fix, so the path must always be present.
            provider = {
                "name": None,
                "base_url": None,
                "scheme_model": None,
                "classifier_model": None,
                "config_path": str(config_path()),
                "sources": {},
                "error": str(exc),
            }

        return jsonify(
            {
                "ok": True,
                "classifier": {
                    "name": "classifier",
                    "available": available,
                    "detail": detail,
                },
                "provider": provider,
                "rules": {"known_extensions": known_extensions()},
                # Only the rule pass has fixed category names now; everything
                # else is proposed per run and arrives on the plan itself.
                "rule_categories": rule_categories(),
                "dynamic_categories": True,
                "state_dir": str(state_dir()),
                "undo_log": str(undo_log_path()),
            }
        )

    # --- scan / plan / apply ------------------------------------------------

    @app.post("/scan")
    def scan():
        """Build an inert plan. Never touches the target folder."""
        try:
            body = _json_body()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        target_dir = body.get("target_dir")
        if not isinstance(target_dir, str) or not target_dir.strip():
            return jsonify({"error": "target_dir is required"}), 400

        max_delegations = body.get("max_delegations")
        if max_delegations is not None:
            if isinstance(max_delegations, bool) or not isinstance(max_delegations, int):
                return jsonify({"error": "max_delegations must be an integer or null"}), 400
            if max_delegations < 0:
                return jsonify({"error": "max_delegations must not be negative"}), 400

        use_rules = body.get("use_rules", True)
        if not isinstance(use_rules, bool):
            return jsonify({"error": "use_rules must be true or false"}), 400

        try:
            plan = manager.scan(
                target_dir, max_delegations=max_delegations, use_rules=use_rules
            )
        except UnsafePath as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(plan.to_dict())

    @app.get("/plan/<plan_id>")
    def get_plan(plan_id: str):
        try:
            plan = plan_store.load(plan_id)
        except ValueError as exc:  # malformed id, per PlanStore._path
            return jsonify({"error": str(exc)}), 404
        if plan is None:
            return jsonify({"error": f"no such plan: {plan_id}"}), 404
        return jsonify(plan.to_dict())

    @app.post("/apply")
    def apply_plan():
        """Execute an already-built plan. The only route that moves files."""
        try:
            body = _json_body()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        plan_id = body.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            return jsonify({"error": "plan_id is required"}), 400

        approved_ids = body.get("approved_ids")
        if approved_ids is not None:
            if not isinstance(approved_ids, list) or not all(
                isinstance(i, str) for i in approved_ids
            ):
                return jsonify({"error": "approved_ids must be a list of strings or null"}), 400

        confirm_first_apply = bool(body.get("confirm_first_apply", False))

        try:
            result = manager.apply(
                plan_id,
                approved_ids=approved_ids,
                confirm_first_apply=confirm_first_apply,
            )
        except FirstRunConfirmationRequired as exc:
            # The frontend keys its confirm dialog off this exact shape.
            return (
                jsonify(
                    {
                        "error": str(exc)
                        or f"first apply to {exc.target_dir} needs confirmation",
                        "requires_confirmation": True,
                        "target_dir": str(exc.target_dir),
                    }
                ),
                409,
            )
        except UnsafePath as exc:
            return jsonify({"error": str(exc)}), 400
        except ValueError as exc:  # unknown or malformed plan id
            return jsonify({"error": str(exc)}), 404

        return jsonify(result.to_dict())

    # --- history ------------------------------------------------------------

    @app.get("/log")
    def get_log():
        try:
            limit = _int_arg("limit", DEFAULT_LOG_LIMIT)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        plan_id = request.args.get("plan_id") or None
        return jsonify(
            {
                "entries": read_log(limit=limit, plan_id=plan_id),
                "undo_log": str(undo_log_path()),
            }
        )

    @app.get("/plans")
    def get_plans():
        try:
            limit = _int_arg("limit", DEFAULT_PLAN_LIMIT)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"plans": plan_store.list_recent(limit)})

    # --- catch-all ----------------------------------------------------------

    @app.errorhandler(Exception)
    def on_error(exc: Exception):
        """Last line of defence: JSON, never an HTML traceback.

        HTTPExceptions (404 on an unknown route, 405 on a wrong method) keep
        their own status code; anything else is a genuine 500.
        """
        if isinstance(exc, HTTPException):
            return jsonify({"error": exc.description or exc.name}), exc.code
        return jsonify({"error": str(exc)}), 500

    return app
