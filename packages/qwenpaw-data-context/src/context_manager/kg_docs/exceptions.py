"""Exceptions for KG document operations."""
from __future__ import annotations


class DocError(Exception):
    """Base error for document operations."""


class DocAlreadyExistsError(DocError):
    """Upload rejected because the filename already exists."""


class DocNotFoundError(DocError):
    """Document does not exist in local storage."""
