-- Deals created during the report week. Portable (PostgreSQL + SQLite).
SELECT
    COUNT(*)                   AS deal_count,
    COALESCE(SUM(amount), 0)   AS total_value
FROM deals
WHERE created_at >= :week_start
  AND created_at <  :week_end;
