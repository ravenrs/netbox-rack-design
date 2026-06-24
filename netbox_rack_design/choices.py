"""
Choice sets for NetBox Rack Design.

Uses NetBox's built-in ``ChoiceSet`` (utilities.choices), per the plugin
development guide. Defining ``key`` lets administrators extend/replace these
choices via ``FIELD_CHOICES`` in configuration.py.
"""

from utilities.choices import ChoiceSet


class DesignStatusChoices(ChoiceSet):
    key = "Design.status"

    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_IMPLEMENTED = "implemented"
    STATUS_SUPERSEDED = "superseded"

    CHOICES = [
        (STATUS_DRAFT, "Draft", "cyan"),
        (STATUS_APPROVED, "Approved", "green"),
        (STATUS_REJECTED, "Rejected", "red"),
        (STATUS_IMPLEMENTED, "Implemented", "blue"),
        (STATUS_SUPERSEDED, "Superseded", "gray"),
    ]


class DesignPlacementKindChoices(ChoiceSet):
    KIND_ADD = "add"
    KIND_MOVE = "move"
    KIND_REMOVE = "remove"

    CHOICES = [
        (KIND_ADD, "Add", "green"),
        (KIND_MOVE, "Move", "blue"),
        (KIND_REMOVE, "Remove", "red"),
    ]
