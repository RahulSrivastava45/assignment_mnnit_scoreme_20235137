"""Command line interface.

    python -m decision_system.cli submit loan_application samples/loan_good.json
    python -m decision_system.cli explain <request_id>
    python -m decision_system.cli serve --port 8000
"""
import argparse
import json
import sys

from .api import serve
from .app import build_engine, DEFAULT_CONFIG, DEFAULT_DB
from .engine import IdempotencyConflict, InvalidOperation, NotFound
from .schema import ValidationError
from .store import StoreError
from .workflow import ConfigError


def _print(obj):
    print(json.dumps(obj, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decision_system")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="workflow config directory")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="sqlite file path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="submit a request")
    p.add_argument("workflow")
    p.add_argument("file", help="JSON file with the request data")
    p.add_argument("--key", help="idempotency key")

    for name in ("show", "history", "audit", "explain", "retry"):
        p = sub.add_parser(name)
        p.add_argument("request_id")

    p = sub.add_parser("review", help="manual review decision")
    p.add_argument("request_id")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)

    sub.add_parser("workflows", help="list configured workflows")

    p = sub.add_parser("serve", help="start the REST API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    engine = build_engine(args.config, args.db)

    try:
        if args.cmd == "submit":
            with open(args.file, encoding="utf-8") as fh:
                data = json.load(fh)
            result = engine.submit(args.workflow, data, args.key)
            if result.replayed:
                print("(duplicate request, returning existing record)", file=sys.stderr)
            _print(result.record)
        elif args.cmd == "show":
            _print(engine.get(args.request_id))
        elif args.cmd == "history":
            _print(engine.history(args.request_id))
        elif args.cmd == "audit":
            _print(engine.audit(args.request_id))
        elif args.cmd == "explain":
            print(engine.explain(args.request_id)["summary"])
        elif args.cmd == "retry":
            _print(engine.retry(args.request_id))
        elif args.cmd == "review":
            _print(engine.review(args.request_id, args.reviewer, args.decision, args.reason))
        elif args.cmd == "workflows":
            _print(engine.registry.names())
        elif args.cmd == "serve":
            server = serve(engine, args.host, args.port)
            print(f"listening on http://{args.host}:{args.port}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                server.server_close()
    except ValidationError as exc:
        print("validation failed:", *exc.errors, sep="\n  - ")
        return 2
    except (IdempotencyConflict, InvalidOperation, ConfigError) as exc:
        print("error:", exc)
        return 1
    except NotFound as exc:
        print(f"error: request {exc} not found")
        return 1
    except StoreError:
        print("error: storage failure, nothing was saved. Run the same command again.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
