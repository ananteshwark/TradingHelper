"""The dashboard's web server for password sign-in: Streamlit's app.py with the /signin
page, and a check that sends a browser without a session there first (igs.ui.signin).
`igs ui` runs this file instead of app.py when IGS_AUTH_MODE is password."""
import streamlit as st
from starlette.middleware import Middleware

from igs.ui import backup_routes
from igs.ui.signin import SignInFirst, routes

# /signin, and the backups' download and upload (too large for Streamlit's own widgets).
app = st.App('app.py', routes=routes() + backup_routes.routes(),
             middleware=[Middleware(SignInFirst)])
