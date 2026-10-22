import json
from pathlib import Path

import click

from .. import human as h
from .. import output as o
from ..client import redact
from ._ctx import dry_run_option, json_option, parse_kv, pass_state

ENDPOINTS = Path(__file__).resolve().parents[1] / "data" / "endpoints.txt"


@click.group("api")
def api():
    """Raw endpoint access (see references/api.md). Output is always JSON."""


@api.command("get")
@click.argument("endpoint")
@click.argument("params", nargs=-1)
@click.option("--op", default="read", show_default=True, help="read | load | read_spf | loadSpeed | ...")
@click.option("--show-secrets", is_flag=True)
@json_option
@pass_state
def get(st, endpoint, params, op, show_secrets):
    """`tpctl api get 'admin/x?form=y' [k=v ...] [--op read_spf]` (secrets redacted by default)."""
    print(json.dumps(redact(st.r.call(endpoint, op, **parse_kv(params)), show_secrets), ensure_ascii=False, indent=1, default=str))


@api.command("set")
@click.argument("endpoint")
@click.argument("changes", nargs=-1, required=True)
@click.option("--op", default="write", show_default=True)
@click.option("--read-op", default="read", show_default=True)
@click.option("--partial", is_flag=True, help="Send only the given fields instead of read-merge-write.")
@dry_run_option
@json_option
@pass_state
def set_(st, endpoint, changes, op, read_op, partial, dry_run):
    """Write with plan/no-op-skip/snapshot/verify. Default read-merge-write of the whole form."""
    res = st.r.apply_form(endpoint, parse_kv(changes), tag="api-set", read_op=read_op, write_op=op,
                          partial=partial, dry_run=dry_run)
    st.out(res, o.write_lines(res), lambda: h.write(res))


@api.command("endpoints")
@click.option("--grep", "pattern", help="Substring filter.")
@json_option
@pass_state
def endpoints(st, pattern):
    """Every endpoint the router UI references, with the operations it uses (no login).

    \b
    ops  = operations the UI sends (read/write/load/insert/update/remove/read_spf/...; others are actions)
    live = operations seen answering on a real router (`!err` = that firmware rejected it)
    """
    rows = []
    for ln in ENDPOINTS.read_text().splitlines():
        if not ln.strip() or ln.startswith("#") or (pattern and pattern not in ln):
            continue
        path, ops, live = (ln.split("\t") + ["-", "-"])[:3]
        rows.append({"path": path, "ops": [] if ops == "-" else ops.split(","), "live": [] if live == "-" else live.split(",")})
    st.out(rows, [o.line("endpoint", path=r["path"], ops=r["ops"] or None, live=r["live"] or None) for r in rows]
           or [o.line("endpoints", count=0, grep=pattern)])


COMMANDS = [api]
