-- Open pipeline by stage for deals that existed by the end of the report week.
-- Uses each deal's *current* stage: the schema keeps no stage history, so this
-- is a snapshot as of the last load, not a reconstruction of the past.
SELECT
    stage,
    COUNT(*)     AS deal_count,
    SUM(amount)  AS total_value
FROM deals
WHERE stage NOT IN ('closed_won', 'closed_lost')
  AND created_at < :week_end
GROUP BY stage
ORDER BY CASE stage
    WHEN 'prospecting'   THEN 1
    WHEN 'qualification' THEN 2
    WHEN 'proposal'      THEN 3
    WHEN 'negotiation'   THEN 4
    ELSE 5
END;
