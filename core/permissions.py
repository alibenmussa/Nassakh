"""DRF permissions built on the role groups of `core.decorators`."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from core.decorators import ROLE_EDITOR, ROLE_PROOFREADER, has_role


class IsEditor(BasePermission):
    """Signed-in editors (and admins/superusers) only; for every mutating JSON endpoint."""

    message = "هذا الإجراء يتطلب صلاحية محرّر."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, ROLE_EDITOR)


class CanReview(BasePermission):
    """Signed-in reviewers: proofreaders, editors and admins (superusers always pass)."""

    message = "هذا الإجراء يتطلب صلاحية مراجع."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, ROLE_PROOFREADER, ROLE_EDITOR)
