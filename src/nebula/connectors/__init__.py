"""Source connectors. Google Drive is the only one."""

from __future__ import annotations

from typing import Any

from nebula.connectors.base import Change, ChangeBatch, Connector, FetchedItem
from nebula.connectors.gdrive import GoogleDriveConnector


def get_connector(kind: str, config: dict[str, Any]) -> Connector:
    """A connector for one collection. Raises ValueError for an unknown kind or bad config."""
    if kind != GoogleDriveConnector.kind:
        raise ValueError(f"unknown connector kind {kind!r}; only 'gdrive' is supported")
    return GoogleDriveConnector(config or {})


__all__ = ["Change", "ChangeBatch", "Connector", "FetchedItem", "GoogleDriveConnector", "get_connector"]
