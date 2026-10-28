"""Canonical opaque revision of an upstream role configuration."""
import re


def validate_role_revision(value):
    if not isinstance(value, str) or re.fullmatch(r'qwenpaw-role-v1:[0-9a-f]{64}', value) is None:
        raise ValueError('role_revision_invalid')
    return value
