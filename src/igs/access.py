"""Fail-closed authorization policy for the public dashboard and service API."""
import math
import os
import secrets
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from igs.config import config_dir, settings_dir


class AccessPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    issuer: str = ''
    admin_emails: list[str] = Field(default_factory=list)
    viewer_emails: list[str] = Field(default_factory=list)
    admin_subjects: list[str] = Field(default_factory=list)
    viewer_subjects: list[str] = Field(default_factory=list)
    session_hours: int = Field(8, ge=1, le=24)
    idle_minutes: int = Field(30, ge=5, le=120)


def mode() -> Literal['oidc', 'local']:
    value = os.environ.get('IGS_AUTH_MODE', 'oidc')
    if value not in ('oidc', 'local'):
        raise ValueError('IGS_AUTH_MODE must be oidc or local')
    return value


def load_policy() -> AccessPolicy:
    path = settings_dir() / 'access.yaml'
    if not path.exists():
        path = config_dir() / 'access.yaml'
    return AccessPolicy.model_validate(yaml.safe_load(path.read_text()) or {})


def role_for(claims: dict, policy: AccessPolicy, now: float) -> str | None:
    """Only verified provider claims are accepted (call with Streamlit's st.user)."""
    if not claims.get('is_logged_in') or not policy.issuer:
        return None
    if claims.get('iss') != policy.issuer or not claims.get('sub'):
        return None
    try:
        exp, iat = float(claims['exp']), float(claims['iat'])
        auth_time = float(claims.get('auth_time', iat))
        if not all(math.isfinite(x) for x in (exp, iat, auth_time)):
            return None
        if exp <= now or iat > now + 60 or auth_time > now + 60:
            return None
        if now - min(iat, auth_time) >= policy.session_hours * 3600:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    amr = claims.get('amr')
    if not isinstance(amr, list) or 'mfa' not in amr:
        return None
    sub = claims['sub']
    email = str(claims.get('email', '')).strip().casefold()
    verified = claims.get('email_verified') is True
    for role, subjects, emails in [('admin', policy.admin_subjects, policy.admin_emails),
                                  ('viewer', policy.viewer_subjects, policy.viewer_emails)]:
        if sub in subjects or (verified and email and email in {e.casefold() for e in emails}):
            return role
    return None


def api_authorized(header: str) -> bool:
    expected = os.environ.get('IGS_API_TOKEN', '')
    # A separate random service credential, never the identity provider client secret.
    if len(expected) < 32:
        return False
    scheme, _, supplied = header.partition(' ')
    return scheme.lower() == 'bearer' and secrets.compare_digest(
        supplied.encode(), expected.encode())
