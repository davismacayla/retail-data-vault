"""
Retail Operations Data Vault - simulated source system generator.

Produces weekly extract files from 5 simulated source systems:
  crm  -> customers
  pim  -> products
  srm  -> suppliers, purchase_orders
  oms  -> orders, order_lines
  wms  -> inventory_snapshots, shipments

Files land in an S3-style layout:
  <out>/<system>/<entity>/load_date=YYYY-MM-DD/<entity>.csv

Each batch after the first contains realistic changes (customers moving or
changing loyalty tier, price changes, order status updates, PO receipts) so the
Data Vault satellites have real history to capture.

Usage:
  pip install pandas faker
  python generate_sources.py --out data/raw --batches 4
"""
import argparse
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

SEED = 42
fake = Faker("en_US")
Faker.seed(SEED)
random.seed(SEED)

FIRST_BATCH = date(2026, 9, 1)
N_CUSTOMERS_START = 500
NEW_CUSTOMERS_PER_BATCH = 60
N_PRODUCTS_START = 120
NEW_PRODUCTS_PER_BATCH = 5
N_SUPPLIERS = 15
ORDERS_PER_BATCH = 700

WAREHOUSES = ["WH-IND", "WH-CHI", "WH-DAL"]
CATEGORIES = {
    "Electronics": (40, 600),
    "Home": (10, 250),
    "Apparel": (8, 120),
    "Grocery": (2, 30),
    "Toys": (5, 90),
    "Sports": (10, 300),
}
CHANNELS = ["web", "mobile_app", "store"]
CARRIERS = ["UPS", "FedEx", "USPS"]
TIERS = ["Bronze", "Silver", "Gold", "Platinum"]


def ts(d: date) -> datetime:
    """Random business-hours timestamp on a given date."""
    return datetime.combine(
        d, time(random.randint(8, 21), random.randint(0, 59), random.randint(0, 59))
    )


def rand_day(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, (end - start).days))


class RetailSim:
    def __init__(self):
        self.suppliers, self.products, self.customers = {}, {}, {}
        self.orders, self.lines_this_batch = {}, []
        self.inventory, self.reorder_points = {}, {}
        self.purchase_orders, self.open_po = {}, {}
        self.next_customer = self.next_product = 1
        self.next_order, self.next_shipment, self.next_po = 100000, 500000, 900000

    # ---------- master data ----------
    def add_supplier(self, i, d):
        sid = f"SUP-{i:03d}"
        self.suppliers[sid] = {
            "supplier_id": sid,
            "supplier_name": fake.company(),
            "country": random.choice(["US", "US", "US", "MX", "CA", "CN", "VN"]),
            "lead_time_days": random.randint(3, 21),
            "reliability_rating": round(random.uniform(2.5, 5.0), 1),
            "updated_at": ts(d),
        }

    def add_product(self, d):
        sku = f"SKU-{self.next_product:05d}"
        self.next_product += 1
        cat = random.choice(list(CATEGORIES))
        lo, hi = CATEGORIES[cat]
        price = round(random.uniform(lo, hi), 2)
        self.products[sku] = {
            "product_sku": sku,
            "product_name": f"{fake.word().title()} {fake.word().title()} {cat}",
            "category": cat,
            "unit_price": price,
            "unit_cost": round(price * random.uniform(0.45, 0.75), 2),
            "supplier_id": random.choice(list(self.suppliers)),
            "is_active": True,
            "updated_at": ts(d),
        }

    def add_customer(self, d):
        cid = f"C{self.next_customer:06d}"
        self.next_customer += 1
        first, last = fake.first_name(), fake.last_name()
        self.customers[cid] = {
            "customer_id": cid,
            "first_name": first,
            "last_name": last,
            "email": f"{first}.{last}{random.randint(1, 999)}@example.com".lower(),
            "city": fake.city(),
            "state": fake.state_abbr(include_territories=False),
            "loyalty_tier": random.choices(TIERS, weights=[50, 30, 15, 5])[0],
            "signup_date": d,
            "updated_at": ts(d),
        }

    def init_master(self, d):
        for i in range(1, N_SUPPLIERS + 1):
            self.add_supplier(i, d)
        for _ in range(N_PRODUCTS_START):
            self.add_product(d)
        for _ in range(N_CUSTOMERS_START):
            self.add_customer(d)

    def apply_changes(self, start, end):
        """Simulate attribute changes so satellites capture history."""
        for c in random.sample(list(self.customers.values()), int(len(self.customers) * 0.08)):
            if random.random() < 0.5:
                c["loyalty_tier"] = TIERS[min(TIERS.index(c["loyalty_tier"]) + 1, 3)]
            else:
                c["city"], c["state"] = fake.city(), fake.state_abbr(include_territories=False)
            c["updated_at"] = ts(rand_day(start, end))
        for p in random.sample(list(self.products.values()), int(len(self.products) * 0.05)):
            p["unit_price"] = round(p["unit_price"] * random.uniform(0.9, 1.1), 2)
            p["updated_at"] = ts(rand_day(start, end))
        for s in random.sample(list(self.suppliers.values()), 2):
            s["lead_time_days"] = max(2, s["lead_time_days"] + random.randint(-3, 5))
            s["reliability_rating"] = round(min(5.0, max(1.0, s["reliability_rating"] + random.uniform(-0.5, 0.3))), 1)
            s["updated_at"] = ts(rand_day(start, end))
        for _ in range(NEW_CUSTOMERS_PER_BATCH):
            self.add_customer(rand_day(start, end))
        for _ in range(NEW_PRODUCTS_PER_BATCH):
            self.add_product(rand_day(start, end))

    # ---------- orders & fulfillment ----------
    def place_orders(self, start):
        self.lines_this_batch = []
        cust_ids, skus = list(self.customers), list(self.products)
        for _ in range(ORDERS_PER_BATCH):
            oid = f"ORD-{self.next_order}"
            self.next_order += 1
            order_ts = ts(start + timedelta(days=random.randint(0, 6)))
            ship_ts = order_ts + timedelta(days=random.randint(0, 3), hours=random.randint(1, 10))
            deliver_ts = ship_ts + timedelta(days=random.randint(1, 6), hours=random.randint(0, 8))
            cancelled = random.random() < 0.03
            self.orders[oid] = {
                "order_id": oid,
                "customer_id": random.choice(cust_ids),
                "order_ts": order_ts,
                "channel": random.choice(CHANNELS),
                "warehouse_id": random.choice(WAREHOUSES),
                "cancelled": cancelled,
                "cancel_ts": order_ts + timedelta(hours=random.randint(1, 30)),
                "ship_ts": ship_ts,
                "deliver_ts": deliver_ts,
                "carrier": random.choice(CARRIERS),
                "shipment_id": None,
                "emitted_status": None,
            }
            for ln, sku in enumerate(random.sample(skus, random.randint(1, 4)), start=1):
                self.lines_this_batch.append({
                    "order_id": oid,
                    "line_number": ln,
                    "product_sku": sku,
                    "quantity": random.randint(1, 5),
                    "unit_price": self.products[sku]["unit_price"],
                    "discount_pct": random.choice([0, 0, 0, 5, 10, 15]),
                })

    @staticmethod
    def status_as_of(o, cutoff):
        if o["cancelled"] and o["cancel_ts"] <= cutoff:
            return "cancelled", o["cancel_ts"]
        if not o["cancelled"] and o["deliver_ts"] <= cutoff:
            return "delivered", o["deliver_ts"]
        if not o["cancelled"] and o["ship_ts"] <= cutoff:
            return "shipped", o["ship_ts"]
        return "placed", o["order_ts"]

    def advance_fulfillment(self, end):
        cutoff = datetime.combine(end, time(23, 59, 59))
        order_rows, shipment_rows = [], []
        for o in self.orders.values():
            status, updated = self.status_as_of(o, cutoff)
            if status == o["emitted_status"]:
                continue
            o["emitted_status"] = status
            order_rows.append({
                "order_id": o["order_id"],
                "customer_id": o["customer_id"],
                "order_ts": o["order_ts"],
                "channel": o["channel"],
                "order_status": status,
                "updated_at": updated,
            })
            if status in ("shipped", "delivered"):
                if o["shipment_id"] is None:
                    o["shipment_id"] = f"SHP-{self.next_shipment}"
                    self.next_shipment += 1
                shipment_rows.append({
                    "shipment_id": o["shipment_id"],
                    "order_id": o["order_id"],
                    "warehouse_id": o["warehouse_id"],
                    "carrier": o["carrier"],
                    "shipped_ts": o["ship_ts"],
                    "delivered_ts": o["deliver_ts"] if status == "delivered" else None,
                    "updated_at": updated,
                })
        return pd.DataFrame(order_rows), pd.DataFrame(shipment_rows)

    # ---------- inventory & purchasing ----------
    def simulate_inventory(self, start):
        snapshots, po_changed = [], set()
        for day_offset in range(7):
            day = start + timedelta(days=day_offset)
            for wh in WAREHOUSES:
                for sku, prod in self.products.items():
                    key = (wh, sku)
                    if key not in self.inventory:
                        self.inventory[key] = random.randint(40, 200)
                        self.reorder_points[key] = random.randint(15, 40)
                    received = 0
                    po_id = self.open_po.get(key)
                    if po_id and self.purchase_orders[po_id]["received_date"] == day:
                        received = self.purchase_orders[po_id]["quantity"]
                        self.purchase_orders[po_id]["po_status"] = "received"
                        self.purchase_orders[po_id]["updated_at"] = ts(day)
                        po_changed.add(po_id)
                        del self.open_po[key]
                    sold = random.randint(0, 9)
                    self.inventory[key] = max(0, self.inventory[key] + received - sold)
                    if self.inventory[key] <= self.reorder_points[key] and key not in self.open_po:
                        sup = self.suppliers[prod["supplier_id"]]
                        lead = sup["lead_time_days"]
                        slip = max(-2, round(random.gauss(0, (5.5 - sup["reliability_rating"]) * 1.5)))
                        po_id = f"PO-{self.next_po}"
                        self.next_po += 1
                        self.purchase_orders[po_id] = {
                            "po_id": po_id,
                            "supplier_id": sup["supplier_id"],
                            "product_sku": sku,
                            "warehouse_id": wh,
                            "quantity": random.randint(80, 200),
                            "unit_cost": prod["unit_cost"],
                            "order_date": day,
                            "expected_date": day + timedelta(days=lead),
                            "received_date": day + timedelta(days=max(1, lead + slip)),
                            "po_status": "open",
                            "updated_at": ts(day),
                        }
                        self.open_po[key] = po_id
                        po_changed.add(po_id)
                    snapshots.append({
                        "snapshot_date": day,
                        "warehouse_id": wh,
                        "product_sku": sku,
                        "on_hand_qty": self.inventory[key],
                        "reorder_point": self.reorder_points[key],
                        "units_received": received,
                    })
        po_rows = []
        for pid in sorted(po_changed):
            row = dict(self.purchase_orders[pid])
            if row["po_status"] == "open":
                row["received_date"] = None  # not known to the source system yet
            po_rows.append(row)
        return pd.DataFrame(snapshots), pd.DataFrame(po_rows)


def write(df, out: Path, system, entity, load_date):
    folder = out / system / entity / f"load_date={load_date}"
    folder.mkdir(parents=True, exist_ok=True)
    df.to_csv(folder / f"{entity}.csv", index=False)
    return len(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--batches", type=int, default=4)
    args = ap.parse_args()
    out = Path(args.out)

    sim = RetailSim()
    for b in range(args.batches):
        start = FIRST_BATCH + timedelta(weeks=b)
        end = start + timedelta(days=6)
        if b == 0:
            sim.init_master(start)
        else:
            sim.apply_changes(start, end)

        sim.place_orders(start)
        inv_df, po_df = sim.simulate_inventory(start)
        orders_df, ship_df = sim.advance_fulfillment(end)
        ld = start.isoformat()

        # CRM / PIM / SRM send full snapshots; OMS / WMS send changes only.
        counts = {
            "crm/customers": write(pd.DataFrame(sim.customers.values()), out, "crm", "customers", ld),
            "pim/products": write(pd.DataFrame(sim.products.values()), out, "pim", "products", ld),
            "srm/suppliers": write(pd.DataFrame(sim.suppliers.values()), out, "srm", "suppliers", ld),
            "srm/purchase_orders": write(po_df, out, "srm", "purchase_orders", ld),
            "oms/orders": write(orders_df, out, "oms", "orders", ld),
            "oms/order_lines": write(pd.DataFrame(sim.lines_this_batch), out, "oms", "order_lines", ld),
            "wms/inventory_snapshots": write(inv_df, out, "wms", "inventory_snapshots", ld),
            "wms/shipments": write(ship_df, out, "wms", "shipments", ld),
        }
        print(f"Batch {ld}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
