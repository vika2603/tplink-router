import runpy
import sys

import click

from ._ctx import json_option, pass_state


@click.command("py", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("-c", "code", help="Python code to run.")
@click.argument("script", required=False, type=click.Path(exists=True, dir_okay=False, allow_dash=True))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@json_option
@pass_state
def py(st, code, script, args):
    """Run Python with a logged-in session: batch many ops in ONE login.

    \b
    Globals: r (Router, session reused/auto-relogin), tp (tplink_router), out (output module),
             show(*objs) (records for any model/list/dict), line(kind, **kv), pp(obj) (JSON).
    \b
    tpctl py -c 'inv = r.mesh.inventory(ping=True); show(inv.nodes_matching("2F"), r.health.issues(inv))'
    tpctl py batch.py arg1 arg2      (sys.argv = [batch.py, arg1, arg2])
    tpctl py - <<'EOF' ... EOF        (script from stdin)
    """
    import json

    import tplink_router as tp
    from .. import output as out
    from ..models import to_dict

    if not code and not script:
        raise click.UsageError("give -c CODE or a SCRIPT path")
    st.no_shared_reads = True              # scripts may poll: every inventory() is a fresh read
    st.r.share_reads = False               # also when the session was opened before this point
    st.r._inv_cache = None
    g = {"r": st.r, "tp": tp, "out": out, "show": out.show, "line": lambda *a, **k: print(out.line(*a, **k)),
         "pp": lambda obj: print(json.dumps(to_dict(obj), ensure_ascii=False, indent=1)), "__name__": "__main__"}
    if code or script == "-":
        src, name = (code, "<tplink py -c>") if code else (sys.stdin.read(), "<stdin>")
        sys.argv = [name, *args]
        exec(compile(src, name, "exec"), g)
    else:
        sys.argv = [script, *args]
        runpy.run_path(script, init_globals=g, run_name="__main__")


COMMANDS = [py]
