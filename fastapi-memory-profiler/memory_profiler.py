import os
import sys
import tracemalloc
import time, datetime
import objgraph
import psutil
from pympler import muppy, summary
from fastapi import FastAPI, APIRouter
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
import html
import threading
import gc

import json
from collections import deque


# Start tracemalloc upon import so it begins tracking immediately
tracemalloc.start()

# Using an APIRouter makes this easy to "inject" into any existing FastAPI app
memory_router = APIRouter(prefix="/debug/memory", tags=["Memory Profiler"])

# Cache Variables to prevent Server Overload
CACHED_DASHBOARD_HTML = ""
LAST_SNAPSHOT_TIME = 0
CACHE_TTL_SECONDS = 5.0  # Only take a deep snapshot every 5 seconds
# Track when the module/app started up as a fallback,
# but psutil.Process().create_time() is even more accurate.
APP_START_TIME = time.time()

# Dictionary to hold the previous snapshot's counts for delta tracking
PREVIOUS_METRICS = {
    "threads": 0,
    "pool_threads": 0,
    "futures": 0,
    "objects": {}
}

# Bounded history queue. 4320 items = 3 days at 1 snapshot per minute.
MEMORY_HISTORY = deque(maxlen=4320)

# def get_trend_indicator(current: int, previous: int) -> str:
#     """Returns a color-coded HTML arrow if the value changed."""
#     if previous == 0:
#         return "" # Don't show trend on the very first load
#     if current > previous:
#         return ' <span style="color: #e74c3c; font-size: 0.9em; margin-left: 5px;">&#9650;</span>' # Red Up
#     elif current < previous:
#         return ' <span style="color: #2ecc71; font-size: 0.9em; margin-left: 5px;">&#9660;</span>' # Green Down
#     return "" # No change

def background_memory_collector():
    """Runs continuously in the background to record RSS memory."""
    while True:
        process = psutil.Process(os.getpid())
        rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        timestamp = datetime.datetime.now().strftime("%m-%d %H:%M")

        MEMORY_HISTORY.append((timestamp, rss_mb))
        # Sleep for 60 seconds between historical snapshots
        time.sleep(60)


# Start the background collector the moment this module is imported
collector_thread = threading.Thread(target=background_memory_collector, daemon=True)
collector_thread.start()


def get_trend_indicator(current: int, previous: int) -> str:
    """Returns a color-coded HTML arrow if the value changed."""
    if previous == 0: return ""
    if current > previous:
        return ' <span style="color: #e74c3c; font-size: 0.9em; margin-left: 5px;">&#9650;</span>'
    elif current < previous:
        return ' <span style="color: #2ecc71; font-size: 0.9em; margin-left: 5px;">&#9660;</span>'
    return ""


@memory_router.get("/dashboard")
def memory_dashboard():
    """Aggregates system RSS, Tracemalloc locations, and Pympler object types into one view."""
    # 1. System Memory (OS level)
    process = psutil.Process(os.getpid())
    os_rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)

    # 2. Tracemalloc (Where it was allocated)
    snapshot = tracemalloc.take_snapshot()
    top_allocations = [str(stat) for stat in snapshot.statistics('lineno')[:10]]

    # 3. Pympler (What types are in memory)
    all_objects = muppy.get_objects()
    sum1 = summary.summarize(all_objects)
    memory_by_type = [line.strip() for line in summary.format_(sum1, limit=15)]

    return {
        "1_system_rss_mb": os_rss_mb,
        "2_leaky_locations_top_10": top_allocations,
        "3_objects_by_type_top_15": memory_by_type
    }


@memory_router.get("/objgraph/{obj_type}")
def generate_object_graph_dynamic(obj_type: str):
    """
    Dynamically generates a reference graph for any object type.
    Examples: /debug/memory/objgraph/set, /debug/memory/objgraph/dict
    """
    # Find all objects matching the requested string
    all_objs = objgraph.by_type(obj_type)

    if not all_objs:
        return {"status": f"No objects of type '{obj_type}' found in memory."}

    # Heuristic: We want to graph the largest object of this type, as it's the most likely leak.
    # We use sys.getsizeof instead of len() because len() fails on objects that aren't collections.
    try:
        largest_obj = max(all_objs, key=sys.getsizeof)
    except Exception as e:
        # Fallback if sizing fails for some obscure C-extension type
        largest_obj = all_objs[0]

    file_path = f"leaked_{obj_type}_backrefs.png"

    # max_depth=7 is deep enough to pierce connection pools and global caches
    objgraph.show_backrefs([largest_obj], max_depth=7, filename=file_path)

    return FileResponse(file_path)


@memory_router.get("/clear-tracemalloc")
def clear_tracemalloc_stats():
    """Clears tracemalloc memory to reset the baseline."""
    tracemalloc.clear_traces()
    gc.collect()
    return {"status": "Tracemalloc baseline reset."}


@memory_router.get("/chart.umd.min.js", include_in_schema=False)
def serve_chart_js():
    """Serves the local Chart.js library for air-gapped environments."""
    # Find the directory where memory_profiler.py is installed
    current_dir = os.path.dirname(os.path.realpath(__file__))
    js_path = os.path.join(current_dir, "static", "chart.umd.min.js")

    return FileResponse(js_path, media_type="application/javascript")


@memory_router.get("/dashboard/html", response_class=HTMLResponse)
def memory_dashboard_html(refresh: int = 30):
    global CACHED_DASHBOARD_HTML, LAST_SNAPSHOT_TIME, PREVIOUS_METRICS
    current_time = time.time()

    if current_time - LAST_SNAPSHOT_TIME < refresh and CACHED_DASHBOARD_HTML:
        return HTMLResponse(content=CACHED_DASHBOARD_HTML.replace("__REFRESH_TIME__", str(refresh)))
    else:
        LAST_SNAPSHOT_TIME = current_time

    # --- 1. SYSTEM & PROCESS METRICS ---
    process = psutil.Process(os.getpid())
    os_rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)
    total_sys_ram_gb = round(psutil.virtual_memory().total / (1024 * 1024 * 1024), 2)
    cpu_count = os.cpu_count() or 1

    try:
        uptime_seconds = int(current_time - process.create_time())
    except Exception:
        uptime_seconds = int(current_time - APP_START_TIME)

    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))

    # --- 2. EXTRACT HISTORICAL DATA FOR CHART.JS ---
    # Convert the deque to parallel lists and serialize to JSON for the frontend
    history_list = list(MEMORY_HISTORY)
    # If the app just started, give it a placeholder so the chart isn't empty
    if not history_list:
        history_list = [(datetime.datetime.now().strftime("%m-%d %H:%M"), os_rss_mb)]

    chart_labels_json = json.dumps([item[0] for item in history_list])
    chart_data_json = json.dumps([item[1] for item in history_list])

    # --- 3. CONCURRENCY & SNAPSHOTS ---
    snapshot = tracemalloc.take_snapshot()
    top_allocations = snapshot.statistics('lineno')[:10]
    all_objects = muppy.get_objects()
    sum1 = summary.summarize(all_objects)
    memory_by_type = [line.strip() for line in summary.format_(sum1, limit=15)]

    active_threads = threading.enumerate()
    thread_count = len(active_threads)
    pool_threads = sum(1 for t in active_threads if "ThreadPoolExecutor" in t.name)

    future_objs = [obj for obj in gc.get_objects() if type(obj).__name__ == 'Future']
    future_count = len(future_objs)

    avg_future_refcount = 0
    if future_count > 0:
        avg_future_refcount = round(sum(sys.getrefcount(f) - 1 for f in future_objs) / future_count, 1)

    del future_objs
    del all_objects

    # --- 4. CALCULATE TRENDS & FORMAT HTML ---
    thread_trend = get_trend_indicator(thread_count, PREVIOUS_METRICS["threads"])
    pool_trend = get_trend_indicator(pool_threads, PREVIOUS_METRICS["pool_threads"])
    future_trend = get_trend_indicator(future_count, PREVIOUS_METRICS["futures"])

    PREVIOUS_METRICS["threads"] = thread_count
    PREVIOUS_METRICS["pool_threads"] = pool_threads
    PREVIOUS_METRICS["futures"] = future_count

    alloc_rows = "".join([
        f"<tr><td>{html.escape(str(stat.traceback[0]))}</td><td>{stat.size / 1024:.1f} KB</td><td>{stat.count}</td></tr>"
        for stat in top_allocations
    ])

    type_rows = ""
    current_objects_state = {}
    for line in memory_by_type[2:]:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) == 3:
            obj_type = parts[0]
            count = int(parts[1]) if parts[1].isdigit() else 0
            trend_arrow = get_trend_indicator(count, PREVIOUS_METRICS["objects"].get(obj_type, 0))
            current_objects_state[obj_type] = count
            type_rows += f"<tr><td>{html.escape(obj_type)}</td><td>{parts[1]}{trend_arrow}</td><td>{parts[2]}</td></tr>"

    PREVIOUS_METRICS["objects"] = current_objects_state

    thread_rows = f"""
        <tr><td>Total Active OS Threads</td><td>{thread_count}{thread_trend}</td><td>-</td></tr>
        <tr><td>ThreadPoolExecutor Threads</td><td>{pool_threads}{pool_trend}</td><td>-</td></tr>
        <tr><td>Active 'Future' Objects</td><td>{future_count}{future_trend}</td><td>Avg Refs: {avg_future_refcount}</td></tr>
    """

    # --- 5. BUILD THE GRID VIEW TEMPLATE ---
    CACHED_DASHBOARD_HTML = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Memory Profiler Dashboard</title>
        <script src="/debug/memory/chart.umd.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f9; color: #333; margin: 20px; }}
            h1, h2 {{ color: #2c3e50; font-size: 22px; margin-top: 25px; }}
            .header-container {{ display: flex; justify-content: space-between; align-items: center; }}
            .refresh-box {{ background: #2ecc71; color: white; padding: 10px; border-radius: 5px; font-size: 14px; }}

            .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 25px; }}
            .metric-card {{ background: white; padding: 15px; border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.06); border-left: 5px solid #3498db; }}
            .metric-card.accent {{ border-left-color: #9b59b6; }}
            .metric-card.time {{ border-left-color: #e67e22; }}
            .metric-label {{ font-size: 12px; color: #7f8c8d; text-transform: uppercase; font-weight: bold; margin-bottom: 5px; }}
            .metric-value {{ font-size: 24px; color: #2c3e50; font-weight: bold; }}

            .chart-container {{ background: white; padding: 20px; border-radius: 6px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); margin-bottom: 25px; height: 300px; }}

            table {{ width: 100%; border-collapse: collapse; margin-bottom: 25px; background: white; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }}
            th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #2c3e50; color: white; }}
            tr:hover {{ background-color: #f1f1f1; }}
        </style>
        <script>
            let timeLeft = __REFRESH_TIME__; 
            setInterval(() => {{
                timeLeft--;
                document.getElementById("timer").innerText = timeLeft;
                if(timeLeft <= 0) {{ window.location.reload(); }}
            }}, 1000);
        </script>
    </head>
    <body>
        <div class="header-container">
            <h1>Application Performance & Memory Diagnostics</h1>
            <div class="refresh-box">Auto-refreshing in <span id="timer">__REFRESH_TIME__</span>s</div>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Process Memory (RSS)</div>
                <div class="metric-value">{os_rss_mb} MB</div>
            </div>
            <div class="metric-card accent">
                <div class="metric-label">Total Host RAM</div>
                <div class="metric-value">{total_sys_ram_gb} GB</div>
            </div>
            <div class="metric-card accent">
                <div class="metric-label">Available CPU Cores</div>
                <div class="metric-value">{cpu_count} Cores</div>
            </div>
            <div class="metric-card time">
                <div class="metric-label">Application Uptime</div>
                <div class="metric-value">{uptime_str}</div>
            </div>
        </div>

        <div class="chart-container">
            <canvas id="memoryChart"></canvas>
        </div>

        <h2>1. Concurrency & Reference Stats</h2>
        <table>
            <tr><th>Metric</th><th>Count</th><th>Reference Status</th></tr>
            {thread_rows}
        </table>

        <h2>2. Top Allocations (Active Track)</h2>
        <table>
            <tr><th>File & Line Number</th><th>Total Size</th><th>Object Count</th></tr>
            {alloc_rows}
        </table>

        <h2>3. Objects In Memory Heap</h2>
        <table>
            <tr><th>Object Type</th><th>Count</th><th>Total Size</th></tr>
            {type_rows}
        </table>

        <script>
            const ctx = document.getElementById('memoryChart').getContext('2d');
            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {chart_labels_json},
                    datasets: [{{
                        label: 'OS RSS Memory (MB)',
                        data: {chart_data_json},
                        borderColor: '#e74c3c',
                        backgroundColor: 'rgba(231, 76, 60, 0.2)',
                        borderWidth: 2,
                        pointRadius: 1,
                        fill: true,
                        tension: 0.2
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        y: {{ beginAtZero: false, title: {{ display: true, text: 'Memory (MB)' }} }},
                        x: {{ title: {{ display: true, text: 'Time' }} }}
                    }},
                    animation: {{ duration: 0 }} /* Disable animation for cleaner auto-refresh */
                }}
            }});
        </script>
    </body>
    </html>
    """

    return HTMLResponse(content=CACHED_DASHBOARD_HTML.replace("__REFRESH_TIME__", str(refresh)))
