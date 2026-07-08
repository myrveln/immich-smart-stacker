from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Asset:
    """Represents an Immich asset (photo/video)."""

    id: str
    userId: str
    fileName: str
    fileCreatedAt: str
    updatedAt: str
    type: str
    stackId: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    isFavorite: bool = False

    @property
    def created_dt(self):
        """Parse fileCreatedAt as datetime."""
        return datetime.fromisoformat(self.fileCreatedAt.replace('Z', '+00:00'))

    def __repr__(self):
        return f"Asset({self.fileName}, {self.fileCreatedAt})"
