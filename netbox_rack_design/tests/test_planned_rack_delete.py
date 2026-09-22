"""Regression coverage for T1.5b: deleting a ``Design`` that has
``PlannedRack``\\ s attached could raise an uncaught ``IntegrityError`` -- a
raw 500 -- under a narrow but real concurrency race, instead of a friendly
error. See PLAN-templates.md sec 1 / D3-D7, D27, D28.

THE BUG
-------
``Design.planned_racks`` is a plain ``ManyToManyField`` to ``PlannedRack``.
Django's cascade delete for a model instance collects the auto-created M2M
through-table rows it needs to remove UP FRONT (one query), deletes exactly
those rows, and only then deletes the instance itself. If a SECOND,
independently-committing request attaches a NEW planned rack to the SAME
design in the gap between that collection and the row's own DELETE -- e.g.
another user's ``create-planned-rack`` POST landing at just the wrong moment
-- that new join row is never part of what got collected, so it is still
there when Postgres checks referential integrity for the final
``DELETE FROM netbox_rack_design_design``, and the whole request dies with an
uncaught ``django.db.utils.IntegrityError``.

Confirmed unreproducible via any SINGLE-connection sequence (ORM
``design.delete()``, a REST ``DELETE``, even against the exact orphaned
``Design`` row a live e2e run left in this state) -- it needs a genuinely
concurrent SECOND writer, which is exactly what ``DesignDeleteRaceMechanismTests``
below constructs deliberately (a ``pre_delete`` signal that opens its OWN
database connection and commits an attach from "outside" the deleting
transaction).

THE FIX (``netbox_rack_design/api/views.py``)
----------------------------------------------
Both ``DesignViewSet.perform_destroy`` and ``DesignViewSet.create_planned_rack``
now take a ``select_for_update()`` lock on the SAME design row, inside a
``transaction.atomic()`` block, before writing. That serializes the two
requests: whichever gets there first wins outright (the other either sees
the fully-attached planned rack and deletes it normally, or finds the design
already gone and 404s instead of attaching to nothing). ``perform_destroy``
also catches ``IntegrityError`` defensively and turns it into a 409, in case
some future or out-of-band writer ever reaches this window some other way
that the lock does not cover.

``DesignDeleteRaceMechanismTests`` proves the underlying mechanism is real by
reproducing it directly against ``Design.delete()`` -- deliberately
bypassing ``DesignViewSet``'s lock, since the model layer itself was never
patched (the fix lives at the API boundary, the only production writer).
``DesignDeleteConcurrencyTests`` proves the PRODUCT is fixed: real concurrent
threads hammering the actual ``create-planned-rack`` and ``DELETE`` REST
endpoints never surface a 500.
"""
import threading
import uuid

from dcim.models import Location, Site
from django.db import IntegrityError, connections
from django.db.models.signals import pre_delete
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from users.models import Token, User

from ..models import Design, PlannedRack
from .utils import api_token_header, make_design


class DesignDeleteRaceMechanismTests(TransactionTestCase):
    """Reproduces the raw Django/Postgres mechanism directly against
    ``Design.delete()`` -- the brief's "reproduce it in a TEST, not by
    staring at the code". Uses a real, separate database connection from
    inside a ``pre_delete`` signal to simulate a second request's write
    landing in the exact gap Django's cascade-delete leaves open.
    """

    def setUp(self):
        self.site = Site.objects.first() or Site.objects.create(name="Race Site", slug="race-site")
        self.location = Location.objects.create(
            site=self.site, name=f"race-loc-{uuid.uuid4().hex[:8]}",
            slug=f"race-loc-{uuid.uuid4().hex[:8]}",
        )
        self.design = make_design(
            title=f"race-design-{uuid.uuid4().hex[:8]}", site=self.site, status="draft",
        )
        self.pr1 = PlannedRack.objects.create(
            name="race-pr1", u_height=10, location=self.location,
        )
        self.design.planned_racks.add(self.pr1)

    def tearDown(self):
        PlannedRack.objects.filter(location=self.location).delete()
        Design.objects.filter(pk=self.design.pk).delete()
        self.location.delete()

    def test_concurrent_attach_during_delete_raises_integrityerror(self):
        pr2 = PlannedRack.objects.create(
            name="race-pr2", u_height=10, location=self.location,
        )
        design_pk = self.design.pk

        def concurrent_attach_from_another_connection(sender, instance, **kwargs):
            if sender is not Design or instance.pk != design_pk:
                return
            # A genuinely separate connection/transaction, committed
            # independently -- exactly what a second concurrent HTTP request
            # would be. NOT going through create-planned-rack's lock: this
            # test is proving the underlying Django/Postgres mechanism, not
            # exercising the fix (that is what DesignDeleteConcurrencyTests
            # below does).
            conn = connections.create_connection("default")
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO netbox_rack_design_design_planned_racks "
                    "(design_id, plannedrack_id) VALUES (%s, %s)",
                    [instance.pk, pr2.pk],
                )
                conn.commit()
            finally:
                conn.close()

        pre_delete.connect(concurrent_attach_from_another_connection, sender=Design, weak=False)
        try:
            with self.assertRaises(IntegrityError):
                self.design.delete()
        finally:
            pre_delete.disconnect(concurrent_attach_from_another_connection, sender=Design)


class DesignDeleteConcurrencyTests(TransactionTestCase):
    """Hammers the REAL ``create-planned-rack`` and ``DELETE`` REST endpoints
    from real concurrent threads. Before the T1.5b fix this could surface
    the 500 ``DesignDeleteRaceMechanismTests`` reproduces above; after the
    fix the two requests serialize on ``DesignViewSet``'s row lock instead,
    and neither ever returns a 500.
    """

    def setUp(self):
        self.site = Site.objects.first() or Site.objects.create(name="Conc Site", slug="conc-site")
        self.user = User.objects.create_user(username=f"race-user-{uuid.uuid4().hex[:8]}", is_superuser=True)
        self.token = Token.objects.create(user=self.user)
        self.header = api_token_header(self.token)

    def _client(self):
        return APIClient()

    def _race_once(self, design, location):
        """Fire a create-planned-rack POST and a design DELETE from two real
        threads at (as near as a Barrier can force) the same instant, and
        return {"create": status, "delete": status}. Not a closure over any
        loop variable -- everything it needs is a parameter."""
        barrier = threading.Barrier(2)
        results = {}

        def do_create():
            connections.close_all()
            barrier.wait()
            client = self._client()
            r = client.post(
                f"/api/plugins/rack-design/designs/{design.pk}/create-planned-rack/",
                {"name": f"pr2-{uuid.uuid4().hex[:6]}", "u_height": 10,
                 "location_id": location.pk},
                format="json",
                **self.header,
            )
            results["create"] = r.status_code
            connections.close_all()

        def do_delete():
            connections.close_all()
            barrier.wait()
            client = self._client()
            r = client.delete(
                f"/api/plugins/rack-design/designs/{design.pk}/", **self.header,
            )
            results["delete"] = r.status_code
            connections.close_all()

        t_create = threading.Thread(target=do_create)
        t_delete = threading.Thread(target=do_delete)
        t_create.start()
        t_delete.start()
        t_create.join(timeout=15)
        t_delete.join(timeout=15)
        return results

    def test_concurrent_create_and_delete_never_500(self):
        iterations = 8
        for i in range(iterations):
            location = Location.objects.create(
                site=self.site, name=f"cloc-{i}-{uuid.uuid4().hex[:8]}",
                slug=f"cloc-{i}-{uuid.uuid4().hex[:8]}",
            )
            design = make_design(
                title=f"cdesign-{i}-{uuid.uuid4().hex[:8]}", site=self.site, status="draft",
            )
            pr1 = PlannedRack.objects.create(name=f"pr1-{i}", u_height=10, location=location)
            design.planned_racks.add(pr1)

            results = self._race_once(design, location)

            self.assertNotIn(
                500, results.values(),
                f"iteration {i}: a concurrent create-planned-rack + delete "
                f"returned a 500: {results}",
            )
            self.assertEqual(
                len(results), 2,
                f"iteration {i}: one of the two requests did not finish "
                f"in time: {results}",
            )

            # Tidy up whatever this iteration left behind, whichever order
            # the two requests actually resolved in.
            PlannedRack.objects.filter(location=location).delete()
            Design.objects.filter(pk=design.pk).delete()
            location.delete()
