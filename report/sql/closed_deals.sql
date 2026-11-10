-- Won / lost deals whose close_date falls in the report week, and the win rate.
-- Portable: aggregate FILTER is in PostgreSQL 9.4+ and SQLite 3.30+.
-- Win rate = won / (won + lost); NULLIF turns "no closed deals" into NULL
-- instead of a division-by-zero error.
SELECT
    COUNT(*) FILTER (WHERE stage = 'closed_won')                AS won_count,
    COALESCE(SUM(amount) FILTER (WHERE stage = 'closed_won'), 0)  AS won_value,
    COUNT(*) FILTER (WHERE stage = 'closed_lost')               AS lost_count,
    COALESCE(SUM(amount) FILTER (WHERE stage = 'closed_lost'), 0) AS lost_value,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE stage = 'closed_won') / NULLIF(COUNT(*), 0),
        1
    )                                                            AS win_rate_pct
FROM deals
WHERE stage IN ('closed_won', 'closed_lost')
  AND close_date >= :week_start_date
  AND close_date <  :week_end_date;
