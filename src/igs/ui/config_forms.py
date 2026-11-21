"""Settings page forms for the config files (config.EDITABLE), access.yaml and the
password sign-in accounts (igs.accounts).

Each form shows a file's current values (config/<name> with the values changed here on
top) as fields, with the file's own comments as help, and saves what differs from
config/<name> to data/settings/<name> (igs.settings.save). Nothing is saved unless the
whole file still validates.
"""
from __future__ import annotations

import math
import re
import time

import pandas as pd
import streamlit as st
import yaml
from pydantic import ValidationError

from igs import settings
from igs.config import _load_yaml, config_dir, effective_yaml, overrides
from igs.ui import auth

# Words written in full or in capitals in field labels.
WORDS = {"pct": "%", "inr": "₹", "bps": "bps", "cr": "₹ crore", "ist": "IST", "sme": "SME",
         "asm": "ASM", "gsm": "GSM", "ai": "AI", "stt": "STT", "sebi": "SEBI", "gst": "GST",
         "dp": "DP", "url": "URL", "ic": "IC", "nse": "NSE", "nbfc": "NBFC", "ev": "EV",
         "ebitda": "EBITDA", "txn": "transaction", "dma": "DMA", "mfa": "MFA",
         "pp": "percentage points", "mtok": "million tokens", "id": "ID", "llm": "LLM",
         "ui": "UI"}
KEY = re.compile(r"^(\s*)(?:- )?([A-Za-z0-9_]+):(.*)$")


def label(key: str) -> str:
    """'stt_sell_pct' -> 'STT sell %'."""
    words = [WORDS.get(w, w) for w in str(key).split("_")]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def comments(text: str) -> dict[str, str]:
    """Help text per dotted key of a YAML file: the comment lines just above the key and
    its comment on the same line. A blank line ends a comment block."""
    out: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            pending = []
            continue
        if stripped.startswith("#"):
            note = stripped.lstrip("#").strip().strip("-=").strip()   # rules: "# ---- x"
            if note:
                pending.append(note)
            continue
        match = KEY.match(line)
        if not match:
            pending = []
            continue
        indent, key, rest = len(match.group(1)), match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        value, _, inline = rest.partition(" #")
        note = " ".join(pending + ([inline.strip()] if inline.strip() else []))
        if note:
            out[path] = note
        pending = []
        if not value.strip():
            stack.append((indent, key))
    return out


def _lower(title: str) -> str:
    """'Universe settings' -> 'universe settings'; 'NSE checks' stays."""
    return title[:1].lower() + title[1:] if title[1:2].islower() or title[1:2] == " " \
        else title


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or (
        isinstance(value, float) and math.isnan(value))


def _items(text: str, like: list):
    """A comma-separated field back into a list, typed like the list it showed."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if like and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in like):
        return [int(p) if re.fullmatch(r"-?\d+", p) else float(p) for p in parts]
    return parts


def rows_out(df: pd.DataFrame, list_columns: set[str]) -> list[dict]:
    """A table of rows (e.g. feeds) back into a list of mappings: empty cells are left
    out, and comma-separated cells of list columns become lists."""
    out = []
    for row in df.to_dict("records"):
        item = {}
        for k, v in row.items():
            if _empty(v):
                continue
            item[k] = _items(str(v), []) if k in list_columns else v
            if item[k] == []:
                del item[k]
        if item:
            out.append(item)
    return out


def _field(value, path: tuple[str, ...], prefix: str, notes: dict, errors: list,
           local: bool):
    name = ".".join(path)
    key = prefix + name
    help_ = (notes.get(name, "") + f" (config key: {name})").strip()
    text_label = label(path[-1])
    if isinstance(value, bool):
        return st.checkbox(text_label, value=value, key=key, help=help_, disabled=not local)
    if isinstance(value, int):
        return int(st.number_input(text_label, value=value, step=1, key=key, help=help_,
                                   disabled=not local))
    if isinstance(value, float):
        text = st.text_input(text_label, value=repr(value), key=key, help=help_,
                             disabled=not local)
        try:
            return float(text)
        except ValueError:
            errors.append(f"{text_label}: enter a number")
            return value
    if isinstance(value, str):
        return st.text_input(text_label, value=value, key=key, help=help_, disabled=not local)
    if value is None:
        text = st.text_input(text_label, value="", key=key, disabled=not local,
                             help=help_ + " Empty means none.")
        return yaml.safe_load(text) if text.strip() else None
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        lists = {k for row in value for k, v in row.items() if isinstance(v, list)}
        df = pd.DataFrame([{k: ", ".join(map(str, v)) if isinstance(v, list) else v
                            for k, v in row.items()} for row in value])
        st.caption(f"{text_label}: {notes.get(name, '')} Add or delete rows below; list "
                   "cells are comma-separated.".strip())
        edited = st.data_editor(df, num_rows="dynamic", key=key, width="stretch",
                                hide_index=True, disabled=not local)
        return rows_out(edited, lists)
    if isinstance(value, list):
        text = st.text_input(text_label, value=", ".join(map(str, value)), key=key,
                             help=help_ + " Comma-separated.", disabled=not local)
        try:
            return _items(text, value)
        except ValueError:
            errors.append(f"{text_label}: enter numbers separated by commas")
            return value
    if isinstance(value, dict):
        if len(path) == 1:
            with st.expander(text_label):
                if notes.get(name):
                    st.caption(notes[name])
                return _fields(value, path, prefix, notes, errors, local)
        st.markdown(f"**{text_label}**")
        if notes.get(name):
            st.caption(notes[name])
        return _fields(value, path, prefix, notes, errors, local)
    st.text(f"{text_label}: {value!r} (not editable here)")
    return value


def _fields(values: dict, path: tuple[str, ...], prefix: str, notes: dict, errors: list,
            local: bool) -> dict:
    return {k: _field(v, (*path, str(k)), prefix, notes, errors, local)
            for k, v in values.items()}


def editor(name: str, title: str, *, local: bool, intro: str = "") -> None:
    """A form for config/<name>: its current values, saved over the defaults."""
    st.subheader(title)
    changed = overrides(name)
    where = (f"`config/{name}`, with {_count(changed)} value(s) changed here (kept in "
             f"`data/settings/{name}`)." if changed else f"`config/{name}`, as shipped.")
    st.caption((intro + " " if intro else "") + "Values from " + where)
    notes = comments((config_dir() / name).read_text(encoding="utf-8"))
    errors: list[str] = []
    for kind, text in st.session_state.pop(f"cfg_flash:{name}", []):
        getattr(st, kind)(text)
    with st.form(f"cfg_form:{name}"):
        values = _fields(effective_yaml(name), (), f"cfg:{name}:", notes, errors, local)
        saved = st.form_submit_button(f"Save {_lower(title)}", disabled=not local)
    if saved and local:
        auth.require_access(admin=True)
        if errors:
            st.error("Not saved: " + "; ".join(errors))
        else:
            try:
                settings.save(name, values)
            except (ValidationError, ValueError) as exc:
                st.error(f"Not saved: {_reason(exc)}")
            else:
                st.session_state[f"cfg_flash:{name}"] = [
                    ("success", f"Saved. Everything that reads config/{name} uses it "
                                "from its next run.")]
                st.rerun()
    if local and changed and st.button(f"Reset {_lower(title)} to config/{name}",
                                       key=f"cfg_reset:{name}"):
        auth.require_access(admin=True)
        settings.reset(name)
        st.session_state[f"cfg_flash:{name}"] = [("success", f"Back to config/{name}.")]
        st.rerun()


def _count(changed: dict) -> int:
    return sum(_count(v) if isinstance(v, dict) else 1 for v in changed.values())


def _reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'file'}: {e['msg']}"
                         for e in exc.errors())
    return str(exc)


def _oidc() -> bool:
    from igs.access import mode
    return mode() == "oidc"


def access_editor(*, local: bool) -> None:
    """Who may sign in, and for how long. The file saved here replaces
    config/access.yaml whole (igs.access.load_policy)."""
    from igs.access import access_decision, load_policy

    if not _oidc():
        accounts_editor(local=local)
        return
    st.subheader("Access")
    own = settings.path_for("access.yaml")
    st.caption("Who can sign in to this app through your identity provider, as an "
               "administrator or a viewer, and how long a session lasts. Values from "
               + ("`data/settings/access.yaml`, which replaces `config/access.yaml`."
                  if own.is_file() else "`config/access.yaml`, as shipped.")
               + " Sign-ins use the new values from the next page load.")
    for kind, text in st.session_state.pop("cfg_flash:access.yaml", []):
        getattr(st, kind)(text)
    try:
        policy = load_policy()
    except (ValidationError, ValueError, OSError) as exc:
        st.error(f"The access policy can't be read: {exc}")
        return
    with st.form("cfg_form:access.yaml"):
        issuer = st.text_input("Issuer", value=policy.issuer, disabled=not local,
                               help="Your identity provider's issuer URL, exactly as it "
                                    "appears in its tokens.")
        mfa = st.checkbox("Require multi-factor sign-in", value=policy.require_mfa,
                          disabled=not local)
        lists = {}
        for field, text in (("admin_emails", "Administrator emails"),
                            ("viewer_emails", "Viewer emails"),
                            ("admin_subjects", "Administrator subject IDs"),
                            ("viewer_subjects", "Viewer subject IDs")):
            lists[field] = _items(st.text_input(
                text, value=", ".join(getattr(policy, field)), disabled=not local,
                help="Comma-separated. Emails count only when the provider has "
                     "verified them; subject IDs are the provider's own account IDs."), [])
        hours = st.number_input("Session length (hours)", 1, 24, value=policy.session_hours,
                                disabled=not local)
        idle = st.number_input("Sign out after idle (minutes)", 5, 120,
                               value=policy.idle_minutes, disabled=not local)
        saved = st.form_submit_button("Save access", disabled=not local)
    if saved and local:
        auth.require_access(admin=True)
        values = {"issuer": issuer.strip(), "require_mfa": mfa, **lists,
                  "session_hours": int(hours), "idle_minutes": int(idle)}
        keep = None if auth.local_mode() else (
            lambda p: access_decision(dict(st.user), p, time.time())[0] == "admin")
        try:
            settings.save_access(values, keep_admin=keep)
        except (ValidationError, ValueError) as exc:
            st.error(f"Not saved: {_reason(exc)}")
        else:
            st.session_state["cfg_flash:access.yaml"] = [("success", "Access saved.")]
            st.rerun()
    if local and own.is_file() and st.button("Reset access to config/access.yaml",
                                             key="cfg_reset:access.yaml"):
        auth.require_access(admin=True)
        if not auth.local_mode() and access_decision(
                dict(st.user), load_policy_from(config_dir()), time.time())[0] != "admin":
            st.error("Not reset: config/access.yaml does not make you an administrator.")
        else:
            own.unlink()
            st.session_state["cfg_flash:access.yaml"] = [("success",
                                                          "Back to config/access.yaml.")]
            st.rerun()


def load_policy_from(directory):
    from igs.access import AccessPolicy
    return AccessPolicy.model_validate(_load_yaml("access.yaml", directory) or {})


def accounts_editor(*, local: bool) -> None:
    """Accounts for password sign-in and how long a session lasts."""
    import datetime as dt

    from igs import accounts
    from igs.access import load_policy
    from igs.timeutil import IST

    st.subheader("Access")
    st.caption("Who can sign in with a password: administrators can change everything, "
               "viewers can only read. Passwords are stored as salted scrypt hashes in "
               "`data/settings/accounts.sqlite3`, which can check a password but never give "
               "it back, so a forgotten one is replaced, not recovered. On the server, "
               "`uv run igs users` does the same from the command line.")
    for kind, text in st.session_state.pop("acct_flash", []):
        getattr(st, kind)(text)
    try:
        policy = load_policy()
        accounts.end_expired(policy.session_hours, policy.idle_minutes)
        rows = accounts.users()
    except Exception as exc:  # noqa: BLE001
        st.error(f"The accounts can't be read: {exc}")
        return

    def when(t):
        return dt.datetime.fromtimestamp(t, IST).strftime("%d %b %Y %H:%M") if t else "never"
    if rows:
        st.dataframe(pd.DataFrame([{"Account": r["username"], "Role": r["role"],
                                    "Last sign-in (IST)": when(r["last_sign_in_at"]),
                                    "Open sessions": r["sessions"]} for r in rows]),
                     hide_index=True, width="stretch")
    else:
        st.info("No accounts yet: add an administrator below, or with "
                "`uv run igs users add NAME --role admin`.")
    me = auth.signed_in_name()
    names = [r["username"] for r in rows]

    def done(text: str) -> None:
        st.session_state["acct_flash"] = [("success", text)]
        st.rerun()

    def two(prefix: str):
        first = st.text_input("Password", type="password", key=f"{prefix}_pw",
                              disabled=not local, autocomplete="new-password",
                              help=f"At least {accounts.MIN_LENGTH} characters, without the "
                                   "account name. A passphrase of four or more random words "
                                   "is strong and easy to type.")
        again = st.text_input("The same password again", type="password",
                              key=f"{prefix}_pw2", disabled=not local,
                              autocomplete="new-password")
        return first, again

    left, right = st.columns(2, gap="large")
    with left, st.form("acct_add", clear_on_submit=True):
        st.markdown("**Add an account**")
        name = st.text_input("Account name", key="acct_add_name", disabled=not local)
        role = st.radio("Role", accounts.ROLES, horizontal=True, key="acct_add_role",
                        index=1, disabled=not local,
                        format_func={"admin": "Administrator", "viewer": "Viewer"}.get)
        first, again = two("acct_add")
        added = st.form_submit_button("Add account", disabled=not local)
    if added and local:
        auth.require_access(admin=True)
        try:
            if first != again:
                raise accounts.AccountError("The two passwords differ.")
            done(f"Added {accounts.add(name, first, role)}. It can sign in now.")
        except accounts.AccountError as exc:
            st.error(f"Not added: {exc}")
    with right, st.form("acct_password", clear_on_submit=True):
        st.markdown("**Change a password**")
        who = st.selectbox("Account", names, key="acct_pw_name", disabled=not local)
        first, again = two("acct_pw")
        changed = st.form_submit_button("Change password", disabled=not local or not names)
    if changed and local and who:
        auth.require_access(admin=True)
        try:
            if first != again:
                raise accounts.AccountError("The two passwords differ.")
            accounts.set_password(who, first, keep=auth.session_token() if who == me else None)
            done(f"Password changed. {who}'s other sessions have ended."
                 if who == me else f"Password changed. {who}'s sessions have ended.")
        except accounts.AccountError as exc:
            st.error(f"Not changed: {exc}")

    others = [n for n in names if n != me]
    with left, st.form("acct_remove"):
        st.markdown("**Remove an account**")
        gone = st.selectbox("Account", others, key="acct_rm_name", disabled=not local,
                            help="Your own account can't be removed while you use it.")
        sure = st.checkbox("Remove it and end its sessions", key="acct_rm_sure",
                           disabled=not local)
        removed = st.form_submit_button("Remove account", disabled=not local or not others)
    if removed and local and gone:
        auth.require_access(admin=True)
        if not sure:
            st.error("Not removed: tick the box to confirm.")
        else:
            try:
                accounts.remove(gone)
                done(f"Removed {gone}.")
            except accounts.AccountError as exc:
                st.error(f"Not removed: {exc}")
    with right, st.form("acct_session"):
        st.markdown("**Sessions**")
        hours = st.number_input("Session length (hours)", 1, 24, value=policy.session_hours,
                                key="acct_hours", disabled=not local)
        idle = st.number_input("Sign out after idle (minutes)", 5, 120,
                               value=policy.idle_minutes, key="acct_idle", disabled=not local)
        timed = st.form_submit_button("Save sessions", disabled=not local)
    if timed and local:
        auth.require_access(admin=True)
        settings.save_access(policy.model_dump() | {"session_hours": int(hours),
                                                    "idle_minutes": int(idle)})
        done("Saved. Sessions follow it from their next page or action.")
