"""
Generates mock packing slip and invoice PDFs for the three seed orders,
and creates the host-side folders if they don't exist.

Run inside the backend container:
    python -m app.db.seed_documents
"""
import asyncio
from app.services.pdf_service import generate_packing_slip_pdf, generate_invoice_pdf


ORDERS = [
    {
        "order_id":       "ORD-1042",
        "delivery":       "DEL-2024-0881",
        "invoice":        "INV-8821",
        "sales_order":    "SO-10042",
        "customer_name":  "Global Trade Co.",
        "customer_email": "sarah.chen@globaltrade.com",
        "ship_date":      "2026-02-25",
        "invoice_date":   "2026-02-25",
        "due_date":       "2026-03-27",
        "lines": [
            {"material": "MAT-001", "description": "Industrial Pump Assembly - 50Hz",
             "lot": "LOT-2024-A1", "qty": 2, "uom": "EA", "unit_price": 4250.00, "total": 8500.00},
            {"material": "MAT-002", "description": "Pressure Valve DN50 PN16",
             "lot": "LOT-2024-A2", "qty": 4, "uom": "EA", "unit_price": 380.00, "total": 1520.00},
        ],
    },
    {
        "order_id":       "ORD-2891",
        "delivery":       "DEL-2024-0992",
        "invoice":        "INV-9014",
        "sales_order":    "SO-10098",
        "customer_name":  "Acme Corporation",
        "customer_email": "m.rodriguez@acmecorp.io",
        "ship_date":      "2026-02-26",
        "invoice_date":   "2026-02-26",
        "due_date":       "2026-03-28",
        "lines": [
            {"material": "MAT-003", "description": "Stainless Steel Pipe 2-inch x 6m",
             "lot": "LOT-2024-B1", "qty": 20, "uom": "M", "unit_price": 145.00, "total": 2900.00},
        ],
    },
    {
        "order_id":       "ORD-9934",
        "delivery":       "DEL-2024-1103",
        "invoice":        "INV-9287",
        "sales_order":    "SO-10201",
        "customer_name":  "FastFreight Net",
        "customer_email": "james.liu@fastfreight.net",
        "ship_date":      "2026-02-27",
        "invoice_date":   "2026-02-27",
        "due_date":       "2026-03-29",
        "lines": [
            {"material": "MAT-004", "description": "Control Panel Unit 480V",
             "lot": "LOT-2024-C1", "qty": 1, "uom": "EA", "unit_price": 12500.00, "total": 12500.00},
            {"material": "MAT-005", "description": "Bearing Kit SKF 6205-2RS",
             "lot": "LOT-2024-C2", "qty": 10, "uom": "KIT", "unit_price": 89.50, "total": 895.00},
        ],
    },
]


async def seed():
    for o in ORDERS:
        ps_path, ps_name = await generate_packing_slip_pdf(
            order_id=o["order_id"],
            delivery_number=o["delivery"],
            customer_name=o["customer_name"],
            customer_po=o["order_id"],
            sales_order=o["sales_order"],
            ship_date=o["ship_date"],
            lines=o["lines"],
        )
        print(f"  Packing slip: {ps_name}")

        inv_path, inv_name = await generate_invoice_pdf(
            invoice_number=o["invoice"],
            order_id=o["order_id"],
            sales_order=o["sales_order"],
            delivery_number=o["delivery"],
            customer_name=o["customer_name"],
            customer_email=o["customer_email"],
            invoice_date=o["invoice_date"],
            due_date=o["due_date"],
            lines=o["lines"],
        )
        print(f"  Invoice:      {inv_name}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(seed())
