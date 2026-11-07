"""Keep page filters when Streamlit removes off-page widget state."""

import streamlit as st


def restore(key: str, default, options=None) -> None:
    """Restore a widget's last manual choice before constructing it on this page."""
    value = st.session_state.get(f"saved_{key}", default)
    if options is not None:
        value = [item for item in value if item in options]
    st.session_state[key] = value


def save(key: str) -> None:
    st.session_state[f"saved_{key}"] = st.session_state[key]
