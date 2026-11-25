"""`trader` CLI — runtime control surface (Track B step 5).

The agent runs as a detached background process; the CLI talks to it
through the shared JSON state file. Commands implemented at this stage:
start, stop, pause, resume, status, watchlist show/set/reset, cycle-now.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import typer
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.control.state import load_state, update_state

app = typer.Typer(no_args_is_help=True, add_completion=False)
watchlist_app = typer.Typer(no_args_is_help=True)
app.add_typer(watchlist_app, name="watchlist")

PYTHON = ROOT / ".venv" / "bin" / "python"
AGENT_LOG = ROOT / "logs" / "agent_stdout.log"


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


@app.command()
def start():
    """Start the agent loop as a background process."""
    state = load_state()
    if _pid_alive(state["pid"]):
        typer.echo(f"already running (pid {state['pid']})")
        raise typer.Exit(1)
    update_state(command="run")
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_LOG, "a") as out:
        proc = subprocess.Popen([str(PYTHON), str(ROOT / "src" / "scheduler.py")],
                                cwd=ROOT, stdout=out, stderr=out,
                                start_new_session=True)
    typer.echo(f"agent started (pid {proc.pid}); logs: {AGENT_LOG.relative_to(ROOT)}")


@app.command()
def stop():
    """Graceful shutdown (takes effect within one tick)."""
    state = update_state(command="stop")
    if _pid_alive(state["pid"]):
        os.kill(int(state["pid"]), signal.SIGTERM)  # don't wait for the next tick
        update_state(status="stopped", pid=None)
        typer.echo(f"stop signal written; pid {state['pid']} terminated")
    else:
        update_state(status="stopped", pid=None)
        typer.echo("agent was not running; state cleared")


@app.command()
def pause():
    """Pause scanning (process stays alive)."""
    update_state(command="pause")
    typer.echo("pause signal written")


@app.command()
def resume():
    """Resume scanning."""
    update_state(command="run")
    typer.echo("resume signal written")


@app.command()
def status():
    """Current status, last cycle, last regime."""
    state = load_state()
    alive = _pid_alive(state["pid"])
    typer.echo(f"status:     {state['status']}{'' if alive or state['status'] == 'stopped' else ' (process dead!)'}")
    typer.echo(f"pid:        {state['pid']} ({'alive' if alive else 'not running'})")
    typer.echo(f"cycles:     {state['cycle_count']}  (last: {state['last_cycle']})")
    typer.echo(f"sector:     {state['sector']} | mode: {state['mode']}")
    wl = state["watchlist_override"]
    typer.echo(f"watchlist:  {'config default' if wl is None else ' '.join(wl)}")
    r = state["last_regime"]
    if r:
        typer.echo(f"regime:     {r['bias']} / trend {r['trend']} / VIX {r['vix']} "
                   f"(as of {r['ts']}){' [DEGRADED]' if r.get('degraded') else ''}")


@app.command()
def confirm(rec_id: str = typer.Argument(..., help="recommendation id to execute")):
    """Confirm a queued recommendation — the agent executes it on its next
    cycle (risk gate re-checked at execution time)."""
    from src.engine.confirm import confirm as _confirm
    typer.echo(_confirm(rec_id))


@app.command()
def decline(rec_id: str = typer.Argument(..., help="recommendation id to discard")):
    """Decline a queued recommendation."""
    from src.engine.confirm import decline as _decline
    typer.echo(_decline(rec_id))


@app.command()
def pending():
    """Show the recommendation confirmation queue (pending + confirmed)."""
    from src.engine.confirm import list_entries
    entries = list_entries()
    if not entries:
        typer.echo("confirmation queue empty")
        return
    for e in entries:
        typer.echo(f"{e['status']:>10}  {e['rec_id']}  {e['ticker']} {e['structure']} "
                   f"{e['direction']} x{e['quantity']} @ {e['net_debit']}  "
                   f"expires {e['expires_at']}")


@app.command("kill-switch")
def kill_switch(action: str = typer.Argument("status", help="on | off | status"),
                reason: str = typer.Option("", "--reason", help="why (recorded in the switch file)")):
    """Emergency stop: while ON the agent places NO new orders (funnel or
    execution-test) — checked every cycle, works mid-session without restart.
    Exits and position management continue (you can always flatten).
    Live mode can never start while the switch is on."""
    from src.engine.live_gate import engage_kill_switch, kill_switch_engaged, release_kill_switch
    if action == "on":
        detail = engage_kill_switch(reason)
        typer.echo(f"KILL SWITCH ENGAGED: {detail}")
    elif action == "off":
        typer.echo("kill switch released" if release_kill_switch()
                   else "kill switch was not engaged")
    elif action == "status":
        ks = kill_switch_engaged()
        typer.echo(f"ENGAGED: {ks}" if ks else "clear")
    else:
        typer.echo(f"unknown action {action!r} — use on | off | status")
        raise typer.Exit(1)


@app.command("cycle-now")
def cycle_now(force: bool = typer.Option(False, "--force",
                                         help="run even when the market is closed (testing only)")):
    """Force an immediate cycle in-process (manual trigger). Gated by the
    Tradier market clock — out-of-window cycles polluted the observation
    log on 6/11; use --force only for offline testing."""
    from src.scheduler import market_open
    if not force and not market_open():
        typer.echo("market closed (Tradier clock) — cycle refused. "
                   "Operating window is 06:30-13:00 PT. Use --force for offline testing.")
        raise typer.Exit(1)
    from src.engine.pipeline import run_cycle
    state = load_state()
    run_cycle(state["cycle_count"] + 1)


@watchlist_app.command("show")
def watchlist_show():
    state = load_state()
    if state["watchlist_override"]:
        typer.echo("override: " + " ".join(state["watchlist_override"]))
    else:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
        typer.echo("default: " + " ".join(cfg["watchlist"]["default"]))


@watchlist_app.command("set")
def watchlist_set(tickers: list[str]):
    update_state(watchlist_override=[t.upper() for t in tickers])
    typer.echo(f"watchlist override set: {' '.join(t.upper() for t in tickers)} (next cycle)")


@watchlist_app.command("reset")
def watchlist_reset():
    update_state(watchlist_override=None)
    typer.echo("watchlist reverted to config default (next cycle)")


if __name__ == "__main__":
    app()
