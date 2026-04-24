"""
Tests for POST /orders/import — v3.6.10 rewritten import logic.

Covers:
  1. New delivery → Order + line created
  2. Same row re-imported → skipped (unchanged)
  3. Same (delivery, NDC, lot) but new qty → updated (latest-wins)
  4. Same delivery, new NDC/lot combination → line created, attached to existing Order
  5. Missing my_delivery_number → error (not a crash)
  6. Multiple rows under same delivery in one import → one Order, many lines
  7. Existing duplicate Orders for same delivery_number do NOT raise
     (regression for the "Multiple rows were found" bug)

Run from backend/:
    python -m pytest tests/test_orders_import.py -v
  or:
    python tests/test_orders_import.py
"""

import asyncio
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub app.core.tasks before orders.py imports it lazily (celery isn't installed in test env).
import types
from unittest.mock import MagicMock
_tasks_stub = types.ModuleType("app.core.tasks")
_tasks_stub.trigger_power_automate_task = MagicMock(delay=MagicMock())
sys.modules["app.core.tasks"] = _tasks_stub

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.models import Base, Order, OrderLine, PodRegistry
from app.api.orders import import_orders


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_xlsx(rows):
    """Build an .xlsx file in memory from a list of dict rows."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        'customer_order_number', 'my_delivery_number', 'warehouse_delivery_number',
        'sales_order_number', 'invoice_number', 'customer_name', 'customer_email',
        'line_number', 'material_number', 'material_description', 'lot_number',
        'quantity', 'unit_of_measure', 'tracking_number', 'carrier',
    ]
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


class _FakeUploadFile:
    """Mimic FastAPI's UploadFile just enough for import_orders."""
    def __init__(self, content: bytes, filename: str = "import.xlsx"):
        self._content = content
        self.filename = filename
    async def read(self):
        return self._content


async def _run_import(session, rows, filename="import.xlsx"):
    upload = _FakeUploadFile(_make_xlsx(rows), filename)
    # current_user/_ are injected by Depends in FastAPI; call directly here
    return await import_orders(file=upload, db=session, current_user=None)


async def _count(session, model):
    from sqlalchemy import select, func
    r = await session.execute(select(func.count()).select_from(model))
    return r.scalar()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
async def _fresh_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class ImportOrdersTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_1_new_delivery_creates_order_and_line(self):
        async def _t():
            _, S = await _fresh_session()
            async with S() as s:
                result = await _run_import(s, [{
                    'customer_order_number': 'PO-1',
                    'my_delivery_number':    'DEL-1',
                    'line_number':            1,
                    'material_number':       'NDC-A',
                    'lot_number':            'LOT-1',
                    'quantity':              100,
                }])
                self.assertEqual(result['created'], 1)
                self.assertEqual(result['updated'], 0)
                self.assertEqual(result['skipped'], 0)
                self.assertEqual(result['errors'], [])
                self.assertEqual(await _count(s, Order), 1)
                self.assertEqual(await _count(s, OrderLine), 1)
                # pod_registry should also be created
                self.assertEqual(await _count(s, PodRegistry), 1)
        self._run(_t())

    def test_2_reimport_same_row_is_skipped(self):
        async def _t():
            _, S = await _fresh_session()
            row = {
                'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                'line_number': 1, 'material_number': 'NDC-A', 'lot_number': 'LOT-1',
                'quantity': 100,
            }
            async with S() as s:
                await _run_import(s, [row])
                result = await _run_import(s, [row])
                self.assertEqual(result['created'], 0)
                self.assertEqual(result['updated'], 0)
                self.assertEqual(result['skipped'], 1)
                self.assertEqual(await _count(s, OrderLine), 1)  # no duplicate
        self._run(_t())

    def test_3_reimport_with_new_quantity_updates(self):
        async def _t():
            _, S = await _fresh_session()
            base = {
                'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                'line_number': 1, 'material_number': 'NDC-A', 'lot_number': 'LOT-1',
            }
            async with S() as s:
                await _run_import(s, [dict(base, quantity=100)])
                result = await _run_import(s, [dict(base, quantity=250)])
                self.assertEqual(result['created'], 0)
                self.assertEqual(result['updated'], 1)
                self.assertEqual(result['skipped'], 0)
                # Confirm qty in DB
                from sqlalchemy import select
                r = await s.execute(select(OrderLine))
                lines = r.scalars().all()
                self.assertEqual(len(lines), 1)
                self.assertEqual(float(lines[0].quantity), 250.0)
        self._run(_t())

    def test_4_new_line_under_existing_delivery(self):
        async def _t():
            _, S = await _fresh_session()
            async with S() as s:
                await _run_import(s, [{
                    'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                    'line_number': 1, 'material_number': 'NDC-A', 'lot_number': 'LOT-1',
                    'quantity': 100,
                }])
                result = await _run_import(s, [{
                    'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                    'line_number': 2, 'material_number': 'NDC-B', 'lot_number': 'LOT-2',
                    'quantity': 50,
                }])
                self.assertEqual(result['created'], 1)
                self.assertEqual(result['updated'], 0)
                self.assertEqual(result['skipped'], 0)
                self.assertEqual(await _count(s, Order), 1)     # still one order
                self.assertEqual(await _count(s, OrderLine), 2) # two lines now
        self._run(_t())

    def test_5_missing_delivery_number_is_error(self):
        async def _t():
            _, S = await _fresh_session()
            async with S() as s:
                result = await _run_import(s, [{
                    'customer_order_number': 'PO-1',
                    'my_delivery_number':    '',  # missing
                    'line_number': 1, 'material_number': 'NDC-A', 'lot_number': 'LOT-1',
                    'quantity': 100,
                }])
                self.assertEqual(result['created'], 0)
                self.assertEqual(len(result['errors']), 1)
                self.assertIn('missing my_delivery_number', result['errors'][0])
                self.assertEqual(await _count(s, Order), 0)
        self._run(_t())

    def test_6_multiple_rows_same_delivery_one_order(self):
        async def _t():
            _, S = await _fresh_session()
            rows = [
                {'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                 'line_number': 1, 'material_number': 'NDC-A', 'lot_number': 'LOT-1', 'quantity': 10},
                {'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                 'line_number': 2, 'material_number': 'NDC-B', 'lot_number': 'LOT-2', 'quantity': 20},
                {'customer_order_number': 'PO-1', 'my_delivery_number': 'DEL-1',
                 'line_number': 3, 'material_number': 'NDC-C', 'lot_number': 'LOT-3', 'quantity': 30},
            ]
            async with S() as s:
                result = await _run_import(s, rows)
                self.assertEqual(result['created'], 3)
                self.assertEqual(await _count(s, Order), 1)
                self.assertEqual(await _count(s, OrderLine), 3)
        self._run(_t())

    def test_7_preexisting_duplicate_orders_do_not_crash(self):
        """Regression: old data has duplicate Orders per delivery. .scalars().first() must tolerate it."""
        async def _t():
            _, S = await _fresh_session()
            async with S() as s:
                # Seed two duplicate Orders by hand (simulates pre-rewrite bad data)
                s.add(Order(customer_order_number='PO-OLD-A', my_delivery_number='DEL-DUP'))
                s.add(Order(customer_order_number='PO-OLD-B', my_delivery_number='DEL-DUP'))
                await s.commit()
                result = await _run_import(s, [{
                    'customer_order_number': 'PO-NEW', 'my_delivery_number': 'DEL-DUP',
                    'line_number': 1, 'material_number': 'NDC-X', 'lot_number': 'LOT-X',
                    'quantity': 5,
                }])
                # Must not crash; one line is added to whichever existing Order .first() picks
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['created'], 1)
                self.assertEqual(await _count(s, Order), 2)      # no new order
                self.assertEqual(await _count(s, OrderLine), 1)
        self._run(_t())


if __name__ == '__main__':
    unittest.main(verbosity=2)
