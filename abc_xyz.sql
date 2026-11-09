-- ABC-XYZ classification of SKUs: ABC by share of revenue, XYZ by how erratic the weekly demand is (CV).
-- Input: table weekly(sku, week, units, revenue), one row per SKU and Monday week WITH sales.
--        weekly_sales.csv is that table; the Python script writes it next to this file.
-- Parameters: the params CTE below holds the first and last Monday of the window and the CV limits x_max and y_max.
-- Dialect: SQLite. Window functions need 3.25+; SQRT needs 3.35+ (the Python script registers its own SQRT for older builds).
-- To run it without Python, in the folder that holds weekly_sales.csv (sqlite3 command-line tool, 3.35 or newer):
--   sqlite3 -csv -header :memory: "CREATE TABLE weekly (sku INTEGER, week TEXT, units INTEGER, revenue REAL);" ".import --skip 1 weekly_sales.csv weekly" ".read abc_xyz.sql"
WITH RECURSIVE
params(w_start, w_end, x_max, y_max) AS (          -- the only fixed values of the query
    SELECT '2015-01-05', '2017-09-25', 0.5, 1.0
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
