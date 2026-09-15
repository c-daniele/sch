#!/bin/bash
# mem-trace.sh — second-granularity memory sampler for TASK-1.1 peak attribution.
#
# AgentCore bills memory on the peak consumed up to each second, so attribution
# needs per-second evidence of WHICH phase spiked (env bootstrap/build vs
# harness+MCP vs checkpoint tar/gzip), not averages. This script samples
# system memory plus the top RSS processes every interval (default 1s) and
# writes CSV the operator aligns with wall-clock phase boundaries from
# task-status.json heartbeats and the shim log (the vended server-side meter
# itself lives in CloudWatch — see docs/history/memory-peak-attribution.md).
#
# Usage:
#   ./bin/mem-trace.sh [-i seconds] [-o trace.csv] -- <command...>  # wrap mode
#   ./bin/mem-trace.sh [-i seconds] [-o trace.csv] -d seconds       # observe mode
#
# Wrap mode runs the command, sampling until it exits, and records the start/end
# as `# mark` rows (the phase boundaries for that command). Observe mode samples
# for the given duration while the operator drives phases from another shell.
# Summary (peak pressure, worst instant, top offender) goes to stdout; the
# full series goes to the CSV (default ./mem-trace-<epoch>.csv).
set -euo pipefail

interval="1"
out=""
duration=""
mode="wrap"

while [ $# -gt 0 ]; do
    case "$1" in
        -i) [ $# -ge 2 ] || { echo "usage: $0 [-i seconds] [-o file] (-- <cmd...> | -d seconds)" >&2; exit 2; }
            interval="$2"; shift 2 ;;
        -o) [ $# -ge 2 ] || { echo "usage: $0 [-i seconds] [-o file] (-- <cmd...> | -d seconds)" >&2; exit 2; }
            out="$2"; shift 2 ;;
        -d) [ $# -ge 2 ] || { echo "usage: $0 [-i seconds] [-o file] (-- <cmd...> | -d seconds)" >&2; exit 2; }
            duration="$2"; mode="observe"; shift 2 ;;
        --) shift; break ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "usage: $0 [-i seconds] [-o file] (-- <cmd...> | -d seconds)" >&2; exit 2 ;;
    esac
done

case "${interval}" in
    ''|*[!0-9.]*|.*) echo "mem-trace: invalid interval '${interval}'" >&2; exit 2 ;;
esac
if [ "${mode}" = "wrap" ] && [ $# -eq 0 ]; then
    echo "usage: $0 [-i seconds] [-o file] (-- <cmd...> | -d seconds)" >&2; exit 2
fi
if [ "${mode}" = "observe" ] && [ -z "${duration}" ]; then
    echo "usage: $0 [-i seconds] [-o file] -d seconds" >&2; exit 2
fi
if [ -z "${out}" ]; then
    out="./mem-trace-$(date +%s).csv"
fi

is_linux=0
[ -r /proc/meminfo ] && is_linux=1

mem_total_kb() {
    if [ "${is_linux}" = 1 ]; then
        awk '/^MemTotal:/ {print $2}' /proc/meminfo
    else
        sysctl -n hw.memsize 2>/dev/null | awk '{printf "%d", $1/1024}'
    fi
}

mem_avail_kb() {
    if [ "${is_linux}" = 1 ]; then
        awk '/^MemAvailable:/ {print $2}' /proc/meminfo
    else
        # macOS fallback: free + inactive pages as rough "available".
        vm_stat 2>/dev/null | awk '
            /page size of/ { pagesz=$8 }
            /Pages free/ { gsub(/[^0-9]/,"",$3); free=$3 }
            /Pages inactive/ { gsub(/[^0-9]/,"",$3); inact=$3 }
            END { if (pagesz=="") pagesz=16384; printf "%d", (free+inact)*pagesz/1024 }'
    fi
}

top_procs() {
    # pid:rss_kb:comm triples of the 5 largest RSS processes, ';'-joined.
    # comm is truncated at ';'/',' so the CSV stays parseable.
    ps -eo pid=,rss=,comm= 2>/dev/null | awk '
        { sub(/^ +/, ""); comm=$3; gsub(/[;,]/, "_", comm);
          print $1 ":" $2 ":" comm "@" $2 }
    ' | sort -t@ -k2 -nr | head -n 5 | cut -d@ -f1 | paste -sd';' - || true
}

total="$(mem_total_kb)"
{
    echo "# mem-trace v1: interval_s=${interval} mode=${mode} mem_total_kb=${total}"
    echo "ts_utc,mem_total_kb,mem_available_kb,top5_pid:rss_kb:comm"
} > "${out}"

peak_used="0"
peak_line=""
samples="0"

sample_once() {
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    avail="$(mem_avail_kb)"
    tops="$(top_procs)"
    printf '%s,%s,%s,%s\n' "${now}" "${total}" "${avail}" "${tops}" >> "${out}"
    samples=$((samples + 1))
    if [ -n "${avail}" ] && [ -n "${total}" ]; then
        used=$((total - avail))
        if [ "${used}" -gt "${peak_used}" ]; then
            peak_used="${used}"
            peak_line="${now} avail=${avail}kB top=[${tops}]"
        fi
    fi
}

mark() {
    printf '# mark %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "${out}"
}

stop=0
trap 'stop=1' INT TERM

if [ "${mode}" = "wrap" ]; then
    mark "start: $*"
    "$@" &
    child=$!
    while kill -0 "${child}" 2>/dev/null && [ "${stop}" = 0 ]; do
        sample_once
        sleep "${interval}" || break
    done
    wait "${child}"
    rc=$?
    sample_once
    mark "end: exit=${rc}"
else
    mark "observe-start duration_s=${duration}"
    end=$(( $(date +%s) + duration ))
    while [ "$(date +%s)" -lt "${end}" ] && [ "${stop}" = 0 ]; do
        sample_once
        sleep "${interval}" || break
    done
    mark "observe-end samples=${samples}"
    rc=0
fi

{
    echo "mem-trace: ${samples} samples in ${out}"
    echo "mem-trace: peak_used_kb=${peak_used} at ${peak_line}"
} >&2
exit "${rc}"
