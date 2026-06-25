import os
import sys
import tracemalloc
import time
import objgraph
import psutil
from pympler import muppy, summary
from fastapi import FastAPI, APIRouter
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
import html
import gc


# Start tracemalloc upon import so it begins tracking immediately
tracemalloc.start()

# Using an APIRouter makes this easy to "inject" into any existing FastAPI app
memory_router = APIRouter(prefix="/debug/memory", tags=["Memory Profiler"])

# Cache Variables to prevent Server Overload
CACHED_DASHBOARD_HTML = ""
LAST_SNAPSHOT_TIME = 0
CACHE_TTL_SECONDS = 5.0  # Only take a deep snapshot every 5 seconds

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
    """Forces garbage collection, then clears tracemalloc memory to reset the baseline."""
    # 1. Sweep away any unreferenced dead objects first
    gc.collect()

    # 2. Reset the tracking baseline to zero
    tracemalloc.clear_traces()

    return {"status": "Garbage collected and Tracemalloc baseline reset."}


@memory_router.get("/dashboard/html", response_class=HTMLResponse)
def memory_dashboard_html(refresh: int = 30):
    """
    Renders the memory dashboard.
    Use ?refresh=X to set the auto-refresh interval in seconds (default: 30).
    """
    global CACHED_DASHBOARD_HTML, LAST_SNAPSHOT_TIME

    current_time = time.time()

    # 1. We use the user's requested refresh rate as our Cache TTL
    if current_time - LAST_SNAPSHOT_TIME < refresh and CACHED_DASHBOARD_HTML:
        # We need to dynamically inject their specific timer into the cached HTML
        return HTMLResponse(content=CACHED_DASHBOARD_HTML.replace(
            "__REFRESH_TIME__", str(refresh)
        ))
    else:
        # Your excellent concurrency fix
        LAST_SNAPSHOT_TIME = current_time

    # 2. Gather Metrics (The heavy lifting)
    process = psutil.Process(os.getpid())
    os_rss_mb = round(process.memory_info().rss / (1024 * 1024), 2)

    snapshot = tracemalloc.take_snapshot()
    top_allocations = snapshot.statistics('lineno')[:10]

    all_objects = muppy.get_objects()
    sum1 = summary.summarize(all_objects)
    memory_by_type = [line.strip() for line in summary.format_(sum1, limit=15)]

    # 3. Format Rows
    alloc_rows = "".join([
        f"<tr><td>{html.escape(str(stat.traceback[0]))}</td><td>{stat.size / 1024:.1f} KB</td><td>{stat.count}</td></tr>"
        for stat in top_allocations
    ])

    type_rows = ""
    for line in memory_by_type[2:]:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) == 3:
            type_rows += f"<tr><td>{html.escape(parts[0])}</td><td>{parts[1]}</td><td>{parts[2]}</td></tr>"

    # 4. Build the HTML template with a dynamic timer placeholder
    CACHED_DASHBOARD_HTML = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Memory Profiler Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f9; color: #333; margin: 20px; }}
            h1, h2 {{ color: #2c3e50; }}
            .header-container {{ display: flex; justify-content: space-between; align-items: center; }}
            .metric-box {{ background: #3498db; color: white; padding: 15px; border-radius: 5px; font-size: 20px; margin-bottom: 20px; }}
            .refresh-box {{ background: #2ecc71; color: white; padding: 10px; border-radius: 5px; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 30px; background: white; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            th, td {{ text-align: left; padding: 12px; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #2c3e50; color: white; }}
            tr:hover {{ background-color: #f1f1f1; }}
        </style>
        <script>
            // The template uses a placeholder so we can swap it out if the user changes the URL param
            let timeLeft = __REFRESH_TIME__; 
            setInterval(() => {{
                timeLeft--;
                document.getElementById("timer").innerText = timeLeft;
                if(timeLeft <= 0) {{
                    window.location.reload();
                }}
            }}, 1000);
        </script>
    </head>
    <body>
        <div class="header-container">
            <h1>Python Application Memory Profile</h1>
            <div class="refresh-box">Auto-refreshing in <span id="timer">__REFRESH_TIME__</span>s</div>
        </div>

        <div class="metric-box">
            <strong>Total OS RSS Memory:</strong> {os_rss_mb} MB
        </div>

        <h2>1. Top Allocations (Where memory was requested)</h2>
        <table>
            <tr><th>File & Line Number</th><th>Total Size</th><th>Object Count</th></tr>
            {alloc_rows}
        </table>

        <h2>2. Objects in Memory (What is currently alive)</h2>
        <table>
            <tr><th>Object Type</th><th>Count</th><th>Total Size</th></tr>
            {type_rows}
        </table>
    </body>
    </html>
    """

    return HTMLResponse(content=CACHED_DASHBOARD_HTML.replace("__REFRESH_TIME__", str(refresh)))


