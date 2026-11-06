"""Storage information on Settings > Maintenance."""
import shutil
from pathlib import Path

import streamlit as st

from igs.storage import format_bytes, inventory
from igs.timeutil import IST, utc_now


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _inventory():
    return inventory(Path(Path.cwd().anchor)), utc_now()


def render() -> None:
    st.subheader("Server disk space")
    root = Path(Path.cwd().anchor)
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        st.warning(f"Disk space is unavailable: {exc}")
        return
    with st.container(horizontal=True):
        st.metric("Available", format_bytes(usage.free))
        st.metric("Used", format_bytes(usage.used))
        st.metric("Total", format_bytes(usage.total))
    st.caption(f"Server filesystem: {root}. Available space is checked on each page refresh.")
    if st.button("Refresh storage", key="maint_storage_refresh"):
        _inventory.clear()
    with st.spinner("Finding the largest files and folders…"):
        try:
            report, checked = _inventory()
        except OSError as exc:
            st.warning(f"Storage scan is unavailable: {exc}")
            return
    st.caption(f"Scanned {checked.astimezone(IST):%d %b %Y %H:%M:%S} IST. "
               "Results are cached for five minutes. Sizes are disk space occupied; "
               "folder totals include their contents and can overlap. "
               "Links and other mounted filesystems are excluded.")
    if report["skipped"] or report["timed_out"]:
        reason = f"{report['skipped']} inaccessible or changing entries were skipped."
        if report["timed_out"]:
            reason += " The 30-second scan limit was reached."
        st.warning("Partial ranking of files readable by the app. " + reason
                   + " Protected folders, such as the database and root-owned backups, "
                     "may be missing; folder sizes may be incomplete.")
    for key, label in (("files", "Top 3 largest files"), ("folders", "Top 3 largest folders")):
        st.markdown(f"**{label}**")
        rows = [{"Path": path, "Size": format_bytes(size)} for size, path in report[key]]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.caption("No readable entries found.")
