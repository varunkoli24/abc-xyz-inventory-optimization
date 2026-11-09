# ABC-XYZ inventory segmentation and safety stock: cleaning -> weekly demand -> ABC-XYZ in SQL -> safety stock
#
# HOW TO RUN, in a notebook (Google Colab or Jupyter) where the ORIGINAL DataCo file is already loaded into a DataFrame called `df`:
#       import pandas as pd
#       df = pd.read_csv("DataCoSupplyChainDataset.csv", encoding="latin-1")
#       %run -i abc_xyz_safety_stock.py    (the "-i" matters: it lets the script see your df)
# or paste each section (they start with "# %%") into its own notebook cell and run them in order.
# Cell 1 does all the cleaning, so `df` is the file as downloaded: no earlier cleaning cells are needed.
#
# Nothing here needs a decision from you. The assumptions are written in capitals at the top of
# the sections where they are used, so you can change them and run again.

if "df" not in globals():
    raise NameError("`df` is missing: first load the original DataCo file into it, "
                    "df = pd.read_csv('DataCoSupplyChainDataset.csv', encoding='latin-1'), then run the script with  %run -i  (with the -i).")

# %% CELL 1: cleaning with fixed rules (no decisions needed from you)
import numpy as np
import pandas as pd

n0 = len(df)

# a) Order Item Id must be unique -> keep the first copy of any id that still repeats
df = df.drop_duplicates(subset="Order Item Id", keep="first")
print("repeated Order Item Ids dropped:", n0 - len(df))

# b) numbers must be usable: junk text -> NaN -> dropped; zero or negative quantity/price is impossible
key_nums = ["Product Card Id", "Order Item Quantity", "Order Item Product Price", "Order Item Discount"]
for c in key_nums:
    df[c] = pd.to_numeric(df[c], errors="coerce")
print("rows with no product name:", int(df["Product Name"].isna().sum()))   # broken rows: they would show up as 'unknown' SKUs
bad = (df[key_nums].isna().any(axis=1) | df["Product Name"].isna() | (df["Order Item Quantity"] <= 0)
       | (df["Order Item Product Price"] <= 0) | (df["Order Item Discount"] < 0))
print("rows with missing or impossible values, or no product name, dropped:", int(bad.sum()))
df = df[~bad].copy()
df["Product Card Id"] = df["Product Card Id"].astype(int)

# c) one name per SKU: the most common spelling wins
names = df.groupby("Product Card Id")["Product Name"].agg(
    lambda s: s.value_counts().idxmax() if s.notna().any() else "unknown")

# d) order date -> real datetime (DataCo writes it as month/day/year hour:minute)
date_col = next(c for c in df.columns if c.lower().startswith("order date"))
od = df[date_col]
if not pd.api.types.is_datetime64_any_dtype(od):
    od = pd.to_datetime(od, format="%m/%d/%Y %H:%M", errors="coerce")
    if od.isna().mean() > 0.01:                      # unexpected format -> let pandas guess instead
        od = pd.to_datetime(df[date_col], errors="coerce")
print(f"order dates: {od.min():%Y-%m-%d} to {od.max():%Y-%m-%d} | unreadable dates: {int(od.isna().sum())}")

# e) which orders count as demand? cancelled and fraud-flagged orders never leave the warehouse -> leave out
status = df["Order Status"].astype(str).str.strip().str.upper()
print(status.value_counts().to_string())
keep = od.notna() & ~status.isin(["CANCELED", "CANCELLED", "SUSPECTED_FRAUD"])

# f) revenue = net revenue of the order line = price x quantity - discount
revenue = df["Order Item Product Price"] * df["Order Item Quantity"] - df["Order Item Discount"]
if "Order Item Total" in df:
    print("net revenue vs 'Order Item Total', largest gap:", round(float((revenue - pd.to_numeric(df["Order Item Total"], errors="coerce")).abs().max()), 4))

lines = pd.DataFrame({
    "sku": df["Product Card Id"],
    "name": df["Product Card Id"].map(names),
    "order_date": od,
    "units": df["Order Item Quantity"],
    "revenue": revenue,
    "ship_days": pd.to_numeric(df["Days for shipping (real)"], errors="coerce"),
    "status": status,
})[keep]
print(f"demand lines kept: {len(lines):,} of {len(df):,} | SKUs: {lines['sku'].nunique()}")
lines.to_pickle("lines_clean.pkl")      # safety copy: after a restart use  lines = pd.read_pickle("lines_clean.pkl")

# %% CELL 2: weekly demand window
import numpy as np
import pandas as pd

W_START = W_END = None      # optional manual override, e.g. W_END = "2017-09-25", if the printout below shows a thin end

lines["week"] = lines["order_date"].dt.to_period("W-SUN").dt.start_time      # Monday that starts each week

wk = lines.groupby("week")["units"].sum()                                    # units sold per week, all SKUs together
wk = wk.reindex(pd.date_range(wk.index.min(), wk.index.max(), freq="W-MON"), fill_value=0)
wk = wk.iloc[1:-1]                                                           # first and last week are usually partial
normal = wk >= 0.5 * wk.median()                                             # weeks with a normal level of activity
w_start, w_end = normal.idxmax(), normal[::-1].idxmax()                      # first and last normal week
if W_START:
    w_start = pd.Timestamp(W_START).to_period("W-SUN").start_time            # snap any date to its Monday
if W_END:
    w_end = pd.Timestamp(W_END).to_period("W-SUN").start_time
n_weeks = (w_end - w_start).days // 7 + 1
print(f"weekly window: {w_start:%Y-%m-%d} to {w_end:%Y-%m-%d} = {n_weeks} weeks "
      f"({len(wk) - n_weeks} edge weeks left out)")

inside = wk.loc[w_start:w_end]
first13, last13 = inside.iloc[:13].median() / inside.median(), inside.iloc[-13:].median() / inside.median()
print(f"typical week = {inside.median():,.0f} units | first 13 weeks run at {first13:.0%}, last 13 weeks at {last13:.0%} of that")
if min(first13, last13) < 0.7:
    print("CHECK: one end of the history runs well below the typical level. If that is the data thinning out rather than "
          "real demand, set W_START / W_END at the top of this cell (any date in the right week) and run it again.")

win = lines[(lines["week"] >= w_start) & (lines["week"] <= w_end)].copy()
weekly = win.groupby(["sku", "week"], as_index=False).agg(units=("units", "sum"), revenue=("revenue", "sum"))
weekly["week"] = weekly["week"].dt.strftime("%Y-%m-%d")                      # plain text dates for SQL
print(f"{weekly['sku'].nunique()} SKUs | {len(weekly):,} SKU-weeks with sales")

# %% CELL 3: ABC-XYZ in SQL, then an independent check in pandas
import math
import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

print("SQLite version:", sqlite3.sqlite_version, "(window functions need 3.25 or newer)")
con = sqlite3.connect(":memory:")
con.create_function("SQRT", 1, lambda x: None if x is None else math.sqrt(max(x, 0.0)))   # some SQLite builds lack SQRT
weekly.to_sql("weekly", con, index=False)          # one row per SKU and week WITH sales: sku, week, units, revenue

X_MAX, Y_MAX = 0.5, 1.0            # CV limits: X = steady, Y = moderate, Z = erratic (the usual textbook cut-offs)

# The query holds its own parameters (the params CTE), so the saved abc_xyz.sql runs as it is on weekly_sales.csv
SQL = f"""-- ABC-XYZ classification of SKUs: ABC by share of revenue, XYZ by how erratic the weekly demand is (CV).
-- Input: table weekly(sku, week, units, revenue), one row per SKU and Monday week WITH sales.
--        weekly_sales.csv is that table; the Python script writes it next to this file.
-- Parameters: the params CTE below holds the first and last Monday of the window and the CV limits x_max and y_max.
-- Dialect: SQLite. Window functions need 3.25+; SQRT needs 3.35+ (the Python script registers its own SQRT for older builds).
-- To run it without Python, in the folder that holds weekly_sales.csv (sqlite3 command-line tool, 3.35 or newer):
--   sqlite3 -csv -header :memory: "CREATE TABLE weekly (sku INTEGER, week TEXT, units INTEGER, revenue REAL);" ".import --skip 1 weekly_sales.csv weekly" ".read abc_xyz.sql"
WITH RECURSIVE
params(w_start, w_end, x_max, y_max) AS (          -- the only fixed values of the query
    SELECT '{w_start:%Y-%m-%d}', '{w_end:%Y-%m-%d}', {X_MAX}, {Y_MAX}
),
cal(week) AS (                                     -- calendar spine: every Monday in the window
    SELECT w_start FROM params
    UNION ALL
    SELECT date(week, '+7 day') FROM cal WHERE week < (SELECT w_end FROM params)
),
launch AS (                                        -- the history of a SKU starts at its first sale
    SELECT sku, MIN(week) AS first_week FROM weekly GROUP BY sku
),
grid AS (                                          -- every SKU x every week since its launch
    SELECT l.sku, c.week FROM launch l JOIN cal c ON c.week >= l.first_week
),
filled AS (                                        -- weeks without sales count as 0, not as missing
    SELECT g.sku, g.week, COALESCE(w.units, 0) AS units
    FROM grid g LEFT JOIN weekly w ON w.sku = g.sku AND w.week = g.week
),
vol AS (                                           -- mean and sample standard deviation of weekly units
    SELECT sku, COUNT(*) AS n_weeks, AVG(units) AS mean_wk,
           SQRT((AVG(units * units) - AVG(units) * AVG(units)) * COUNT(*) / (COUNT(*) - 1)) AS sd_wk
    FROM filled GROUP BY sku
),
rev AS (
    SELECT sku, SUM(revenue) AS revenue FROM weekly GROUP BY sku
),
ranked AS (                                        -- running revenue total, biggest SKU first
    SELECT sku, revenue,
           SUM(revenue) OVER (ORDER BY revenue DESC, sku
                              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_rev,
           SUM(revenue) OVER () AS total_rev
    FROM rev
)
SELECT r.sku, r.revenue,
       r.cum_rev / r.total_rev AS cum_share,
       CASE WHEN (r.cum_rev - r.revenue) / r.total_rev < 0.80 THEN 'A'      -- share of revenue BEFORE this SKU
            WHEN (r.cum_rev - r.revenue) / r.total_rev < 0.95 THEN 'B'
            ELSE 'C' END AS abc,
       v.n_weeks, v.mean_wk, v.sd_wk, v.sd_wk / v.mean_wk AS cv,
       CASE WHEN v.sd_wk / v.mean_wk <= p.x_max THEN 'X'
            WHEN v.sd_wk / v.mean_wk <= p.y_max THEN 'Y'
            ELSE 'Z' END AS xyz
FROM ranked r JOIN vol v USING (sku) CROSS JOIN params p
ORDER BY r.revenue DESC
"""
sku = pd.read_sql_query(SQL, con)
con.close()
sku["segment"] = sku["abc"] + sku["xyz"]
Path("abc_xyz.sql").write_text(SQL)                 # save the query as its own file: it runs as it is (see its header)
weekly.to_csv("weekly_sales.csv", index=False)      # the table the query reads, so the SQL can be run without this script

# ---- independent check: same classes computed in pandas from a zero-filled weekly grid ----
all_weeks = pd.date_range(w_start, w_end, freq="W-MON")
grid = (win.groupby(["sku", "week"])["units"].sum().unstack(fill_value=0)
           .reindex(columns=all_weeks, fill_value=0))
first = (grid.to_numpy() > 0).argmax(axis=1)                          # column of each SKU first sale
after_launch = np.arange(grid.shape[1])[None, :] >= first[:, None]
vals = grid.where(after_launch)                                       # weeks before launch -> NaN, ignored
chk = pd.DataFrame({"mean_pd": vals.mean(axis=1), "sd_pd": vals.std(axis=1, ddof=1)})
chk["cv_pd"] = chk["sd_pd"] / chk["mean_pd"]
chk["xyz_pd"] = np.where(chk["cv_pd"] <= X_MAX, "X", np.where(chk["cv_pd"] <= Y_MAX, "Y", "Z"))
rev_pd = win.groupby("sku")["revenue"].sum().sort_values(ascending=False)
before = (rev_pd.cumsum() - rev_pd) / rev_pd.sum()
chk["abc_pd"] = pd.Series(np.where(before < 0.80, "A", np.where(before < 0.95, "B", "C")), index=rev_pd.index)

m = sku.set_index("sku").join(chk)
same = ((m["abc"] == m["abc_pd"]) & (m["xyz"] == m["xyz_pd"])
        & np.isclose(m["mean_wk"], m["mean_pd"]) & np.isclose(m["cv"], m["cv_pd"], equal_nan=True))
print(f"\nSQL and pandas agree on {int(same.sum())} of {len(m)} SKUs")
if not same.all():
    print(m.loc[~same, ["abc", "abc_pd", "xyz", "xyz_pd", "cv", "cv_pd"]])

# ---- the 9-box ----
box = pd.crosstab(sku["abc"], sku["xyz"]).reindex(index=list("ABC"), columns=list("XYZ"), fill_value=0)
share = (pd.crosstab(sku["abc"], sku["xyz"], values=sku["revenue"], aggfunc="sum")
           .reindex(index=list("ABC"), columns=list("XYZ")).fillna(0) / sku["revenue"].sum() * 100).round(1)
print("\nNumber of SKUs per box (rows = ABC, columns = XYZ):\n" + box.to_string())
print("\nShare of revenue per box, %:\n" + share.to_string())

# zero-filled weekly demand, one row per SKU and week (for Power BI)
long = pd.DataFrame({"sku": np.repeat(vals.index.to_numpy(), vals.shape[1]),
                     "week": np.tile(vals.columns.to_numpy(), vals.shape[0]),
                     "units": vals.to_numpy().ravel()}).dropna()
long["units"] = long["units"].astype(int)
long.to_csv("weekly_demand_long.csv", index=False)

# %% CELL 4: safety stock, reorder point, EOQ and the comparison with the flat 15-day rule
from statistics import NormalDist
import numpy as np
import pandas as pd

# ---- assumptions: change them here and run again (they are listed in the README) ----
ORDER_COST = 50      # cost of placing one replenishment order
HOLD_RATE = 0.22     # yearly holding cost as a share of inventory value (18-25% is typical)
COST_RATIO = 0.70    # unit cost as a share of net selling price (the data has no cost column)
FLAT_DAYS = 15       # baseline rule being beaten: safety stock = 15 days of average demand
SERVICE = {"AX": .99, "AY": .98, "AZ": .97,          # target cycle service level for each box:
           "BX": .97, "BY": .95, "BZ": .93,          # higher for valuable (A) and predictable (X) items,
           "CX": .95, "CY": .92, "CZ": .90}          # because protecting them is worth more or costs less

# lead time proxy = actual shipping days of each SKU's orders (the data has no supplier lead time)
lt = win.groupby("sku")["ship_days"].agg(lt_mean="mean", lt_sd="std")
lt["lt_mean"] = lt["lt_mean"].fillna(win["ship_days"].mean())
lt["lt_sd"] = lt["lt_sd"].fillna(win["ship_days"].std())
sk = sku.set_index("sku").join(lt)
sk = sk[sk["n_weeks"] >= 2].copy()      # one week of history has no standard deviation -> no policy can be computed
sk["unit_cost"] = COST_RATIO * sk["revenue"] / win.groupby("sku")["units"].sum()
sk["name"] = names.reindex(sk.index)


def policy(extra_days=0):
    t = sk.copy()
    t["L_wk"] = (t["lt_mean"] + extra_days) / 7                    # lead time in WEEKS, because demand is weekly
    t["L_sd_wk"] = t["lt_sd"] / 7
    # sd of demand over the lead time = sqrt( L * var(demand) + mean(demand)^2 * var(L) )
    t["sd_ltd"] = np.sqrt(t["L_wk"] * t["sd_wk"] ** 2 + (t["mean_wk"] * t["L_sd_wk"]) ** 2)
    t["target_sl"] = t["segment"].map(SERVICE)
    t["z"] = t["target_sl"].map(NormalDist().inv_cdf)
    t["safety_stock"] = t["z"] * t["sd_ltd"]
    t["reorder_point"] = t["mean_wk"] * t["L_wk"] + t["safety_stock"]
    t["eoq"] = np.sqrt(2 * t["mean_wk"] * 52 * ORDER_COST / (HOLD_RATE * t["unit_cost"]))
    t["ss_flat"] = t["mean_wk"] * FLAT_DAYS / 7
    t["flat_sl"] = (t["ss_flat"] / t["sd_ltd"]).map(NormalDist().cdf)   # service level the flat rule really delivers
    t["ss_value"] = t["safety_stock"] * t["unit_cost"]
    t["ss_flat_value"] = t["ss_flat"] * t["unit_cost"]
    return t


pol = policy(0)
seg = pol.groupby("segment").agg(
    skus=("revenue", "size"), revenue_share=("revenue", "sum"), target_sl=("target_sl", "first"),
    flat_rule_sl=("flat_sl", "median"), flat_rule_value=("ss_flat_value", "sum"), new_policy_value=("ss_value", "sum"))
seg["revenue_share"] = (seg["revenue_share"] / pol["revenue"].sum() * 100).round(1)
seg["target_sl"] = (seg["target_sl"] * 100).round(1)
seg["flat_rule_sl"] = (seg["flat_rule_sl"] * 100).round(1)
seg["change_%"] = ((seg["new_policy_value"] / seg["flat_rule_value"] - 1) * 100).round(0)
print("Safety-stock value by box (service levels in %):")
print(seg.round({"flat_rule_value": 0, "new_policy_value": 0}).to_string())

flat, new = pol["ss_flat_value"].sum(), pol["ss_value"].sum()
cycle = (pol["eoq"] / 2 * pol["unit_cost"]).sum()                # average cycle stock is half an order
print(f"\nSafety-stock value: flat rule {flat:,.0f} vs new policy {new:,.0f} ({(new / flat - 1) * 100:+.0f}%)")
print(f"Total average inventory incl. cycle stock: {flat + cycle:,.0f} vs {new + cycle:,.0f} "
      f"({((new + cycle) / (flat + cycle) - 1) * 100:+.0f}%)")

# the shipping-days proxy is short, so test what happens if real replenishment takes longer.
# The saving is only a like-for-like comparison while the flat rule still reaches each box's target service level,
# so also count the SKUs where it does not (its estimated service level is below the target).
rows = []
for extra in [0, 7, 14, 28]:
    p = policy(extra)
    miss = p["flat_sl"] < p["target_sl"]
    rows.append({"extra_lead_days": extra, "flat_rule_value": p["ss_flat_value"].sum(),
                 "new_policy_value": p["ss_value"].sum(),
                 "flat_misses_target_skus": int(miss.sum()),
                 "flat_misses_target_revenue_%": round(p.loc[miss, "revenue"].sum() / p["revenue"].sum() * 100, 1)})
sens = pd.DataFrame(rows)
sens.insert(3, "change_%", ((sens["new_policy_value"] / sens["flat_rule_value"] - 1) * 100).round(0))
print("\nSame comparison if lead time is longer than the shipping-days proxy:")
print(sens.round({"flat_rule_value": 0, "new_policy_value": 0}).to_string(index=False))

# which boxes drive the result at the longest lead time tested
longest = int(sens["extra_lead_days"].max())
p = policy(longest)
by_box = (p.assign(flat_misses_target=p["flat_sl"] < p["target_sl"]).groupby("segment")
           .agg(skus=("revenue", "size"), flat_rule_value=("ss_flat_value", "sum"),
                new_policy_value=("ss_value", "sum"), flat_misses_target=("flat_misses_target", "sum")))
print(f"\nBy box with {longest} extra lead days:")
print(by_box.round({"flat_rule_value": 0, "new_policy_value": 0}).to_string())

cols = ["name", "segment", "revenue", "mean_wk", "sd_wk", "cv", "lt_mean", "lt_sd", "target_sl", "safety_stock",
        "reorder_point", "eoq", "ss_flat", "flat_sl", "unit_cost", "ss_value", "ss_flat_value"]
pol[cols].round(3).reset_index().to_csv("policy_by_sku.csv", index=False)
seg.to_csv("segment_summary.csv")
sens.to_csv("lead_time_sensitivity.csv", index=False)
# data-quality flags, counted on the SKUs that have a policy
STALE_WEEKS = 13      # stale = no sale in the last 13 weeks of the window = the 13 Mondays from (w_end - 12 weeks) to w_end
last_sale = weekly.groupby("sku")["week"].max().reindex(sk.index)
stale = last_sale < f"{w_end - pd.Timedelta(weeks=STALE_WEEKS - 1):%Y-%m-%d}"    # last sale before the first of those Mondays
short = sk["n_weeks"] < 26
print(f"\nFlags on the {len(sk)} SKUs with a policy: {int(short.sum())} with under 26 weeks of history | "
      f"{int(stale.sum())} with no sale in the last {STALE_WEEKS} weeks | {int((~short & ~stale).sum())} with neither problem "
      f"{sk.loc[~short & ~stale, 'segment'].value_counts().to_dict()}")

print("\nSaved: policy_by_sku.csv, segment_summary.csv, lead_time_sensitivity.csv, weekly_demand_long.csv, weekly_sales.csv, abc_xyz.sql")
