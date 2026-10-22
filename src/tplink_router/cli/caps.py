import click

from .. import human as h
from .. import output as o
from ._ctx import json_option, pass_state


@click.command("capabilities")
@click.option("--refresh", is_flag=True, help="Re-probe the router (cached per host+model+firmware otherwise).")
@json_option
@pass_state
def capabilities_cmd(st, refresh):
    """What the connected model/firmware supports (probed read-only, cached). Unsupported commands exit 8."""
    c = st.r.caps.detect(refresh=refresh)
    st.out(c, [o.line("router", model=c["model"], hardware=c["hardware"], firmware=c["firmware"], detected_at=c["detected_at"]),
               o.line("bands", list=c["bands"]),
               o.line("supported", list=sorted(k for k, v in c["features"].items() if v)),
               o.line("unsupported", list=sorted(k for k, v in c["features"].items() if not v) or None)],
           lambda: [h.kvline("Router", f"{c['model']} " + h.dim(f"fw {c['firmware']} · probed {c['detected_at'][:16]}"), 12),
                    h.kvline("Bands", ", ".join(b.replace("g", "G").replace("_2", "-2") for b in c["bands"]), 12),
                    h.kvline("Supported", ", ".join(h.green(k) for k, v in sorted(c["features"].items()) if v), 12),
                    h.kvline("Unsupported", ", ".join(h.dim(k) for k, v in sorted(c["features"].items()) if not v) or "none", 12)])


COMMANDS = [capabilities_cmd]
