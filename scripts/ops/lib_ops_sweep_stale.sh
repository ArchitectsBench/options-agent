#!/bin/bash
# Age check for the nightly ops sweep log, extracted from scripts/deadman.sh
# (check 2) on 2026-10-05 so it can be probed hermetically against a tmp copy
# instead of production logs/launchd_nightlyops.log (CLAUDE.md destructive-probe
# rule). Sourced by deadman.sh; carries no side effects of its own.
#
# WHY DAY-AWARE (2026-10-05): the nightly chain (com.optionsagent.nightlyops)
# fires 18:40 on Weekday 1-5 ONLY, so from Friday 18:47 until Monday 18:40 the
# newest run is legitimately Friday's. The old flat 26h threshold therefore made
# every Monday run before 18:40 page a FALSE `ops-sweep-stale` — ~53h old at
# 00:05, ~71h at 18:05, i.e. 00:05/06:05/12:05/18:05 all false, four per Monday.
# Monday now allows 75h (4500 min); Tue-Fri keep 26h (1560 min).
#
# This is still a REAL check on Monday, which is why Mondays are not simply
# skipped: a chain that died after Thursday's run reads ~96h by Monday 06:05 and
# still pages. Skipping Mondays would mask a Friday death until Tuesday.

# Minutes of staleness tolerated for a given day-of-week (1=Mon .. 7=Sun).
ops_sweep_max_age_min() {
    local dow="$1"
    if [ "$dow" -eq 1 ]; then
        echo 4500   # Monday: newest legitimate run is Friday 18:40 (~71h at 18:05)
    else
        echo 1560   # Tue-Fri: previous night's 18:40 run (26h)
    fi
}

# ops_sweep_stale <log-path> <day-of-week 1-7>
# Exit 0 = STALE (caller should alert), exit 1 = fresh or not applicable.
# A missing log on a weekday is stale; the weekend is not applicable at all
# because the chain does not run Sat/Sun.
ops_sweep_stale() {
    local log="$1" dow="$2" max
    if [ "$dow" -gt 5 ]; then
        return 1    # Sat/Sun: chain does not run, nothing to be stale about
    fi
    if [ ! -f "$log" ]; then
        return 0
    fi
    max=$(ops_sweep_max_age_min "$dow")
    if [ -n "$(find "$log" -mmin +"$max" 2>/dev/null)" ]; then
        return 0
    fi
    return 1
}
