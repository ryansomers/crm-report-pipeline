-- SQLite version of postgres/weekly_trend.sql (same output columns).
-- SQLite has neither generate_series() nor date_trunc(), so:
--   * a recursive CTE builds the week spine;
--   * a timestamp's Monday is date(ts, '-N days') where
--     N = (weekday + 6) % 7 and strftime('%w') gives Sunday = 0.
-- Window functions (LAG, WINDOW clause) work as in PostgreSQL (SQLite 3.28+).
WITH RECURSIVE weeks(week_start) AS (
    SELECT date(:first_week_date)
    UNION ALL
    SELECT date(week_start, '+7 days') FROM weeks WHERE week_start < date(:week_start_date)
),
created AS (
    SELECT date(created_at,
                '-' || ((CAST(strftime('%w', created_at) AS INTEGER) + 6) % 7) || ' days'
           ) AS week_start,
           COUNT(*)    AS new_deals,
           SUM(amount) AS new_value
    FROM deals
    WHERE created_at >= :first_week AND created_at < :week_end
    GROUP BY 1
),
closed AS (
    SELECT date(close_date,
                '-' || ((CAST(strftime('%w', close_date) AS INTEGER) + 6) % 7) || ' days'
           ) AS week_start,
           COUNT(*) FILTER (WHERE stage = 'closed_won')               AS won_count,
           COALESCE(SUM(amount) FILTER (WHERE stage = 'closed_won'), 0) AS won_value,
           COUNT(*) FILTER (WHERE stage = 'closed_lost')              AS lost_count
    FROM deals
    WHERE stage IN ('closed_won', 'closed_lost')
      AND close_date >= :first_week_date AND close_date < :week_end_date
    GROUP BY 1
),
weekly AS (
    SELECT w.week_start,
           COALESCE(c.new_deals, 0)  AS new_deals,
           COALESCE(c.new_value, 0)  AS new_value,
           COALESCE(k.won_count, 0)  AS won_count,
           COALESCE(k.won_value, 0)  AS won_value,
           COALESCE(k.lost_count, 0) AS lost_count
    FROM weeks w
    LEFT JOIN created c ON c.week_start = w.week_start
    LEFT JOIN closed  k ON k.week_start = w.week_start
)
SELECT
    week_start, new_deals, new_value, won_count, won_value, lost_count,
    LAG(new_deals)  OVER w AS prev_new_deals,
    LAG(new_value)  OVER w AS prev_new_value,
    LAG(won_count)  OVER w AS prev_won_count,
    LAG(won_value)  OVER w AS prev_won_value,
    LAG(lost_count) OVER w AS prev_lost_count
FROM weekly
WINDOW w AS (ORDER BY week_start)
ORDER BY week_start;
