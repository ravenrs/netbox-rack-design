"""
GraphQL schema for NetBox Rack Design.

For more information on NetBox GraphQL, see:
https://docs.netbox.dev/en/stable/plugins/development/graphql/

For Strawberry GraphQL documentation, see:
https://strawberry.rocks/
"""

from typing import List

import strawberry
import strawberry_django

from .models import Rackdesign


@strawberry_django.type(
    Rackdesign,
    fields='__all__',
)
class RackdesignType:
    """GraphQL type for Rackdesign model."""
    pass


@strawberry.type(name="Query")
class RackdesignQuery:
    """GraphQL queries for NetBox Rack Design."""

    rackdesign: RackdesignType = strawberry_django.field()
    rackdesign_list: List[RackdesignType] = strawberry_django.field()


schema = [
    RackdesignQuery,
]

