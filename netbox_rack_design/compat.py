"""
Cross-version compatibility shims for the supported NetBox range (4.4 - 4.6).

This is the ONLY module allowed to know which NetBox version it is running on.
Everything else imports the names below, so widening or narrowing the supported
range means editing one file. Each shim uses capability detection rather than a
version comparison, so it keeps working when the next minor moves things again.

Two things changed under us between 4.4 and 4.6:

1. GraphQL filter bases. NetBox 4.5 replaced the mixins in
   ``netbox.graphql.filter_mixins`` with concrete filter classes in a new
   ``netbox.graphql.filters`` module (netbox-community/netbox#20926). The old module
   still exists in 4.5+ but no longer carries those names, and the new module does
   not exist at all in 4.4 -- so neither import alone can serve both.

2. API tokens. From 4.5 a newly created token is a "v2" token whose secret is never
   stored (HMAC + pepper). ``Token.key`` -- the entire secret in 4.4 -- is now only a
   non-sensitive identifier, and the plaintext lives on ``Token.token`` for as long as
   the instance is in memory. Sending the 4.4 header form to 4.5+ fails with
   ``403 Invalid v1 token``.
"""

__all__ = (
    "GraphQLModelFilterBase",
    "GraphQLDescribedModelFilterBase",
    "API_TOKEN_PREFIX",
    "HAS_V2_API_TOKENS",
    "custom_fields_for",
)


try:  # NetBox 4.5+
    from netbox.graphql.filters import NetBoxModelFilter as GraphQLModelFilterBase

    # Adds exactly `description` + `comments` on top of the NetBoxModel filter (and,
    # importantly, NOT an `owner` filter -- verified on both 4.5.10 and 4.6.8), so a
    # NetBoxModel that carries those two fields can use it as its filter base.
    from netbox.graphql.filters import PrimaryModelFilter as GraphQLDescribedModelFilterBase
except ImportError:  # NetBox 4.4
    from netbox.graphql.filter_mixins import (
        NetBoxModelFilterMixin as GraphQLModelFilterBase,
    )
    from netbox.graphql.filter_mixins import (
        PrimaryModelFilterMixin as GraphQLDescribedModelFilterBase,
    )

try:  # NetBox 4.5+ ships the v2 token prefix ('nbt_')
    from users.constants import TOKEN_PREFIX as API_TOKEN_PREFIX

    HAS_V2_API_TOKENS = True
except ImportError:  # NetBox 4.4: Token.key IS the secret
    API_TOKEN_PREFIX = ""
    HAS_V2_API_TOKENS = False


def custom_fields_for(model):
    """The custom fields assigned to ``model``, as a list.

    NetBox 4.4-4.6 return a queryset from ``CustomField.objects.get_for_model``;
    4.7 returns a request-cached list of the ACTIVE fields only (a field whose
    stored data is still being purged is hidden). Either way the caller gets a
    list, and on a queryset the choice set and related object type come in the
    same query.
    """
    from extras.models import CustomField

    fields = CustomField.objects.get_for_model(model)
    if hasattr(fields, "select_related"):
        fields = fields.select_related("choice_set", "related_object_type")
    return list(fields)
